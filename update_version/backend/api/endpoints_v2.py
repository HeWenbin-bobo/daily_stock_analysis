# -*- coding: utf-8 -*-
"""
update_version - 所有 v2 端点
- 前缀：/api/v2
- 端点分组：notifications / alerts / backtest / paper / community / portfolio / data / llm / cron / health
"""
import logging
import os
import hmac
import json
import asyncio
import threading
import time
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request

from db.migrate import get_conn
from core import cron as cron_mod
from core import data_source as ds
from core import llm_router as llm
from core import notifier
from core import audit
from core import ratelimit
from modules import alerts_engine as ae
from modules import backtest_engine as be
from modules import hyperopt_engine as he
from modules import paper_trading as pt
from modules import community as cm
from modules import portfolio as pf
from api._validation import require_keys

# ★ v3 融合 ai-berkshire：深度研究服务（四大师价值投资框架）
from services.deep_research import deep_research, generate_markdown_report

logger = logging.getLogger(__name__)

router = APIRouter()

# 社区热榜的行情富化预算：榜单行本身已在库中，行情只是附加字段。上游数据源
# 挂起时（实测 akshare/baostock 各 15s 超时），给整批一个上限，超时即返回已
# 拿到的部分，避免只读接口长期占用 Web 线程池。
_HOT_LIST_QUOTE_BUDGET_SECONDS = 8.0


# ============================================================================
# 公共 helper：限流统一调用
# ============================================================================
def _require_rate_limit(request: Optional[Request], endpoint_key: str) -> None:
    """★ Round 4：route handler 统一通过此函数校验 ratelimit。
    request 可为 None（缺省 Query 路由），None 时放行（兼容非 rate-limited 路径）。"""
    if request is None:
        return
    ip = ratelimit.get_client_ip(request)
    allowed, retry = ratelimit.check_limit(ip, endpoint_key)
    if not allowed:
        raise HTTPException(429, f"限流，{retry}s 后重试")


def _require_user_match(request: Optional[Request], payload_user_id: Optional[str]) -> None:
    """★ Round 5：owner check helper（严格模式）

    工作模式：
    1) 若请求带 X-User-Id header（UserIDMiddleware 写入 request.state.user_id），
       payload["user_id"] 必须与之相同，否则 403
    2) 若请求未带 X-User-Id header，视为匿名调用 — 但写端点（POST/DELETE/PUT）
       不允许匿名 user_id 伪造，返回 401 提示传 X-User-Id header

    适用：DELETE / 高风险 POST。GET 类公开端点不受影响。

    注意：本函数对无 X-User-Id header 的请求会 401。下面的 _check_resource_owner
    是向后兼容版本，无 header 时不 401 而是回落 payload（单用户桌面应用场景）。
    """
    server_uid = getattr(request.state, "user_id", None) if request else None
    if server_uid is None:
        # 匿名调用方写 user_id → 拒（防伪造）
        raise HTTPException(
            401,
            "需要 X-User-Id header 才能调用此端点（防止 user_id 伪造）",
        )
    if payload_user_id != server_uid:
        raise HTTPException(
            403,
            f"payload user_id 与 X-User-Id header 不一致（可能伪造）",
        )


def _check_resource_owner(
    request: Optional[Request],
    payload_user_id: Optional[str],
    resource_user_id: Optional[str],
) -> None:
    """按 user_id 做资源归属校验（向后兼容模式）。

    与 `_require_user_match` 的核心区别：**不要求前端必须发 X-User-Id header**。
    实测当前前端（update_version/frontend/src/api/）从未携带 X-User-Id，
    若对写端点套用 `_require_user_match` 严格模式，会把所有现有写操作打成 401，
    直接搞坏应用。

    ⚠️ 这不是认证边界，只是"声明了身份就按身份校验"的一致性检查。
    v2 没有登录/会话层，`X-User-Id` 是调用方自填的请求头：
      - 不带头 → 直接放行（本函数的设计取舍，见下方第 3 条）
      - 带头但与 owner 不符 → 403
      - 伪造成 owner 的值 → 通过
    所以它能拦住"带错身份的手滑调用"，**拦不住"根本不声明身份"的调用者**。
    真正的边界要么是绑定 localhost、要么是给 v2 补认证层 —— 见
    update_version/docker-compose.yml 的 ports 注释。

    工作模式（按优先级）：
    1) 若 X-User-Id header 存在 → 它是 authoritative server-side uid；
       若 payload_user_id 也存在，必须一致（防伪造），否则 403；
       effective_uid = server_uid
    2) 若 X-User-Id header 不存在 → 回落信任 payload_user_id（单用户桌面应用场景）；
       effective_uid = payload_user_id
    3) 若两者都不存在 → 无任何 user 信息可校验，直接放行（极端兼容）
    4) resource_user_id 与 effective_uid 不匹配 → 403
    """
    server_uid = getattr(request.state, "user_id", None) if request else None
    if server_uid is not None:
        if payload_user_id is not None and payload_user_id != server_uid:
            raise HTTPException(
                403, "payload user_id 与 X-User-Id header 不一致（可能伪造）",
            )
        effective_uid: Optional[str] = server_uid
    else:
        # 无 header → 回落 payload（向后兼容现有无 X-User-Id 的前端）
        effective_uid = payload_user_id

    # 极端兜底：两个都拿不到 → 跳过校验（保持现有功能零破坏）
    if not effective_uid:
        return
    # 无 resource owner 信息（如资源不存在）→ 不在此处判，由调用方处理 404
    if resource_user_id is None:
        return
    if resource_user_id != effective_uid:
        raise HTTPException(403, "无权操作他人资源")


def _coerce_int(value: Any, field: str) -> int:
    """把外部传入的 resource_id 转成 int，非法值返回 400 而非 500"""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} 必须是整数")


def _coerce_float(value: Any, field: str, *, allow_none: bool = False) -> Optional[float]:
    """把外部传入的数值转成 float，非法值返回 400 而非 500。

    ★ 探索期修复(endpoint 500)：`/portfolio-p0/trades` 里直接写
      `float(payload["quantity"])` / `float(payload.get("fee") or 0.0)`，
      而外层只 `except ValueError` —— `float([100])` / `float({"a":1})` 抛的是
      **TypeError**，一路冒到全局处理器变成 500。实测
      `{"quantity":[100]}`、`{"price":{"a":1}}`、`{"fee":[5]}` 三个都是 500。
      端点层必须是 400：调用方参数错，不是服务端故障。
    """
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or isinstance(value, (dict, list, tuple, set)):
        raise HTTPException(400, f"{field} 必须是数字")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{field} 必须是数字")


# ============================================================================
# Health
# ============================================================================
@router.get("/health")
def health():
    return {"status": "ok", "service": "update_version", "ts": datetime.now().isoformat()}


# ============================================================================
# 通知推送
# ============================================================================
@router.get("/notifications/channels")
def list_notification_channels(user_id: Optional[str] = None):
    return {"channels": notifier.list_channels(user_id)}


@router.post("/notifications/channels")
def add_notification_channel(payload: dict):
    # ★ Round 4：input validation + audit 错误路径
    required = ("user_id", "channel_type", "target")
    for f in required:
        if not payload.get(f):
            raise HTTPException(400, f"{f} 必填")
    try:
        ch_id = notifier.add_channel(
            user_id=payload["user_id"],
            channel_type=payload["channel_type"],
            target=payload["target"],
            enabled=payload.get("enabled", True),
            quiet_hours_start=payload.get("quiet_hours_start"),
            quiet_hours_end=payload.get("quiet_hours_end"),
        )
    except ValueError as e:
        audit.record(action="create_notification_channel", user_id=payload.get("user_id", "anonymous"),
                     status="error", error_message=str(e))
        raise HTTPException(400, str(e))
    audit.record(action="create_notification_channel", user_id=payload["user_id"], resource_id=str(ch_id))
    return {"channel_id": ch_id}


@router.delete("/notifications/channels/{channel_id}")
def delete_notification_channel(channel_id: int, request: Request, user_id: str = ""):
    """★ Round 5：user_id 必须与 X-User-Id header 一致"""
    _require_user_match(request, user_id or None)
    # 进一步校验该 channel 属于该 user（防 ID 伪造）
    ch = notifier._get_channel(channel_id)
    if not ch:
        raise HTTPException(404, "channel 不存在")
    if ch["user_id"] != user_id:
        raise HTTPException(403, "无权删除他人 channel")
    if not notifier.delete_channel(channel_id):
        raise HTTPException(404, "channel 不存在")
    audit.record(action="delete_notification_channel", user_id=user_id, resource_id=str(channel_id))
    return {"deleted": channel_id}


@router.post("/notifications/test")
def test_notification(payload: dict, request: Request):
    """测试推送（限流 5/min/IP）

    ★ 本轮修复(M4)：补归属校验 —— A 不能用 B 的 channel_id 发推送消耗 B 的配额
    """
    ip = ratelimit.get_client_ip(request)
    allowed, retry = ratelimit.check_limit(ip, "/api/v2/notifications/test")
    if not allowed:
        raise HTTPException(429, f"限流，{retry}s 后重试")
    require_keys(payload, "channel_id")
    ch_id = payload["channel_id"]
    # ★ 本轮修复(M4)：校验 channel 属于当前用户
    ch = notifier._get_channel(ch_id)
    if not ch:
        raise HTTPException(404, "channel 不存在")
    _check_resource_owner(request, payload.get("user_id"), ch.get("user_id"))
    result = notifier.send(ch_id, title=payload.get("title", "测试推送"), content=payload.get("content", "这是一条测试推送"))
    return {
        "success": result.success,
        "error_message": result.error_message,
        "retry_count": result.retry_count,
        "channel_type": result.channel_type,
    }


@router.get("/notifications/logs")
def list_notification_logs(user_id: Optional[str] = None, status: Optional[str] = None, limit: int = Query(50, le=200)):
    return {"logs": notifier.list_logs(user_id, status, limit)}


# ============================================================================
# 异动规则
# ============================================================================
@router.get("/alerts/rule-types")
def list_alert_rule_types():
    return {"rule_types": ae.list_rule_types()}


@router.get("/alerts/rules/{rule_id}")
def get_alert_rule(rule_id: int, request: Request = None, user_id: str = ""):
    rule = ae.get_rule(rule_id)
    if not rule:
        raise HTTPException(404, "rule 不存在")
    # 归属一致性检查（非安全边界，见 _check_resource_owner 的说明）。
    # ★ 探索期修复(IDOR)：按 id 直读需校验归属 —— 实测带 X-User-Id: bob
    #   读到了 u_cd 的规则 32（含 symbol_filter / 冷却时间等配置）。
    _check_resource_owner(request, user_id or None, rule.get("user_id"))
    return rule


@router.post("/alerts/rules")
def create_alert_rule(payload: dict):
    require_keys(payload, "user_id", "name", "rule_type")
    try:
        rid = ae.create_rule(
            user_id=payload["user_id"],
            name=payload["name"],
            rule_type=payload["rule_type"],
            params=payload.get("params", {}),
            symbol_filter=payload.get("symbol_filter"),
            cooldown_minutes=payload.get("cooldown_minutes", 60),
            enabled=payload.get("enabled", True),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.record(action="create_alert_rule", user_id=payload["user_id"], resource_id=str(rid))
    return {"rule_id": rid}


@router.get("/alerts/rules")
def list_alert_rules(user_id: Optional[str] = None):
    return {"rules": ae.list_rules(user_id)}


@router.put("/alerts/rules/{rule_id}")
def update_alert_rule(rule_id: int, payload: dict, request: Request):
    """★ 本轮修复(M4)：补归属校验 —— 拿 rule 看 owner 再校验

    ★ Round 19 修复(M5)：payload 若含 rule_id/user_id 等字段，**payload 展开会抛
      TypeError: got multiple values for argument 'rule_id' → 500。白名单过滤后再展开。
    """
    # ★ 本轮修复(M5)：在 update_rule 白名单增加 rule_type，端点透传 → alerts_engine.update_rule 同步支持
    allowed = {"name", "symbol_filter", "enabled", "cooldown_minutes", "params", "rule_type"}
    clean = {k: v for k, v in payload.items() if k in allowed}
    # 先查 rule 做归属校验（resource 不存在应返回 404，与原行为对齐）
    rule = ae.get_rule(rule_id)
    if not rule:
        raise HTTPException(404, "rule 不存在")
    # payload 里没有 user_id 时也校验 rule 归属（防止无 user_id 也能改）
    _check_resource_owner(request, payload.get("user_id"), rule.get("user_id"))
    try:
        if not ae.update_rule(rule_id, **clean):
            raise HTTPException(404, "rule 不存在")
    except ValueError as e:
        # ★ 本轮修复(M5)：alerts_engine.update_rule 在 rule_type 非法时抛 ValueError，
        #   这里转换为 400 而非 500（用户输入错误是 4xx 而非 5xx）
        raise HTTPException(400, f"参数错误: {e}")
    return {"updated": rule_id}


@router.delete("/alerts/rules/{rule_id}")
def delete_alert_rule(rule_id: int, request: Request):
    """★ Round 5：owner check"""
    user_id = request.query_params.get("user_id", "")
    _require_user_match(request, user_id or None)
    rule = ae.get_rule(rule_id)
    if not rule:
        raise HTTPException(404, "rule 不存在")
    if rule.get("user_id") != user_id:
        raise HTTPException(403, "无权删除他人异动规则")
    if not ae.delete_rule(rule_id):
        raise HTTPException(404, "rule 不存在")
    audit.record(action="delete_alert_rule", user_id=user_id, resource_id=str(rule_id))
    return {"deleted": rule_id}


@router.post("/alerts/rules/bulk-toggle")
def alerts_bulk_toggle(payload: dict, request: Request):
    """★ v2 audit: 批量启用/禁用（限流 5/min/IP）

    ★ 本轮修复(M4)：逐条校验 rule 归属 —— 之前完全无校验，
       A 可以用一串别人的 rule_id 把自己想用的禁掉
    """
    _require_rate_limit(request, "/api/v2/alerts/rules/bulk-toggle")
    rule_ids = payload.get("rule_ids", [])
    enabled = payload.get("enabled", True)
    # ★ 本轮修复(M4)：先全部读出来校验归属，再批量 toggle
    # 一次性 SELECT IN 避免 N+1
    rules = [ae.get_rule(rid) for rid in rule_ids]
    missing = [rid for rid, r in zip(rule_ids, rules) if r is None]
    if missing:
        raise HTTPException(404, f"rule 不存在: {missing}")
    # 取 effective uid
    server_uid = getattr(request.state, "user_id", None) if request else None
    effective_uid = server_uid or payload.get("user_id")
    if effective_uid:
        # 找到不属于 effective_uid 的 rule
        foreign = [r["id"] for r in rules if r.get("user_id") != effective_uid]
        if foreign:
            raise HTTPException(403, f"无权操作他人 rule: {foreign}")
    affected = ae.bulk_toggle(rule_ids, enabled)
    return {"affected": affected, "enabled": enabled}


@router.post("/alerts/scan")
def trigger_alert_scan(request: Request):
    """触发全量异动扫描（限流 3/min/IP — 扫描可能很耗时）"""
    ip = ratelimit.get_client_ip(request)
    allowed, retry = ratelimit.check_limit(ip, "/api/v2/alerts/scan")
    if not allowed:
        raise HTTPException(429, f"限流，{retry}s 后重试")
    summary = ae.scan_all_alerts()
    return summary


@router.get("/alerts/triggers")
def list_alert_triggers(user_id: Optional[str] = None, rule_id: Optional[int] = None, limit: int = Query(100, le=500)):
    return {"triggers": ae.list_triggers(user_id, rule_id, limit)}


# ============================================================================
# 策略回测
# ============================================================================
@router.post("/backtest/strategies")
def create_backtest_strategy(payload: dict):
    require_keys(payload, "user_id", "name", "natural_language")
    sid = be.create_strategy(
        user_id=payload["user_id"],
        name=payload["name"],
        natural_language=payload["natural_language"],
        description=payload.get("description", ""),
    )
    audit.record(action="create_strategy", user_id=payload["user_id"], resource_id=str(sid))
    return {"strategy_id": sid}


@router.get("/backtest/strategies")
def list_backtest_strategies(user_id: Optional[str] = None):
    return {"strategies": be.list_strategies(user_id)}


@router.get("/backtest/strategies/{strategy_id}")
def get_backtest_strategy(strategy_id: int):
    s = be.get_strategy(strategy_id)
    if not s:
        raise HTTPException(404, "strategy 不存在")
    return s


@router.delete("/backtest/strategies/{strategy_id}")
def delete_backtest_strategy(strategy_id: int, request: Request, user_id: str = ""):
    """★ 本轮修复(HIGH)：补归属校验 —— 之前完全无校验（甚至不接收 user_id），
    任何人枚举 strategy_id 即可删除他人策略。对齐 delete_alert_rule 的模式。"""
    _require_user_match(request, user_id or None)
    s = be.get_strategy(strategy_id)
    if not s:
        raise HTTPException(404, "strategy 不存在")
    if s.get("user_id") != user_id:
        raise HTTPException(403, "无权删除他人策略")
    if not be.delete_strategy(strategy_id):
        raise HTTPException(404, "strategy 不存在")
    audit.record(action="delete_backtest_strategy", user_id=user_id, resource_id=str(strategy_id))
    return {"deleted": strategy_id}


@router.post("/backtest/strategies/{strategy_id}/compile")
async def compile_backtest_strategy(strategy_id: int, payload: dict = {}, request: Request = None):
    """★ Round 6：改 async + asyncio.to_thread（LLM 编译可能阻塞 20s+）

    ★ 本轮修复(HIGH)：补归属校验 —— compile 会调用 LLM（计费记在 strategy 所属
    user_id 下）并覆写该 strategy 的 compiled_dsl，之前任何人可对他人 strategy_id
    调用，白嫖 LLM 配额并篡改他人策略的编译结果。
    """
    strategy = be.get_strategy(strategy_id)
    if not strategy:
        raise HTTPException(404, f"strategy {strategy_id} 不存在")
    _check_resource_owner(request, payload.get("user_id"), strategy.get("user_id"))
    try:
        return await asyncio.to_thread(
            be.compile_strategy,
            strategy_id,
            task_complexity=payload.get("task_complexity", "medium"),
            user_id=payload.get("user_id"),
        )
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/backtest/run")
async def run_backtest(payload: dict, request: Request):
    """执行回测（限流 5/min/IP）

    ★ Round 6：改 async + asyncio.to_thread。回测涉及 2 年 K 线 + 指标计算 + sqlite 写，
    最坏情况几分钟；to_thread 让 FastAPI worker 不被独占。
    ★ 本轮修复(HIGH)：补策略归属校验 —— 之前任何人可用别人的 strategy_id 跑回测
    （读取其 compiled_dsl 并生成挂在该 strategy 下的结果），与 compile 端点同源漏洞。
    """
    _require_rate_limit(request, "/api/v2/backtest/run")
    require_keys(payload, "user_id", "strategy_id", "symbol")
    # ★ 探索期修复：strategy_id 传 dict/list 时 sqlite3 绑定参数抛
    #   ProgrammingError → 500（实测 `{"strategy_id": {}}`）。先归一为整数。
    strategy_id = _coerce_int(payload["strategy_id"], "strategy_id")
    # ★ 本轮修复(HIGH)：strategy_id 必须属于 payload.user_id（与 compile/delete 对齐）
    strategy = be.get_strategy(strategy_id)
    if not strategy:
        raise HTTPException(404, f"strategy {strategy_id} 不存在")
    if strategy.get("user_id") != payload["user_id"]:
        raise HTTPException(403, "无权运行他人策略的回测")
    # ★ 迭代2 修复：initial_capital 校验（0 或负数会导致 total_return_pct 除零 500）
    try:
        initial_capital = float(payload.get("initial_capital", 1000000.0))
    except (TypeError, ValueError):
        raise HTTPException(400, "initial_capital 必须是数字")
    if initial_capital <= 0:
        raise HTTPException(400, "initial_capital 必须 > 0")
    try:
        result = await asyncio.to_thread(
            be.run_backtest,
            user_id=payload["user_id"],
            strategy_id=payload["strategy_id"],
            symbol=payload["symbol"],
            start_date=payload.get("start_date"),
            end_date=payload.get("end_date"),
            initial_capital=initial_capital,
        )
        audit.record(
            action="run_backtest",
            user_id=payload["user_id"],
            resource_type="backtest_result",
            resource_id=str(result.get("result_id")),
        )
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/backtest/results")
def list_backtest_results(user_id: Optional[str] = None, strategy_id: Optional[int] = None, limit: int = Query(50, le=200)):
    return {"results": be.list_results(user_id, strategy_id, limit)}


@router.get("/backtest/results/{result_id}")
def get_backtest_result(result_id: int, request: Request = None, user_id: str = ""):
    r = be.get_result(result_id)
    if not r:
        raise HTTPException(404, "result 不存在")
    # 归属一致性检查（非安全边界，见 _check_resource_owner 的说明）：
    # 声明了身份就按身份校验；不声明仍放行。
    # ★ 探索期修复(IDOR)：按 id 直读的 GET 此前完全无归属校验 —— 任何人猜到
    #   result_id 就能读走他人的回测结果（含全部交易与权益曲线）。
    #   _check_resource_owner 在无 X-User-Id header 时放行，桌面单用户场景零破坏。
    _check_resource_owner(request, user_id or None, r.get("user_id"))
    return r


@router.post("/backtest/compare-results")
def compare_backtest_results(payload: dict):
    """★ v2 audit: 策略结果对比 A vs B"""
    a_id = payload.get("result_id_a")
    b_id = payload.get("result_id_b")
    if not a_id or not b_id:
        raise HTTPException(400, "result_id_a + result_id_b 必填")
    # ★ 探索期修复(500)：list/dict 传进 SQLite 绑定参数会抛 ProgrammingError
    #   （实测 `{"result_id_a":[1]}` → 500 "type 'list' is not supported"）。
    a_id = _coerce_int(a_id, "result_id_a")
    b_id = _coerce_int(b_id, "result_id_b")
    return be.compare_backtest_results(a_id, b_id)


# ============================================================================
# Hyperopt（参数优化，最小版 —— 借鉴 freqtrade Hyperopt 的 optuna 贝叶斯搜索）
# ============================================================================
@router.post("/backtest/hyperopt")
async def run_hyperopt(payload: dict, request: Request):
    """启动一轮参数优化（限流 2/min/IP）。

    payload: {user_id, strategy_id, symbol, epochs?, loss_fn?, start_date?, end_date?}
    - loss_fn: sharpe | sortino | multi_metric
    - ★ 强制样本外验证：优化只在 train 段（前 80% 时间）跑，test 段（后 20%）单独报告
    """
    _require_rate_limit(request, "/api/v2/backtest/hyperopt")
    require_keys(payload, "user_id", "strategy_id", "symbol")
    # ★ 探索期修复：同 backtest/run，strategy_id 需先归一为整数
    strategy_id = _coerce_int(payload["strategy_id"], "strategy_id")
    strategy = be.get_strategy(strategy_id)
    if not strategy:
        raise HTTPException(404, f"strategy {strategy_id} 不存在")
    if strategy.get("user_id") != payload["user_id"]:
        raise HTTPException(403, "无权优化他人策略")
    try:
        result = await asyncio.to_thread(
            he.run_hyperopt,
            user_id=payload["user_id"],
            strategy_id=strategy_id,
            symbol=payload["symbol"],
            epochs=int(payload.get("epochs", 100)),
            loss_fn=payload.get("loss_fn", "sharpe"),
            start_date=payload.get("start_date"),
            end_date=payload.get("end_date"),
        )
        audit.record(
            action="run_hyperopt",
            user_id=payload["user_id"],
            resource_type="hyperopt_run",
            resource_id=str(result.get("run_id")),
        )
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/backtest/hyperopt/runs")
def list_hyperopt_runs(user_id: Optional[str] = None, strategy_id: Optional[int] = None, limit: int = Query(20, le=100)):
    return {"runs": he.list_runs(user_id, strategy_id, limit)}


@router.get("/backtest/hyperopt/runs/{run_id}")
def get_hyperopt_run(run_id: int):
    r = he.get_run(run_id)
    if not r:
        raise HTTPException(404, "run 不存在")
    return r


@router.get("/backtest/hyperopt/runs/{run_id}/epochs")
def get_hyperopt_epochs(run_id: int, limit: int = Query(100, le=500)):
    return {"epochs": he.list_epochs(run_id, limit)}


# ★ P0：应用最优参数到策略（闭环最后一环）
@router.post("/backtest/hyperopt/runs/{run_id}/apply")
def apply_hyperopt_params(run_id: int, payload: dict):
    """把 run 的 best_params 写回策略 compiled_dsl，原 DSL 备份到 description。"""
    require_keys(payload, "user_id")
    try:
        result = he.apply_best_params(run_id, payload["user_id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.record(
        action="apply_hyperopt_params",
        user_id=payload["user_id"],
        resource_type="hyperopt_run",
        resource_id=str(run_id),
    )
    return result


# ★ P2：清理脏 run（防 API 超时留永久 running 记录）
@router.post("/backtest/hyperopt/cleanup")
def cleanup_hyperopt_runs(payload: dict):
    """把超过 stale 秒数仍 running 的 run 标记 interrupted（默认 1 小时）。

    ★ 探索期修复：`float(payload.get(...))` 未捕获异常 ——
      `{"stale_seconds": "abc"}` 与 `{"stale_seconds": null}` 都抛 TypeError/ValueError
      → 500（实测）。`null` 尤其容易触发：前端把未填字段序列化成 null。
    """
    raw = payload.get("stale_seconds", 3600)
    if raw is None or raw == "":
        stale = 3600.0
    else:
        try:
            stale = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, "stale_seconds 必须是数字")
    if stale <= 0:
        raise HTTPException(400, "stale_seconds 必须 > 0")
    return {"interrupted": he.cleanup_stale_runs(stale)}


# ============================================================================
# 模拟盘
# ============================================================================
@router.post("/paper/accounts")
def create_paper_account(payload: dict):
    require_keys(payload, "user_id", "name")
    # ★ 修复(除零)：initial_capital=0 时 leaderboard/overview 的收益率计算除零 → 500。
    #   在源头拒绝非法初始资金。
    initial_capital = payload.get("initial_capital", 1000000.0)
    try:
        initial_capital = float(initial_capital)
    except (TypeError, ValueError):
        raise HTTPException(400, "initial_capital 必须是数字")
    if initial_capital <= 0:
        raise HTTPException(400, "initial_capital 必须 > 0")
    # ★ 探索期修复(静默丢失参数)：(user_id, name) 上有 UNIQUE 约束，
    #   `create_account` 撞唯一键时会**返回已存在账户的 id**（组合模块的惰性
    #   建户依赖这个语义），但端点直接把这个 id 当"新建成功"返回 ——
    #   实测：以 initial_capital=1000 建 dup_acct → {"account_id":91}；
    #   同名再以 500000 建 → 仍是 {"account_id":91} 且 HTTP 200，
    #   库里 initial_capital 仍是 1000，用户以为改成了 50 万。
    #   端点层明确拒绝并告知已有账户，避免"看起来成功、参数被吞"。
    name = payload.get("name")
    if isinstance(name, str) and name.strip():
        existing = next(
            (a for a in pt.list_accounts(payload["user_id"]) if a.get("name") == name.strip()),
            None,
        )
        if existing:
            raise HTTPException(
                409,
                f"同名账户「{name.strip()}」已存在（id={existing['id']}，"
                f"初始资金 {existing['initial_capital']:.0f}）。"
                f"请换一个名称，或直接使用该账户",
            )
    # ★ 探索期修复：create_account 会把类型/范围校验错误抛成 ValueError
    #   （name/user_id 非字符串、资金越界），端点不接住就会变 500。
    try:
        acc_id = pt.create_account(
            user_id=payload["user_id"],
            name=payload["name"],
            initial_capital=initial_capital,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    # ★ 修复(M3)：资金类操作补审计（旧实现完全无审计，无法追溯账户创建）
    audit.record(action="create_paper_account", user_id=payload["user_id"], resource_id=str(acc_id))
    return {"account_id": acc_id}


@router.get("/paper/accounts")
def list_paper_accounts(user_id: Optional[str] = None):
    return {"accounts": pt.list_accounts(user_id)}


@router.delete("/paper/accounts/{account_id}")
def delete_paper_account(account_id: int, request: Request):
    """★ Round 5：owner check（必须与 channel/account 拥有者匹配）"""
    # 取 query param
    user_id = request.query_params.get("user_id", "")
    _require_user_match(request, user_id or None)
    # 校验 account 拥有者
    acc = pt.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账户不存在")
    if acc["user_id"] != user_id:
        raise HTTPException(403, "无权删除他人账户")
    if not pt.delete_account(account_id):
        raise HTTPException(404, "账户不存在")
    audit.record(action="delete_paper_account", user_id=user_id, resource_id=str(account_id))
    return {"deleted": account_id}


@router.get("/paper/accounts/{account_id}/overview")
def get_paper_account_overview(account_id: int, request: Request = None, user_id: str = ""):
    overview = pt.get_account_overview(account_id)
    if not overview:
        raise HTTPException(404, "账户不存在")
    # 归属一致性检查（非安全边界，见 _check_resource_owner 的说明）。
    # ★ 探索期修复(IDOR)：按 id 直读的 GET 无归属校验 —— 实测带 X-User-Id: bob
    #   读到了 conc10 账户 90 的余额与最新快照。写端点早有 M4 校验，读端点漏了。
    _check_resource_owner(request, user_id or None, (overview.get("account") or {}).get("user_id"))
    return overview


@router.post("/paper/accounts/{account_id}/orders")
def place_paper_order(account_id: int, payload: dict, request: Request):
    require_keys(payload, "symbol", "side", "quantity")
    # ★ 本轮修复(M4)：先取账户校验归属（之前完全无校验，A 能给 B 的账户下单）
    acc = pt.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账户不存在")
    _check_resource_owner(request, payload.get("user_id"), acc.get("user_id"))
    try:
        result = pt.place_order(
            account_id=account_id,
            symbol=payload["symbol"],
            side=payload["side"],
            quantity=payload["quantity"],
            price=payload.get("price"),
            reason=payload.get("reason", "manual"),
            related_alert_id=payload.get("related_alert_id"),
            related_strategy_id=payload.get("related_strategy_id"),
        )
        audit.record(action="paper_order", resource_id=str(result.get("trade_id")), request_body=payload)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/paper/accounts/{account_id}/trades")
def list_paper_trades(account_id: int, limit: int = Query(100, le=500), request: Request = None, user_id: str = ""):
    # 归属一致性检查（非安全边界，见 _check_resource_owner 的说明）。
    acc = pt.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账户不存在")
    _check_resource_owner(request, user_id or None, acc.get("user_id"))
    return {"trades": pt.list_trades(account_id, limit)}


@router.post("/paper/accounts/{account_id}/snapshot")
async def take_paper_snapshot(account_id: int, payload: dict = {}, request: Request = None):
    """★ Round 6：async + to_thread（snapshot 内部 fetch_quote_with_fallback + sqlite 写）

    ★ 本轮修复(M4)：补归属校验（之前 A 可对 B 的账户做 snapshot，污染数据）
    """
    acc = pt.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账户不存在")
    _check_resource_owner(request, payload.get("user_id"), acc.get("user_id"))
    try:
        return await asyncio.to_thread(pt.take_snapshot, account_id, payload.get("snapshot_date"))
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/paper/accounts/{account_id}/snapshots")
def list_paper_snapshots(account_id: int, limit: int = Query(365, le=1000), request: Request = None, user_id: str = ""):
    # 归属一致性检查（非安全边界，见 _check_resource_owner 的说明）。
    acc = pt.get_account(account_id)
    if not acc:
        raise HTTPException(404, "账户不存在")
    _check_resource_owner(request, user_id or None, acc.get("user_id"))
    return {"snapshots": pt.list_snapshots(account_id, limit)}


@router.get("/paper/leaderboard")
def paper_leaderboard(limit: int = Query(50, le=200)):
    return {"leaderboard": pt.leaderboard(limit)}


@router.post("/paper/transfer")
def paper_transfer(payload: dict, request: Request):
    """★ v2 audit: 账户间转账（限流 10/min/IP）

    ★ 本轮修复(M4)：补归属校验 —— 转出/转入两个账户都必须属于当前用户，
       否则任何人拿到一对 account_id 都能搬空别人的钱
    """
    _require_rate_limit(request, "/api/v2/paper/transfer")
    require_keys(payload, "from_account_id", "to_account_id", "amount")
    # ★ 本轮修复(M4)：转出转入账户双校验
    from_id = payload["from_account_id"]
    to_id = payload["to_account_id"]
    from_acc = pt.get_account(from_id)
    if not from_acc:
        raise HTTPException(404, f"转出账户 {from_id} 不存在")
    to_acc = pt.get_account(to_id)
    if not to_acc:
        raise HTTPException(404, f"转入账户 {to_id} 不存在")
    # 取有效 user_id：优先服务端 header（与 _check_resource_owner 行为对齐）
    server_uid = getattr(request.state, "user_id", None) if request else None
    effective_uid = server_uid or payload.get("user_id")
    if effective_uid:
        if from_acc["user_id"] != effective_uid:
            raise HTTPException(403, "无权操作他人转出账户")
        if to_acc["user_id"] != effective_uid:
            raise HTTPException(403, "无权操作他人转入账户")
    try:
        return pt.transfer_between_accounts(
            from_account_id=from_id,
            to_account_id=to_id,
            amount=payload["amount"],
            note=payload.get("note", ""),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))


# ============================================================================
# 社区
# ============================================================================
@router.get("/community/hot-list")
def community_hot_list(date: Optional[str] = None, limit: int = Query(50, le=200)):
    # ★ 迭代2 修复(M2)：date=None 时 date 与 items 必须用同一个日期，否则响应显示
    #   "最新日期" 却查今天（可能无数据）→ 自相矛盾
    # ★ 本轮修复：榜单行的行情富化设总预算。这是只读接口且榜单数据已在库里，
    #   上游数据源挂起时（实测 akshare/baostock 各 15s 超时）不能把请求线程占死。
    actual_date = date or cm.get_latest_hot_list_date()
    return {
        "date": actual_date,
        "items": (
            cm.get_daily_hot_list(actual_date, limit, quote_budget_seconds=_HOT_LIST_QUOTE_BUDGET_SECONDS)
            if actual_date else []
        ),
    }


@router.post("/community/hot-list/refresh")
def community_hot_list_refresh(top_n: int = Query(50, le=200), request: Request = None):
    """刷新每日公开榜单（限流 1/10min/IP — 抓全网数据很贵）"""
    _require_rate_limit(request, "/api/v2/community/hot-list/refresh")
    return cm.refresh_daily_hot_list(top_n)


@router.post("/community/shares")
def create_community_share(payload: dict):
    """★ 本轮修复(CRITICAL)：create_share 之前从不校验 resource_id 是否真的属于
    payload['user_id'] —— 任何人都能对别人的 backtest_result / paper_account 生成
    "公开"分享短链，创建者自己就能拿到 share_token 并立刻用 resolve_share 读到
    别人的回测明细（trades_json/equity_curve_json）或模拟盘账户现金余额，
    等价于绕过所有归属校验的读任意资源漏洞。analysis_run/watchlist/market_review
    在当前设计里是全局资源（表里无 user_id 列），维持可分享。
    """
    require_keys(payload, "user_id", "resource_type", "resource_id")
    resource_type = payload["resource_type"]
    resource_id = payload["resource_id"]
    user_id = payload["user_id"]
    if resource_type == "backtest_result":
        res = be.get_result(_coerce_int(resource_id, "resource_id"))
        if not res:
            raise HTTPException(404, "backtest_result 不存在")
        if res.get("user_id") != user_id:
            raise HTTPException(403, "无权分享他人的回测结果")
    elif resource_type == "paper_account":
        acc = pt.get_account(_coerce_int(resource_id, "resource_id"))
        if not acc:
            raise HTTPException(404, "paper_account 不存在")
        if acc.get("user_id") != user_id:
            raise HTTPException(403, "无权分享他人的模拟盘账户")
    try:
        share = cm.create_share(
            user_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            title=payload.get("title", ""),
            is_public=payload.get("is_public", True),
            expires_in_days=payload.get("expires_in_days"),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.record(action="create_share", user_id=user_id, resource_id=share["share_token"])
    return share


@router.get("/community/shares/{token}")
def resolve_community_share(token: str):
    s = cm.resolve_share(token)
    if not s:
        raise HTTPException(404, "分享不存在或已过期")
    return s


@router.post("/community/follow")
def community_follow(payload: dict, request: Request):
    """★ 本轮修复(MEDIUM)：follower_id 之前完全不校验调用方身份 —— A 可以让
    payload.follower_id=B，伪造 B 关注任意人。有 X-User-Id header 时必须与
    follower_id 一致（与 add_portfolio_holding 的自校验模式一致）。"""
    require_keys(payload, "follower_id", "followee_id")
    _check_resource_owner(request, payload.get("follower_id"), payload.get("follower_id"))
    ok = cm.follow(payload["follower_id"], payload["followee_id"])
    return {"followed": ok}


@router.post("/community/unfollow")
def community_unfollow(payload: dict, request: Request):
    """★ 本轮修复(MEDIUM)：同 community_follow —— 补 follower_id 自校验"""
    require_keys(payload, "follower_id", "followee_id")
    _check_resource_owner(request, payload.get("follower_id"), payload.get("follower_id"))
    ok = cm.unfollow(payload["follower_id"], payload["followee_id"])
    return {"unfollowed": ok}


@router.get("/community/profile/{user_id}")
def community_profile(user_id: str):
    return cm.get_user_profile(user_id)


# ============================================================================
# 组合管理
# ============================================================================
@router.post("/portfolio/holdings")
def add_portfolio_holding(payload: dict, request: Request):
    """★ 本轮修复(M4)：补归属校验 —— 持仓只通过 user_id 隔离；同一个 user_id 下
       重复 add 同 symbol 会持续覆盖（add_holding 现有行为）。
       这里仅校验 caller 有权写入该 user_id 的持仓空间（防止 A 用 user_id=B 写脏数据）。
       ★ 迭代4 修复：捕获 ValueError → 400（cost/quantity 非正会触发 add_holding 的校验，
         之前 FastAPI 默认转 500 "internal error"，对用户是黑盒）。
    """
    require_keys(payload, "user_id", "symbol", "cost", "quantity")
    _check_resource_owner(request, payload.get("user_id"), payload.get("user_id"))
    # ★ 探索期修复(500)：`symbol` 传 list 会让 sqlite3 绑定参数抛
    #   ProgrammingError；`cost`/`quantity` 传 list 则在 add_holding 里的
    #   `cost <= 0` 抛 TypeError —— 两者都不是 ValueError，端点接不住 → 500。
    #   实测 `{"symbol":["600519"]}` 与 `{"cost":[10]}` 都是 500。
    symbol = payload["symbol"]
    if not isinstance(symbol, str) or not symbol.strip():
        raise HTTPException(400, "symbol 必须是非空字符串")
    cost = _coerce_float(payload["cost"], "cost")
    quantity = _coerce_float(payload["quantity"], "quantity")
    try:
        trade_id = pf.add_holding(
            user_id=payload["user_id"],
            symbol=symbol.strip(),
            cost=cost,
            quantity=quantity,
            purchase_date=payload.get("purchase_date"),
            note=payload.get("note", ""),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit.record(action="add_holding", user_id=payload["user_id"], request_body=payload)
    return {"trade_id": trade_id}


@router.get("/portfolio/holdings")
def list_portfolio_holdings(user_id: str):
    return {"holdings": pf.list_holdings(user_id)}


@router.delete("/portfolio/holdings/{symbol}")
def remove_portfolio_holding(symbol: str, user_id: str, request: Request):
    """★ 本轮修复(M4)：补归属校验 —— 删除前先查持仓是否存在；
       用 _check_resource_owner 校验请求方有权操作该 user 的持仓空间。"""
    holdings = pf.list_holdings(user_id)
    if not any(h.get("symbol") == symbol for h in holdings):
        raise HTTPException(404, f"持仓 {symbol} 不存在")
    _check_resource_owner(request, user_id, user_id)
    if not pf.remove_holding(user_id, symbol):
        raise HTTPException(404, f"持仓 {symbol} 不存在")
    # ★ 修复(M3)：删除持仓补审计（旧实现只 add_holding 有审计，remove 无记录）
    audit.record(action="remove_holding", user_id=user_id, resource_id=symbol)
    return {"removed": symbol}


@router.get("/portfolio/overview")
def portfolio_overview(user_id: str):
    return pf.get_overview(user_id)


@router.get("/portfolio/report")
def portfolio_report(user_id: str):
    return pf.generate_report(user_id)


# ============================================================================
# 数据源 + LLM + Cron
# ============================================================================
@router.get("/data/health")
def data_health():
    return ds.get_health_report()


@router.get("/data/quote")
def data_quote(code: str, request: Request):
    """实时行情（限流 120/min/IP）

    ★ 本轮修复(M-mock)：返回 mock 数据时，在响应体里追加 `is_mock: true`，
    前端如需分辨真假数据，无需改动接口签名。
    选 body 字段而非 header 的理由：与 /data/kline 对称，前端可在 axios interceptor
    里统一判断 `data.is_mock === true`，或在组件里直接读取 r.data.is_mock。
    """
    _require_rate_limit(request, "/api/v2/data/quote")
    quote = ds.fetch_quote_with_fallback(code)
    if quote is None:
        raise HTTPException(404, f"无法获取 {code} 行情")
    # ★ 本轮修复(M-mock)：mock 数据对调用方可见，但保持原 dict 结构 + 追加标记字段，
    #   现有消费方按未识别字段忽略即可，TS interface 因可选字段也不会报错。
    if ds.is_mock_payload(quote):
        return {**quote, "is_mock": True}
    return quote


@router.get("/data/kline")
def data_kline(
    code: str,
    days: int = Query(120, ge=10, le=1000),
    period: str = Query("daily", pattern="^(daily|weekly|monthly|quarterly|yearly)$"),
    request: Request = None,
):
    """K 线（限流 60/min/IP）★ Round 8：支持 period=daily/weekly/monthly/quarterly/yearly

    ★ 本轮修复(M-mock)：与 /data/quote 对称，响应体顶层追加 `is_mock: true`，
    这样前端只需要在一处解析，K 线数组本身的结构（与 TS KlinePoint[] 完全兼容）不动。
    """
    _require_rate_limit(request, "/api/v2/data/kline")
    kline = ds.fetch_kline_with_fallback(code, period=period, days=days)
    resp = {"code": code, "period": period, "kline": kline}
    if ds.is_mock_payload(kline):
        resp["is_mock"] = True
    return resp


@router.get("/llm/usage")
def llm_usage(user_id: Optional[str] = None, days: int = Query(30, le=365)):
    return llm.get_usage_summary(user_id, days)


@router.post("/llm/chat")
async def llm_chat(payload: dict, request: Request):
    """LLM 调用（限流 10/min/IP）

    ★ Round 5：改 async + asyncio.to_thread，避免 LLM urlopen 阻塞 event loop。
    超时由 llm_router 决定，两条协议不同：OpenAI 兼容路径 20s；
    Anthropic / CC-switch 路径读 LLM_TIMEOUT（默认 300s）且失败会加倍
    max_tokens 重试一次，所以单次请求最坏可达 2×LLM_TIMEOUT。
    这也是本端点限流 10/min 的原因之一。
    """
    ip = ratelimit.get_client_ip(request)
    allowed, retry = ratelimit.check_limit(ip, "/api/v2/llm/chat")
    if not allowed:
        raise HTTPException(429, f"限流，{retry}s 后重试")
    require_keys(payload, "prompt")
    # ★ 迭代3 修复：prompt 类型/长度校验（非字符串或 MB 级 prompt 会打爆 token/带宽）
    prompt = payload["prompt"]
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(400, "prompt 必须是非空字符串")
    if len(prompt) > 100_000:
        raise HTTPException(400, "prompt 过长（上限 10 万字符）")
    system = payload.get("system", "")
    if system and (not isinstance(system, str) or len(system) > 20_000):
        raise HTTPException(400, "system 过长或类型错误")
    return await asyncio.to_thread(
        llm.chat,
        prompt,
        system=system,
        task_complexity=payload.get("task_complexity", "simple"),
        user_id=payload.get("user_id"),
        task_type=payload.get("task_type", "general"),
    )


@router.get("/cron/jobs")
def cron_jobs():
    return {"jobs": cron_mod.list_jobs()}


@router.get("/cron/user-jobs")
def cron_user_jobs(user_id: Optional[str] = None):
    return {"user_jobs": cron_mod.list_user_jobs(user_id)}


@router.post("/cron/user-jobs")
def cron_user_jobs_add(payload: dict, request: Request):
    """添加用户自定义 cron 任务（限流 5/h/IP — 防滥用）"""
    _require_rate_limit(request, "/api/v2/cron/user-jobs")
    required = ("job_id", "func_name", "cron_expr")
    for f in required:
        if not payload.get(f):
            raise HTTPException(400, f"{f} 必填")
    ok = cron_mod.add_user_job(
        user_id=payload.get("user_id", "default"),
        job_id=payload["job_id"],
        func_name=payload["func_name"],
        cron_expr=payload["cron_expr"],
        args=payload.get("args", {}),
        description=payload.get("description", ""),
    )
    if not ok:
        raise HTTPException(400, "添加失败（可能 func_name 不在白名单 / cron 表达式无效）")
    audit.record(action="cron_user_job_add", user_id=payload.get("user_id", "anonymous"),
                 resource_id=payload["job_id"])
    return {"job_id": payload["job_id"]}


@router.delete("/cron/user-jobs/{job_id}")
def cron_user_jobs_remove(job_id: str, request: Request, user_id: str = ""):
    """★ 本轮修复(HIGH)：补归属校验 —— 之前任何人知道/猜到 job_id（用户自定义的
    字符串，非随机 token）即可删除他人的自定义定时任务。"""
    jobs = cron_mod.list_user_jobs()
    job = next((j for j in jobs if j.get("job_id") == job_id), None)
    if not job:
        raise HTTPException(404, f"job {job_id} 不存在")
    _check_resource_owner(request, user_id or None, job.get("user_id"))
    if not cron_mod.remove_user_job(job_id):
        raise HTTPException(404, f"job {job_id} 不存在")
    audit.record(action="cron_user_job_remove", user_id=user_id or job.get("user_id"), resource_id=job_id)
    return {"removed": job_id}


@router.get("/cron/run-log")
def cron_run_log(job_id: Optional[str] = None, limit: int = Query(50, le=500)):
    return {"logs": cron_mod.list_run_log(job_id, limit)}


@router.post("/cron/trigger/{job_id}")
def cron_trigger(job_id: str, request: Request):
    """手动触发（限流 10/min/IP）"""
    _require_rate_limit(request, "/api/v2/cron/trigger")
    if not cron_mod.trigger_now(job_id):
        raise HTTPException(404, f"job {job_id} 不存在")
    audit.record(action="cron_trigger", resource_id=job_id)
    return {"triggered": job_id}


# ============================================================================
# 审计日志
# ============================================================================
@router.get("/audit/recent")
def audit_recent(user_id: Optional[str] = None, action: Optional[str] = None, limit: int = Query(100, le=500)):
    return {"logs": audit.list_recent(user_id, action, limit)}


@router.get("/audit/stats")
def audit_stats(days: int = Query(7, le=90)):
    """审计摘要：按 action 统计 + 错误数"""
    from db.migrate import get_conn
    conn = get_conn()
    cur = conn.cursor()
    since = time.time() - days * 86400
    cur.execute(
        """SELECT action, COUNT(*) as cnt FROM audit_log
           WHERE created_at >= ? GROUP BY action ORDER BY cnt DESC""",
        (since,),
    )
    by_action = [dict(r) for r in cur.fetchall()]
    cur.execute(
        "SELECT COUNT(*) as cnt FROM audit_log WHERE created_at >= ? AND status='error'",
        (since,),
    )
    errors = cur.fetchone()["cnt"]
    cur.execute(
        "SELECT COUNT(*) as cnt FROM audit_log WHERE created_at >= ?",
        (since,),
    )
    total = cur.fetchone()["cnt"]
    return {
        "days": days,
        "total": total,
        "errors": errors,
        "by_action": by_action,
    }


# ============================================================================
# 数据源缓存管理
# ============================================================================
@router.post("/data/cache/clear")
def data_cache_clear():
    from core import data_source as ds
    ds.clear_cache()
    return {"cleared": True, "ts": datetime.now().isoformat()}


# ============================================================================
# 组合再平衡
# ============================================================================
@router.post("/portfolio/rebalance")
def portfolio_rebalance(user_id: str, target_weights: Optional[Dict[str, float]] = None,
                        max_position_pct: float = Query(30.0, ge=1, le=100),
                        max_industry_pct: float = Query(60.0, ge=1, le=100),
                        request: Request = None):
    """再平衡建议（限流 5/min/IP）"""
    _require_rate_limit(request, "/api/v2/portfolio/rebalance")
    try:
        return pf.suggest_rebalance(user_id, target_weights, max_position_pct, max_industry_pct)
    except ValueError as e:
        # ★ 迭代4：target_weights 非法（负值等）→ 400
        raise HTTPException(400, str(e))


# ============================================================================
# Webhook 签名辅助（用于第三方回调验证）
# ★ Round 4 安全加固：webhooks/sign 需要 X-Admin-Token，webhooks/verify 限流保留
# ============================================================================
def _require_admin(request: Request) -> None:
    expected = os.environ.get("APP_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(503, "APP_ADMIN_TOKEN 未配置；webhooks/sign 暂不可用")
    token = request.headers.get("X-Admin-Token", "")
    if not hmac.compare_digest(token, expected):
        raise HTTPException(401, "需要 X-Admin-Token 头才能调用 webhooks/sign")


@router.post("/webhooks/sign")
def webhooks_sign(payload: dict, request: Request):
    """对一段 payload 签名（仅 admin）。客户端带 X-Signature / X-Timestamp 头用。

    ★ Round 4 安全加固：不再允许匿名调用，必须在 header 中带 X-Admin-Token
    并与 APP_ADMIN_TOKEN 环境变量匹配。配置 admin token 后才能签出合法 HMAC。
    """
    _require_admin(request)
    from core.webhook_signing import sign_payload
    body = payload.get("body", "")
    # ★ 探索期修复：body 可能是对象/数字，sign_payload 已能接受任意 JSON 值，
    #   但 None 会变成 "null" 字面量——明确拒绝，避免签出与调用方理解不一致的签名。
    if body is None:
        raise HTTPException(400, "body 不能为 null")
    # ★ Round 19 修复(M8)：timestamp 非法值返回 400 而非 500
    try:
        ts = int(payload.get("timestamp", time.time()))
    except (TypeError, ValueError):
        raise HTTPException(400, "timestamp 必须是整数")
    sig = sign_payload(body, ts)
    audit.record(action="webhook_sign", user_id="admin", resource_type="webhook", resource_id=str(ts))
    return {"signature": sig, "timestamp": ts, "algorithm": "sha256"}


@router.post("/webhooks/verify")
def webhooks_verify(payload: dict, request: Request):
    """校验 webhook 回调签名（默认 5 分钟时间窗，限流 20/min）"""
    ip = ratelimit.get_client_ip(request)
    allowed, retry = ratelimit.check_limit(ip, "/api/v2/webhooks/verify")
    if not allowed:
        raise HTTPException(429, f"限流，{retry}s 后重试")
    from core.webhook_signing import verify_signature
    body = payload.get("body", "")
    # ★ Round 19 修复(M8)：timestamp/max_age 非法值返回 400 而非 500
    try:
        ts = int(payload.get("timestamp", time.time()))
        max_age = int(payload.get("max_age", 300))
    except (TypeError, ValueError):
        raise HTTPException(400, "timestamp / max_age 必须是整数")
    # ★ 迭代3 修复(M10)：max_age 服务端上限（≤ 86400s=1天），防调用方传超大值解除重放窗口
    if max_age <= 0 or max_age > 86400:
        raise HTTPException(400, "max_age 必须在 1-86400 秒之间")
    sig = payload.get("signature", "")
    # ★ 探索期修复：body/signature 类型未校验 → 实测 `{"body":123}`、
    #   `{"signature":123}`、`{"body":{...}}` 全部 500（body.encode / compare_digest
    #   抛 AttributeError·TypeError）。这里统一转成字符串（body 允许任意 JSON，
    #   序列化后参与签名；signature 必须是字符串）。
    if body is None:
        raise HTTPException(400, "body 不能为 null")
    if not isinstance(body, str):
        # ★ 探索期修复：与 sign 端共用 canonical_body —— 此前这里多加了
        #   sort_keys=True，而 sign 没有，字典 body 签出的签名在本端点恒 invalid。
        from core.webhook_signing import canonical_body
        body = canonical_body(body)
    if not isinstance(sig, str):
        raise HTTPException(400, "signature 必须是字符串")
    ok = verify_signature(body, ts, sig, max_age=max_age)
    return {"valid": ok, "max_age": max_age}


# ============================================================================
# 社区分享撤销
# ============================================================================
@router.delete("/community/shares/{token}")
def community_share_revoke(token: str, user_id: str, request: Request):
    """撤销分享。

    ★ 探索期修复(越权)：旧实现只用 query 里的 `user_id` 与分享记录的 user_id
    比对，而 `GET /community/shares/{token}`（公开接口）会把 `user_id` 一并返回。
    实测：拿到任意分享链接 → 读出 user_id → `DELETE ...?user_id=<对方>
    ` 即可删除他人的分享（200 + 之后 404）。现在要求服务端身份
    （X-User-Id header）与分享归属一致。
    """
    share = cm.resolve_share(token) or {}
    owner = share.get("user_id")
    if owner is None:
        # resolve_share 对私有/过期分享返回 None，改用只取 owner 的查询
        owner = cm.get_share_owner(token)
    if owner is None:
        raise HTTPException(404, "分享不存在或无权删除")
    if owner != user_id:
        raise HTTPException(403, "无权删除他人分享")
    _require_user_match(request, user_id)
    if not cm.delete_share_by_token(token, user_id):
        raise HTTPException(404, "分享不存在或无权删除")
    return {"revoked": token}


# ============================================================================
# ★ v3 融合 ai-berkshire：深度研究（四大师价值投资框架）
# ============================================================================
@router.post("/deep-research")
def research_deep(payload: dict, request: Request):
    """对单只股票执行四大师综合深度研究（ai-berkshire 框架）

    payload: {code | code_or_name | name, prefer_provider?}
    - 复用 v3 LLM 路由 + 数据源
    - 返回六维评分表 + 强制结论 + 价格区间 + 风险清单
    """
    code = payload.get("code") or payload.get("code_or_name") or payload.get("name")
    if not code:
        raise HTTPException(400, "code 必填")
    # ★ 探索期修复(500)：`code` 传 dict/list 时下游 `code.strip()` 抛
    #   AttributeError（实测 `{"code":{"a":1}}` → 500「深度研究失败: 'dict'
    #   object has no attribute 'strip'」）。这里显式要求字符串。
    if not isinstance(code, str) or not code.strip():
        raise HTTPException(400, "code 必须是非空字符串")
    code = code.strip()
    # 限流：深度研究较贵（多步 LLM），5/min/IP
    from core import ratelimit as _rl
    ip = _rl.get_client_ip(request)
    allowed, retry = _rl.check_limit(ip, "/api/v2/deep-research")
    if not allowed:
        raise HTTPException(429, f"限流，{retry}s 后重试")
    name = payload.get("name", "")
    prefer_provider = payload.get("prefer_provider")
    try:
        result = deep_research(code, name=name, prefer_provider=prefer_provider)
    except Exception as e:
        logger.exception("deep_research failed for %s: %s", code, e)
        raise HTTPException(500, f"深度研究失败: {str(e)[:200]}")
    # 审计记录
    try:
        audit.record(action="deep_research", user_id=payload.get("user_id", "anonymous"), resource_id=code)
    except Exception:
        pass
    return result


@router.get("/deep-research/{code}/report")
def research_deep_report(code: str, request: Request):
    """深度研究报告 Markdown 渲染（用于导出/分享）"""
    try:
        result = deep_research(code)
        md = generate_markdown_report(result)
        return {"code": code, "markdown": md, "mocked": result.get("mocked", False)}
    except Exception as e:
        logger.exception("deep_research report failed for %s: %s", code, e)
        raise HTTPException(500, f"深度研究报告生成失败: {str(e)[:200]}")


@router.get("/berkshire/tools-health")
def berkshire_tools_health():
    """ai-berkshire 集成工具健康检查（financial_rigor 等是否可用）"""
    import importlib.util
    from pathlib import Path
    tools_dir = Path(__file__).resolve().parent.parent / "services" / "berkshire"
    status = {}
    for name in ("financial_rigor.py", "ashare_data.py", "report_audit.py"):
        p = tools_dir / name
        status[name] = p.exists()
    # 冒烟：financial_rigor calc 是否可执行
    rigor_ok = False
    try:
        from services.deep_research import _load_financial_rigor
        mod = _load_financial_rigor()
        rigor_ok = mod is not None and hasattr(mod, "exact")
    except Exception:
        rigor_ok = False
    return {
        "tools_dir": str(tools_dir),
        "files": status,
        "financial_rigor_importable": rigor_ok,
    }

# ============================================================================
# 组合总览 Dashboard（复用主项目 src/services/portfolio_service — P0 组合域）
# ============================================================================
_pf_lock = threading.Lock()
_pf_service = None


def _get_pf_service():
    """进程内单例 PortfolioService（主项目 src 层，连接主项目数据库）。"""
    global _pf_service
    if _pf_service is None:
        with _pf_lock:
            if _pf_service is None:
                import importlib
                import sys as _sys
                _MAIN_ROOT = Path(__file__).resolve().parent.parent.parent.parent
                v4_snap = {
                    n: m for n, m in _sys.modules.items()
                    if (n == "src" or n.startswith("src.") or n.startswith("data_provider"))
                    and "update_version" in str(getattr(m, "__file__", "") or "")
                }
                while str(_MAIN_ROOT) in _sys.path:
                    _sys.path.remove(str(_MAIN_ROOT))
                _sys.path.insert(0, str(_MAIN_ROOT))
                for n in list(v4_snap):
                    del _sys.modules[n]
                try:
                    mod = importlib.import_module("src.services.portfolio_service")
                finally:
                    _sys.modules.update(v4_snap)
                _pf_service = mod.PortfolioService()
    return _pf_service


@router.get("/portfolio-p0/accounts")
def portfolio_p0_list_accounts():
    """列出组合账户（主项目 P0 组合域）。"""
    return {"accounts": _get_pf_service().list_accounts()}


@router.post("/portfolio-p0/accounts")
def portfolio_p0_create_account(payload: dict):
    require_keys(payload, "name", "market")
    try:
        account = _get_pf_service().create_account(
            name=payload["name"],
            broker=payload.get("broker"),
            market=payload["market"],
            base_currency=payload.get("base_currency") or "",
            owner_id=payload.get("owner_id"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    audit.record(action="create_p0_account", user_id=payload.get("owner_id"), resource_id=str(account.get("id")))
    return account


@router.post("/portfolio-p0/trades")
def portfolio_p0_record_trade(payload: dict):
    """记录一笔交易（buy/sell，含费税；sell 校验可用数量）。"""
    require_keys(payload, "account_id", "symbol", "trade_date", "side", "quantity", "price")
    from datetime import date as _date
    # ★ 探索期修复(500)：这些 float() 此前只被 `except ValueError` 兜住，
    #   而 list/dict 参数抛的是 TypeError → 500。统一走 _coerce_float 转 400。
    account_id = _coerce_int(payload["account_id"], "account_id")
    quantity = _coerce_float(payload["quantity"], "quantity")
    price = _coerce_float(payload["price"], "price")
    fee = _coerce_float(payload.get("fee") or 0.0, "fee")
    tax = _coerce_float(payload.get("tax") or 0.0, "tax")
    try:
        trade_date = _date.fromisoformat(str(payload["trade_date"]))
        result = _get_pf_service().record_trade(
            account_id=account_id,
            symbol=str(payload["symbol"]),
            trade_date=trade_date,
            side=str(payload["side"]),
            quantity=quantity,
            price=price,
            fee=fee,
            tax=tax,
            note=payload.get("note"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        name = type(exc).__name__
        if "Conflict" in name:
            raise HTTPException(409, str(exc))
        raise
    audit.record(action="record_p0_trade", resource_id=str(result.get("id")))
    return result


@router.get("/portfolio-p0/snapshot")
def portfolio_p0_snapshot(
    account_id: Optional[int] = None,
    cost_method: str = "fifo",
    include_realtime: bool = False,
):
    """组合快照：现金/持仓/市值/盈亏（Web 端默认关实时行情，避免慢请求）。"""
    try:
        return _get_pf_service().get_portfolio_snapshot(
            account_id=account_id,
            cost_method=cost_method,
            include_realtime=include_realtime,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ============================================================================
# LLM 设置（Web 界面直接配置自定义模型 + API key；敏感字段脱敏防窥）
# ============================================================================
@router.get("/settings/llm")
def settings_llm_get():
    """LLM 设置状态。敏感字段只回 is_set + 尾 4 位掩码（防窥），永不回明文。"""
    from core import llm_settings
    return llm_settings.get_status()


@router.post("/settings/llm")
def settings_llm_update(payload: dict):
    """保存 LLM 设置并即时生效（写入 llm_settings.json + 同步 os.environ）。

    body: {fields: {field_key: value}}
    - 敏感字段传 "__MASKED__" 表示未修改（保留原值）；传空串表示清除
    - 非敏感字段（base_url/model）传实际值
    """
    from core import llm_settings
    fields = payload.get("fields")
    if not isinstance(fields, dict):
        raise HTTPException(400, "fields 必填（对象）")
    unknown = set(fields) - {f[0] for f in llm_settings.FIELDS}
    if unknown:
        raise HTTPException(400, f"未知字段: {sorted(unknown)}")
    # ★ 探索期修复(500)：`llm_settings.update` 对容器值抛 ValueError
    #   （"字段 X 的值类型不支持（需要字符串）"），但端点此前没接住 → 500。
    #   实测 `{"fields": {"openai_model": ["x"]}}` 返回 500 internal error。
    try:
        result = llm_settings.update(fields)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    audit.record(action="update_llm_settings", request_body={"changed": result["saved"]})
    return result


# ============================================================================
# LLM 设置辅助：上游模型列表拉取 + 连接测试
# ============================================================================
# 云元数据 / 链路本地地址：这些是 SSRF 真正要防的目标（能换出云凭据），
# 且**不可能是合法的 LLM 端点**。故意不封整个私网段 —— 本项目的 cc-switch
# 本地代理就是 `host.docker.internal:15721` / `127.0.0.1:15721`
# （见 core/llm_router.py 的 CC_SWITCH 分支），一刀切会把文档支持的功能打断。
_SSRF_BLOCKED_HOSTS = {
    "metadata.google.internal",
    "metadata.goog",
}
_SSRF_BLOCKED_NETS = ("169.254.0.0/16", "fd00:ec2::/64", "100.100.100.200/32")


def _reject_ssrf_host(url: str) -> None:
    """拒绝指向云元数据 / 链路本地的 base_url。

    ★ 探索期修复(SSRF)：`/settings/llm/test` 与 `/settings/llm/models` 会
      直接向调用方指定的 base_url 发请求并回显上游响应 —— 不校验就等于给了
      一个"任意地址探测 + 响应读取"的原语，最危险的是云元数据
      `169.254.169.254`（可换取实例凭据）。
      httpx 默认 `follow_redirects=False`，所以不存在通过 302 绕到内网的问题；
      这里只需挡住直接的元数据地址。
    """
    from urllib.parse import urlparse
    import ipaddress

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "base_url 必须以 http:// 或 https:// 开头")
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(400, "base_url 缺少主机名")
    if host in _SSRF_BLOCKED_HOSTS:
        raise HTTPException(400, "base_url 指向云元数据地址，已拒绝（SSRF 防护）")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return  # 域名（且不在黑名单）→ 放行
    for net in _SSRF_BLOCKED_NETS:
        try:
            if addr in ipaddress.ip_network(net):
                raise HTTPException(
                    400, "base_url 指向链路本地/云元数据地址，已拒绝（SSRF 防护）"
                )
        except HTTPException:
            raise
        except Exception:
            continue


def _settings_openai_client(base_url: str, api_key: str, timeout: float = 15.0):
    """构造 OpenAI 兼容客户端（openai SDK 可用则用 SDK，否则退回 HTTP 直连）。"""
    try:
        from openai import OpenAI
        return OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
    except Exception:
        return None


@router.post("/settings/llm/models")
def settings_llm_list_models(payload: dict):
    """从上游拉取可用模型列表（OpenAI 兼容 /v1/models）。

    body: {base_url, api_key?, api_key_field?}
    - api_key 为空/哨兵时用已保存或环境变量中的 key（对应 api_key_field）
    返回: {ok, models: [{id, owned_by}], error?}
    """
    import httpx
    from core import llm_settings as ls

    base_url = (payload.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(400, "base_url 必填")
    _reject_ssrf_host(base_url)
    # key 来源优先级：显式传入 > 已保存文件 > 环境变量
    api_key = (payload.get("api_key") or "").strip()
    if not api_key or api_key == ls.MASK_SENTINEL:
        field = payload.get("api_key_field") or "openai_api_key"
        env = next((f[1] for f in ls.FIELDS if f[0] == field), "OPENAI_API_KEY")
        data = ls._load_file()
        api_key = data.get(field) or os.environ.get(env) or ""
    if not api_key:
        return {"ok": False, "models": [], "error": "未设置 API key（请先填写并保存，或在表单中直接输入）"}

    try:
        resp = httpx.get(f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data") if isinstance(data, dict) else data
        models = []
        for m in items or []:
            if isinstance(m, dict) and m.get("id"):
                models.append({"id": m["id"], "owned_by": m.get("owned_by", "")})
        models.sort(key=lambda x: x["id"])
        return {"ok": True, "models": models, "count": len(models)}
    except Exception as exc:
        return {"ok": False, "models": [], "error": str(exc)[:300]}


@router.post("/settings/llm/test")
def settings_llm_test_connection(payload: dict):
    """测试渠道连接：向上游发一条最小对话请求验证 key/base_url/model 可用。

    body: {base_url, model, api_key?, api_key_field?, prompt?}
    返回: {ok, provider_reply?, latency_ms?, error?}
    """
    import time as _time
    import httpx
    from core import llm_settings as ls

    base_url = (payload.get("base_url") or "").strip().rstrip("/")
    model = (payload.get("model") or "").strip()
    if not base_url or not model:
        raise HTTPException(400, "base_url 和 model 必填")
    _reject_ssrf_host(base_url)
    api_key = (payload.get("api_key") or "").strip()
    if not api_key or api_key == ls.MASK_SENTINEL:
        field = payload.get("api_key_field") or "openai_api_key"
        env = next((f[1] for f in ls.FIELDS if f[0] == field), "OPENAI_API_KEY")
        data = ls._load_file()
        api_key = data.get(field) or os.environ.get(env) or ""
    if not api_key:
        return {"ok": False, "error": "未设置 API key"}

    prompt = (payload.get("prompt") or "ping，请回复 pong").strip()
    started = _time.time()
    try:
        resp = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 32,
                "temperature": 0,
            },
            timeout=30.0,
        )
        latency_ms = int((_time.time() - started) * 1000)
        resp.raise_for_status()
        data = resp.json()
        content = ""
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            content = str(data)[:200]
        return {"ok": True, "latency_ms": latency_ms, "reply": content[:200], "model": model}
    except Exception as exc:
        latency_ms = int((_time.time() - started) * 1000)
        detail = str(exc)[:300]
        # httpx.HTTPStatusError 的响应体里常有上游真实错误（无效 key / 模型不存在）
        body = getattr(getattr(exc, "response", None), "text", "")[:200]
        return {"ok": False, "latency_ms": latency_ms, "error": detail, "upstream_body": body}
