# -*- coding: utf-8 -*-
"""
自选股异动监控 - 规则引擎
- 8 种规则：opening_spike / closing_spike / volume_surge / ma_break / volume_ratio / north_flow / limit_up / limit_down
- 扫描入口：scan_all_alerts()（被 cron 每 5 分钟调用）
- 触发后：写入 alert_trigger + 调用 notifier.send() 推送到用户的所有启用通道
"""
import time
import sys
import json
import logging
import importlib.util
from pathlib import Path
from typing import Dict, Any, List, Optional, Callable, Tuple
from datetime import datetime, timedelta

from db.migrate import get_conn
from core import notifier
from core.data_source import fetch_quote_with_fallback, fetch_kline_with_fallback, is_mock_payload

logger = logging.getLogger(__name__)

# ============================================================================
# 规则定义
# ============================================================================
RULE_TYPES = {
    "opening_spike": "开盘 30 分钟涨幅 > 阈值",
    "closing_spike": "尾盘 30 分钟涨幅 > 阈值",
    "volume_surge": "连续 3 日放量",
    "ma_break": "突破/跌破 20 日均线",
    "volume_ratio": "量比 > 阈值",
    "north_flow": "北向资金净流入 > 阈值",
    "limit_up": "涨停",
    "limit_down": "跌停",
}

# 每种规则实际用到（并被 float()/int() 强转）的数值字段。
# ★ v2 迭代2 CRITICAL 回归修复：v4 复制自 v3 时丢失了该校验（v2 Round 53 已修）。
#   无校验时 params={"multiplier":"abc"} 能入库，扫描时 float("abc") 抛错被 try/except
#   吞掉 → 规则永不触发且用户无感知。
_PARAM_NUMERIC_FIELDS: Dict[str, List[str]] = {
    "opening_spike": ["threshold_pct"],
    "closing_spike": ["threshold_pct"],
    "volume_surge": ["multiplier"],
    "ma_break": ["period"],
    "volume_ratio": ["threshold"],
    "north_flow": ["threshold_wan"],
    "limit_up": [],
    "limit_down": [],
}


def validate_rule_params(rule_type: str, params: Optional[Dict[str, Any]]) -> None:
    """校验规则 params 的数值字段可转换；非法值抛 ValueError。

    - params 为 None 或空 dict → 通过（评估器用默认值）
    - 仅校验该规则类型实际用到的数值字段；无关字段不校验（容忍扩展）
    - 数值字段必须是数字或可转数字的字符串；否则抛 ValueError("参数 X 必须是数字")
    """
    if not params:
        return
    numeric_fields = _PARAM_NUMERIC_FIELDS.get(rule_type, [])
    for f in numeric_fields:
        v = params.get(f)
        if v is None or v == "":
            continue  # 未提供 → 评估器用默认值
        try:
            if f == "period":
                n = int(v)
                if n <= 0:
                    raise ValueError
            else:
                float(v)
        except (TypeError, ValueError):
            raise ValueError(f"参数 {f} 必须是数字，got {v!r}")


def list_rule_types() -> List[Dict[str, str]]:
    return [{"type": k, "description": v} for k, v in RULE_TYPES.items()]


# ============================================================================
# 规则 CRUD
# ============================================================================
def create_rule(
    user_id: str,
    name: str,
    rule_type: str,
    params: Dict[str, Any],
    *,
    symbol_filter: Optional[str] = None,
    cooldown_minutes: int = 60,
    enabled: bool = True,
) -> int:
    if rule_type not in RULE_TYPES:
        raise ValueError(f"Unknown rule_type: {rule_type}")
    # ★ v2 迭代2 CRITICAL 回归修复：入库前校验 params 数值字段（非法值如 multiplier="abc" 直接拒绝）
    validate_rule_params(rule_type, params)
    conn = get_conn()
    cur = conn.cursor()
    now = time.time()
    cur.execute(
        """INSERT INTO alert_rule
           (user_id, name, rule_type, symbol_filter, params_json,
            enabled, cooldown_minutes, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, name, rule_type, symbol_filter,
         json.dumps(params, ensure_ascii=False),
         1 if enabled else 0, cooldown_minutes, now, now),
    )
    return cur.lastrowid or 0


def list_rules(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    sql = "SELECT * FROM alert_rule"
    params: list = []
    if user_id:
        sql += " WHERE user_id=?"
        params.append(user_id)
    sql += " ORDER BY created_at DESC"
    cur.execute(sql, params)
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        try:
            d["params"] = json.loads(d.pop("params_json", "{}"))
        except Exception:
            d["params"] = {}
        rows.append(d)
    return rows


def get_rule(rule_id: int) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM alert_rule WHERE id=?", (rule_id,))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    d["params"] = json.loads(d.pop("params_json", "{}"))
    return d


def update_rule(rule_id: int, **fields) -> bool:
    """更新规则（白名单字段）

    ★ 本轮修复(M5)：白名单增加 rule_type（与端点白名单对齐），
      端点层白名单已经含 rule_type 但引擎层未接收 → 调用方以为改成功了。
      加上后要做合法性校验：必须是 RULE_TYPES 里支持的类型。
    """
    allowed = {"name", "symbol_filter", "enabled", "cooldown_minutes", "rule_type"}
    sets = []
    params: list = []
    for k, v in fields.items():
        if k in allowed:
            # ★ 本轮修复(M5)：rule_type 必须校验，否则脏值会破坏评估器
            if k == "rule_type":
                if v not in RULE_TYPES:
                    raise ValueError(f"Unknown rule_type: {v}")
            sets.append(f"{k}=?")
            if k == "enabled":
                v = 1 if v else 0
            params.append(v)
    if "params" in fields:
        # ★ v2 迭代2 CRITICAL 回归修复：更新 params 时同样校验数值字段（与 create_rule 对齐）
        #   注意：更新时 rule_type 可能也在本次更新里，优先用新值，否则读库里旧值
        _rt = fields.get("rule_type")
        if _rt is None:
            existing = get_rule(rule_id)
            _rt = existing.get("rule_type") if existing else None
        if _rt:
            validate_rule_params(_rt, fields["params"])
        sets.append("params_json=?")
        params.append(json.dumps(fields["params"], ensure_ascii=False))
    if not sets:
        return False
    sets.append("updated_at=?")
    params.append(time.time())
    params.append(rule_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE alert_rule SET {','.join(sets)} WHERE id=?", params)
    return cur.rowcount > 0


def delete_rule(rule_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM alert_rule WHERE id=?", (rule_id,))
    return cur.rowcount > 0


# ============================================================================
# ★ 新功能 v2 audit round 1：批量启用/禁用
# ============================================================================
def bulk_toggle(rule_ids: List[int], enabled: bool) -> int:
    """批量启用/禁用规则；返回影响行数"""
    if not rule_ids:
        return 0
    conn = get_conn()
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in rule_ids)
    cur.execute(
        f"UPDATE alert_rule SET enabled=?, updated_at=? WHERE id IN ({placeholders})",
        [1 if enabled else 0, time.time()] + rule_ids,
    )
    return cur.rowcount


# ============================================================================
# 触发记录
# ============================================================================
def list_triggers(
    user_id: Optional[str] = None,
    rule_id: Optional[int] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    sql = "SELECT * FROM alert_trigger WHERE 1=1"
    params: list = []
    if user_id:
        sql += " AND user_id=?"
        params.append(user_id)
    if rule_id:
        sql += " AND rule_id=?"
        params.append(rule_id)
    sql += " ORDER BY triggered_at DESC LIMIT ?"
    params.append(limit)
    cur.execute(sql, params)
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        try:
            d["context"] = json.loads(d.pop("context_json", "{}"))
        except Exception:
            d["context"] = {}
        rows.append(d)
    return rows


# ============================================================================
# 规则评估器
# ============================================================================
# ★ 修复(L2)：缓存 v2 db.history 模块，避免每次扫描（每 5 分钟）重新 import + exec。
_WATCHLIST_DB_MOD = None


def _get_watchlist_symbols(user_id: str) -> List[str]:
    """取用户的自选股代码列表"""
    global _WATCHLIST_DB_MOD
    # ★ Round 19 修复(M7)：旧代码 import update_version.db.history（v1 包）在 v2 进程里
    #   大概率 ImportError 被 try/except 吞掉 → 规则空转且无日志。
    #   优先用 v2 自己的 db.history（update_version_v4/db/history.py），再退 v1。
    if _WATCHLIST_DB_MOD is not None:
        try:
            items = _WATCHLIST_DB_MOD.get_watchlist()
            return [it["code"] for it in items if it.get("code")]
        except Exception as e:
            logger.warning("_get_watchlist_symbols cached mod failed: %s", e)
            _WATCHLIST_DB_MOD = None  # 缓存失效，重新加载
    for path, mod_name in (
        (str(Path(__file__).resolve().parent.parent.parent / "db" / "history.py"), "v2_root_db_history"),
        (str(Path(__file__).resolve().parent.parent.parent.parent / "update_version" / "db" / "history.py"), "update_version_db_history"),
    ):
        try:
            if not Path(path).exists():
                continue
            spec = importlib.util.spec_from_file_location(mod_name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            if hasattr(mod, "get_watchlist"):
                _WATCHLIST_DB_MOD = mod
                items = mod.get_watchlist()
                return [it["code"] for it in items if it.get("code")]
        except Exception as e:
            logger.warning("_get_watchlist_symbols via %s failed: %s", path, e)
            continue
    return []


def _get_symbols_for_rule(rule: Dict[str, Any]) -> List[str]:
    """根据规则的 symbol_filter 决定监控的股票列表"""
    if rule.get("symbol_filter"):
        return [s.strip() for s in rule["symbol_filter"].split(",") if s.strip()]
    return _get_watchlist_symbols(rule["user_id"])


def _evaluate_opening_spike(quote: Dict[str, Any], params: Dict[str, Any], kline: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """开盘异动：现价相对当日开盘涨幅 > 阈值（用当日 K 线的 open 计算，比全天 change_pct 更贴近'开盘'语义）"""
    threshold = float(params.get("threshold_pct", 3.0))
    price = float(quote.get("price", 0) or 0)
    day_open = None
    if kline:
        today_bar = kline[-1]
        day_open = float(today_bar.get("open", 0) or 0)
    if not day_open or day_open <= 0:
        # 无当日 open（收盘后 K 线未含当日）→ 退回全天 change_pct 作为近似
        change_pct = float(quote.get("change_pct", 0) or 0)
        if change_pct > threshold:
            return {"metric": change_pct, "threshold": threshold, "commentary": f"开盘异动(近似) {change_pct}%，突破阈值 {threshold}%"}
        return None
    intraday_from_open = (price - day_open) / day_open * 100
    if intraday_from_open > threshold:
        return {"metric": round(intraday_from_open, 2), "threshold": threshold,
                "commentary": f"现价 {price} 较开盘 {day_open} 涨 {intraday_from_open:.2f}%，突破阈值 {threshold}%"}
    return None


def _evaluate_closing_spike(quote: Dict[str, Any], params: Dict[str, Any], kline: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """尾盘异动：全天涨幅 > 阈值（无分时数据，用全天 change_pct 近似尾盘拉升；阈值默认 2% 更敏感）"""
    threshold = float(params.get("threshold_pct", 2.0))
    change_pct = float(quote.get("change_pct", 0) or 0)
    if change_pct > threshold:
        return {"metric": change_pct, "threshold": threshold, "commentary": f"尾盘拉升(近似) {change_pct}%，突破阈值 {threshold}%"}
    return None


def _evaluate_volume_surge(quote: Dict[str, Any], params: Dict[str, Any], kline: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """连续 3 日放量：当前成交量 > 前 3 日均量 * 倍数"""
    if len(kline) < 4:
        return None
    multiplier = float(params.get("multiplier", 1.5))
    avg_vol = sum(k["volume"] for k in kline[-4:-1]) / 3
    current_vol = quote.get("volume", 0)
    if avg_vol > 0 and current_vol > avg_vol * multiplier:
        return {"metric": current_vol, "threshold": avg_vol * multiplier, "commentary": f"成交量 {current_vol:.0f} > 3日均量 {avg_vol:.0f} × {multiplier}"}
    return None


def _evaluate_ma_break(quote: Dict[str, Any], params: Dict[str, Any], kline: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """突破/跌破 20 日均线"""
    period = int(params.get("period", 20))
    direction = params.get("direction", "above")  # above / below
    if len(kline) < period:
        return None
    ma = sum(k["close"] for k in kline[-period:]) / period
    price = quote.get("price", 0)
    if direction == "above" and price > ma:
        return {"metric": price, "threshold": ma, "commentary": f"价格 {price} 突破 {period} 日均线 {ma:.2f}"}
    if direction == "below" and price < ma:
        return {"metric": price, "threshold": ma, "commentary": f"价格 {price} 跌破 {period} 日均线 {ma:.2f}"}
    return None


def _evaluate_volume_ratio(quote: Dict[str, Any], params: Dict[str, Any], kline: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """量比 > 阈值。
    ★ Round 20 修复(M5)：akshare/tencent quote 常不含 volume_ratio 字段（原实现恒 1.0 永不触发）。
       改为优先取 quote.volume_ratio（tencent 源有），否则用 K 线当前量 / 近 5 日均量计算。
    """
    threshold = float(params.get("threshold", 2.0))
    ratio = float(quote.get("volume_ratio", 0) or 0)
    source = "行情字段"
    if ratio <= 0 and kline and len(kline) >= 6:
        avg5 = sum(k["volume"] for k in kline[-6:-1]) / 5
        cur = float(quote.get("volume", 0) or kline[-1].get("volume", 0) or 0)
        if avg5 > 0 and cur > 0:
            ratio = cur / avg5
            source = "K线估算"
    if ratio > threshold:
        return {"metric": round(ratio, 2), "threshold": threshold, "commentary": f"量比 {ratio:.2f} > {threshold}（{source}）"}
    return None


def _evaluate_north_flow(quote: Dict[str, Any], params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """北向资金净流入 > 阈值（简化版：用金额变化近似）"""
    threshold = float(params.get("threshold_wan", 5000))
    north = quote.get("north_flow_wan", 0)
    if north > threshold:
        return {"metric": north, "threshold": threshold, "commentary": f"北向资金净流入 {north} 万 > {threshold} 万"}
    return None


def _evaluate_limit_up(quote: Dict[str, Any], params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """涨停（涨幅 > 9.5%）"""
    if quote.get("change_pct", 0) > 9.5:
        return {"metric": quote["change_pct"], "threshold": 9.5, "commentary": f"涨停，涨 {quote['change_pct']}%"}
    return None


def _evaluate_limit_down(quote: Dict[str, Any], params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """跌停（跌幅 < -9.5%）"""
    if quote.get("change_pct", 0) < -9.5:
        return {"metric": quote["change_pct"], "threshold": -9.5, "commentary": f"跌停，跌 {quote['change_pct']}%"}
    return None


EVALUATORS = {
    "opening_spike": _evaluate_opening_spike,
    "closing_spike": _evaluate_closing_spike,
    "volume_surge": _evaluate_volume_surge,
    "ma_break": _evaluate_ma_break,
    "volume_ratio": _evaluate_volume_ratio,
    "north_flow": _evaluate_north_flow,
    "limit_up": _evaluate_limit_up,
    "limit_down": _evaluate_limit_down,
}


# ============================================================================
# 扫描入口（被 cron 调用）
# ============================================================================
def scan_all_alerts() -> Dict[str, Any]:
    """扫描所有启用的规则，返回扫描摘要。

    ★ Round 4 优化：先按 (rules × symbols) 收集所有需要的 symbol，
    一次性 fetch_quotes_bulk 拿到所有 quote，再逐条评估；K 线需要时也批量。
    这样原本 "N rules × M symbols" 的串行 fetch 变成 1 次 bulk（受 60s 缓存复用）。
    """
    rules = [r for r in list_rules() if r["enabled"]]
    summary = {"scanned_rules": len(rules), "triggers": 0, "errors": 0}
    if not rules:
        return summary

    # 1) 收集全部 symbol + 决定哪些需要 K 线
    rule_symbols: List[Tuple[Dict[str, Any], str]] = []
    needs_kline_symbols: set = set()
    for rule in rules:
        try:
            symbols = _get_symbols_for_rule(rule)
            for sym in symbols:
                rule_symbols.append((rule, sym))
                # ★ Round 20 修复：opening_spike / closing_spike / volume_ratio
                #   需要用 K 线算开盘涨幅/量比，加入 needs_kline_symbols
                if rule["rule_type"] in ("volume_surge", "ma_break", "opening_spike", "closing_spike", "volume_ratio"):
                    needs_kline_symbols.add(sym)
        except Exception as e:
            logger.exception("Get symbols for rule %s failed: %s", rule.get("id"), e)
            summary["errors"] += 1

    # 2) 批量拉行情（一次并发）
    from core.data_source import fetch_quotes_bulk
    all_symbols = list({sym for _, sym in rule_symbols})
    quotes_map = fetch_quotes_bulk(all_symbols, parallel=True) if all_symbols else {}

    # 3) 批量拉 K 线（只对需要的 symbol，60s 缓存复用）
    from core.data_source import fetch_kline_with_fallback
    klines_map: Dict[str, List[Dict[str, Any]]] = {}
    for sym in needs_kline_symbols:
        klines_map[sym] = fetch_kline_with_fallback(sym, days=60)

    # 4) 评估 + 触发
    # ★ 本轮修复(M-mock)：批量路径同样要排除 mock 数据 —— 定时任务实际走的是这条路径，
    #   如果只在 _evaluate_symbol_for_rule 单只入口做防护等于没修。
    # 同时 kline 是 mock 时也应跳过：异动规则对合成 K 线触发的告警同样无意义。
    for rule, symbol in rule_symbols:
        try:
            quote = quotes_map.get(symbol)
            if not quote or is_mock_payload(quote):
                continue
            # ★ 本轮修复(M-mock)：volume_surge / ma_break / opening_spike /
            #   closing_spike / volume_ratio 类规则走 K 线计算真实指标，
            #   K 线本身是 mock 时同样跳过，避免基于合成数据告警。
            kline_needed = rule["rule_type"] in (
                "volume_surge", "ma_break", "opening_spike", "closing_spike", "volume_ratio"
            )
            if kline_needed and is_mock_payload(klines_map.get(symbol, [])):
                continue
            evaluator = EVALUATORS.get(rule["rule_type"])
            if not evaluator:
                continue
            if kline_needed:
                triggered = evaluator(quote, rule["params"], klines_map.get(symbol, []))
            else:
                triggered = evaluator(quote, rule["params"])
            if triggered:
                _record_trigger_and_notify(rule, symbol, triggered, quote)
                summary["triggers"] += 1
        except Exception as e:
            logger.exception("Scan rule %s symbol %s failed: %s", rule.get("id"), symbol, e)
            summary["errors"] += 1
    return summary


def _evaluate_symbol_for_rule(rule: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
    """评估单只股票 + 单个规则。

    ★ Round 4 提示：scan_all_alerts 已改为批量 fetch，这里仅保留单 symbol 调用入口
    供单独测试或未来按需使用。
    ★ 本轮修复(M-mock)：单只入口同样要排除 mock 数据，否则批量路径防护会被绕过。
    """
    quote = fetch_quote_with_fallback(symbol)
    # ★ 本轮修复(M-mock)：mock 行情下的触发毫无意义（价格是按 code hash 编造的），
    #   直接 return None，跳过触发流程，不写入 trigger 表、不推送通知。
    if quote is None or is_mock_payload(quote):
        return None
    evaluator = EVALUATORS.get(rule["rule_type"])
    if not evaluator:
        return None
    # ★ Round 20：opening/closing/volume_ratio 也走 kline 分支
    if rule["rule_type"] in ("volume_surge", "ma_break", "opening_spike", "closing_spike", "volume_ratio"):
        kline = fetch_kline_with_fallback(symbol, days=60)
        # ★ 本轮修复(M-mock)：K 线也是 mock 时同样放弃评估
        if is_mock_payload(kline):
            return None
        return evaluator(quote, rule["params"], kline)
    return evaluator(quote, rule["params"])


def _record_trigger_and_notify(
    rule: Dict[str, Any],
    symbol: str,
    triggered: Dict[str, Any],
    quote: Optional[Dict[str, Any]] = None,
) -> None:
    """写入 trigger 表 + 推送到用户所有启用通道。

    ★ Round 4 修复：quote 改由调用方传入（避免重复 fetch_quote_with_fallback）。
    ★ 本轮修复(TOCTOU)：cooldown 判定改为**条件 UPDATE 原子认领** —— 旧实现先
       SELECT 读 last_notified_at、函数末尾再无条件 UPDATE，两个并发扫描（cron
       每 5 分钟 + 手动 /alerts/scan）都能读到旧值通过检查 → 各发一次通知、
       各写一条 trigger，用户收到重复告警。现在用单条条件 UPDATE 一次性拿
       到"本次可推送"的排他资格，rowcount==0 说明已被并发扫描抢先认领。
    """
    # 只重读 cooldown_minutes（last_notified_at 由下方条件 UPDATE 原子判断）
    fresh = get_rule(rule["id"]) or rule
    cooldown_sec = (fresh.get("cooldown_minutes") or 60) * 60

    conn = get_conn()
    cur = conn.cursor()
    now = time.time()
    # ★ 本轮修复(TOCTOU)：条件 UPDATE —— 仅在"从未通知过 或 距上次通知已过
    #   cooldown"时才更新成功（rowcount=1）；否则（仍在冷却窗口内，或被并发
    #   扫描抢先）rowcount=0 → 直接跳过。SQLite 写操作串行化保证原子性。
    cur.execute(
        """UPDATE alert_rule SET last_notified_at=?, last_triggered_at=?
           WHERE id=? AND (last_notified_at IS NULL OR ? - last_notified_at >= ?)""",
        (now, now, rule["id"], now, cooldown_sec),
    )
    if cur.rowcount == 0:
        logger.debug("Rule %s in cooldown, skipping notify (atomic claim)", rule["id"])
        # ★ 修复(H7)语义保持：cooldown 期间不写 trigger 表 —— 旧实现每 5 分钟扫描
        #   只要规则仍满足就一直 _record_trigger(...)，alert_trigger 表无限膨胀。
        return

    # 写 trigger 表
    trigger_id = _record_trigger(rule, symbol, triggered, notification_log_id=None)

    # 推送到所有启用通道
    cur.execute(
        "SELECT id FROM notification_channel WHERE user_id=? AND enabled=1",
        (rule["user_id"],),
    )
    channel_ids = [r["id"] for r in cur.fetchall()]

    if quote is None:
        # 单独调用入口（_evaluate_symbol_for_rule）时 quote 为 None，补一次拉取
        quote = fetch_quote_with_fallback(symbol) or {}
    context = {
        "symbol": symbol,
        "name": quote.get("name", symbol),
        "rule_name": RULE_TYPES.get(rule["rule_type"], rule["rule_type"]),
        "metric_value": triggered["metric"],
        "threshold_value": triggered["threshold"],
        "trigger_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "commentary": triggered["commentary"],
    }
    for ch_id in channel_ids:
        try:
            result = notifier.send(
                ch_id,
                template_name="alert_triggered",
                context=context,
            )
            logger.info("Alert notify: rule=%s symbol=%s ch=%s ok=%s", rule["id"], symbol, ch_id, result.success)
        except Exception as e:
            logger.warning("Notify failed for channel %s: %s", ch_id, e)


def _record_trigger(rule, symbol, triggered, notification_log_id) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO alert_trigger
           (rule_id, user_id, symbol, triggered_at, metric_value, threshold_value,
            context_json, notification_log_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (rule["id"], rule["user_id"], symbol, time.time(),
         triggered["metric"], triggered["threshold"],
         json.dumps(triggered, ensure_ascii=False),
         notification_log_id),
    )
    return cur.lastrowid or 0