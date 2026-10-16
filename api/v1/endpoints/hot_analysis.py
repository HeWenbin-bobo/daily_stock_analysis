# -*- coding: utf-8 -*-
"""
===================================
三榜/四榜交集分析 API（POST /api/v1/hot-analysis/run）
===================================

功能：
1. 抓取 4 个榜单实时数据（东方财富 / 雪球 / 同花顺 / 全市场活跃）
2. 至少 2/4 成功即可分析；剔除科创板 688*
3. 对每只股票调用本项目现有的 `StockAnalysisPipeline.analyze_stock()` 进行 3 轮独立分析
4. 过滤：3 轮 score 均 ≥ 60 OR 3 轮 advice 均为 买入/持有
5. 返回结果（含每轮 score / advice / 时间戳）

设计要点：
- 复用 `src/core/pipeline.py:StockAnalysisPipeline` —— 不重新实现 LLM 编排
- 复用 `data_provider/hot_lists.py` —— 独立三榜抓取
- 异步执行（asyncio.to_thread 把同步 LLM 调用扔到线程池）
- 全部异常隔离：单股失败不影响整体；单榜失败降级为空
- 进度由前端"loading"轮询体验承担；当前实现为同步长任务，UI 显示加载动画

未接入：截至 2026-10 该 router 未在任何入口注册（`api/app.py` 无引入），
因此 `POST /api/v1/hot-analysis/run` 对外不可达；前端亦无调用方。
本文件连同 `api/v1/schemas/hot_analysis.py`、`data_provider/hot_lists.py`
（以及未接入的 `data_provider/adaptive_throttle.py`）保留待评估。
重新接入前先确认榜单数据源可用性与同步长任务的超时语义。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from api.v1.schemas.hot_analysis import (
    AnalysisHistoryItem,
    AnalysisHistoryResponse,
    AnalysisRunItem,
    AnalysisTemplate,
    CompareRequest,
    CompareResult,
    FilteredStockItem,
    HotAnalysisPreviewRequest,
    HotAnalysisPreviewResponse,
    HotAnalysisRunRequest,
    HotAnalysisRunResponse,
    HotAnalysisStockResult,
    MarketDataItem,
    MarketDataResponse,
    MarketReviewItem,
    MarketReviewListResponse,
    MarketReviewRequest,
    PDFReportRequest,
    ScheduledTask,
    ScheduledTaskListResponse,
    ScheduledTaskRequest,
    SingleStockRequest,
    SingleStockResponse,
    TaskProgress,
    TemplateListResponse,
    TemplateRequest,
    WatchlistItem,
    WatchlistRequest,
    WatchlistResponse,
)
from data_provider.hot_lists import (
    compute_intersection,
    fetch_all_four,
    filter_a_share,
    _CACHE,
    clear_cache,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level imports so tests can monkeypatch them.
# Local re-imports inside functions stay where they are needed for runtime.
try:
    from src.services.name_to_code_resolver import resolve_name_to_code  # noqa: E402
except Exception:  # pragma: no cover - 服务不可用时仍可单股分析
    resolve_name_to_code = None  # type: ignore[assignment]

# 名称→代码解析预算：包含本地映射 + AkShare 在线降级 + 模糊匹配，必须有上限
# 冷启动 AkShare 全量拉取约 2-5s，留 8s 余量
_NAME_RESOLVE_TIMEOUT_SECONDS = 8.0


def _warm_name_resolver_cache() -> None:
    """
    后台预热 name→code 解析器缓存，避免首次单股分析请求撞上 AkShare 冷启动。

    启动阶段 spawn 一个 daemon 线程去拉 ak.stock_info_a_code_name() 写入模块级缓存；
    主进程不阻塞（except 也不会影响后续请求——没 warm 上就走运行时解析 + 8s 超时）。
    """
    if resolve_name_to_code is None:
        return
    import threading

    def _runner() -> None:
        try:
            # 触发模块级 cache：调用一个不依赖服务的常见名称
            resolve_name_to_code("贵州茅台")
            logger.info("[hot-analysis] name→code 解析器缓存已预热")
        except Exception as e:  # noqa: BLE001
            logger.debug("[hot-analysis] name→code 缓存预热失败（不影响运行）: %s", e)

    threading.Thread(target=_runner, daemon=True, name="warm-name-resolver").start()


# 启动时即触发预热（fire-and-forget；不阻塞 import）
_warm_name_resolver_cache()


# ---------------------------------------------------------------------------
# 单股分析入口：code/name 解析
# ---------------------------------------------------------------------------
def _resolve_code_input(raw: str) -> Optional[str]:
    """
    把单股分析请求中的 `code` 字段统一解析为有效股票代码。

    UI 占位符明确允许用户输入"代码或名称"（如 600519 / 贵州茅台），
    但 `analyze_stock()` 只能接受合法代码——若把中文名称直接当作代码透传，
    所有数据源会失败并最终导致每轮 LLM 超时（用户感知为"三轮都失败"）。

    策略：
    1. 输入已是合法代码（5-6 位数字 / 港股 / 美股符号）→ 直接返回
    2. 输入是名称 → 复用 `resolve_name_to_code`（本地映射 + 拼音 + AkShare 降级 + 模糊匹配），
       并在 _NAME_RESOLVE_TIMEOUT_SECONDS 预算内完成（避免冷启动 AkShare 拉取阻塞请求）
    3. 解析失败 / 超时 / 解析器抛异常 → 返回 None，由调用方决定如何处理

    Args:
        raw: 用户在 UI 输入的字符串（可能带前后空格）。

    Returns:
        标准化后的股票代码；无法解析则返回 None。
    """
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    from src.services.stock_code_utils import is_code_like, normalize_code
    if is_code_like(text):
        normalized = normalize_code(text)
        if normalized:
            return normalized

    # 名称 → 代码：复用项目已存在的 name-to-code resolver
    if resolve_name_to_code is None:
        return None
    try:
        resolved = resolve_name_to_code(text)
    except Exception as e:  # noqa: BLE001 - 解析异常不能阻断分析
        logger.warning("[analyze-single] name_to_code 解析异常 input=%s err=%s", text, e)
        return None
    if not resolved:
        return None
    return normalize_code(resolved) or resolved


# ---------------------------------------------------------------------------
# 全局任务存储（SSE 进度推送 + 后台任务管理）
# ---------------------------------------------------------------------------
_tasks: Dict[str, Dict[str, Any]] = {}
_task_lock = asyncio.Lock()


def _generate_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


_TASK_MAX_AGE_HOURS = 24
# ★ 上限：限制 per-stock 重试次数，避免 LLM 持续失败导致单股永远耗在重试上
# ★ Round 26 修复：用户要求"最多尝试 10 次，累积 3 次成功即停" → 6 提到 10
MAX_ANALYSIS_ATTEMPTS = 10
# ★ SSE 进度队列上限：客户端断开后无人消费，防止 OOM
_PROGRESS_QUEUE_MAXSIZE = 1000
_DROPPED_EVENTS_LOG: Dict[str, int] = {}  # task_id → drop 计数（仅供排障）


def _safe_queue_put(
    queue: "asyncio.Queue",
    event: Dict[str, Any],
    *,
    task_id: str = "",
) -> bool:
    """
    非阻塞把事件塞进 SSE 队列；队列已满时丢弃（进度事件是瞬态，可丢；
    权威状态在 task["result"] 与 DB，不受丢事件影响）。

    Returns:
        True 表示入队；False 表示已丢弃
    """
    try:
        queue.put_nowait(event)
        return True
    except asyncio.QueueFull:
        if task_id:
            _DROPPED_EVENTS_LOG[task_id] = _DROPPED_EVENTS_LOG.get(task_id, 0) + 1
            if _DROPPED_EVENTS_LOG[task_id] == 1 or _DROPPED_EVENTS_LOG[task_id] % 100 == 0:
                logger.warning(
                    "[SSE][%s] 进度队列已满（maxsize=%d），开始丢弃事件 total_dropped=%d",
                    task_id, _PROGRESS_QUEUE_MAXSIZE, _DROPPED_EVENTS_LOG[task_id],
                )
        return False


def _task_cancelled(task: Dict[str, Any]) -> bool:
    """判断任务是否已被取消（cancel_event 优先，兼容旧 status 标记）。"""
    ev = task.get("cancel_event")
    if ev is not None:
        try:
            return bool(ev.is_set())
        except Exception:
            pass
    return task.get("status") == "cancelled"


def _validate_cron_expr(expr: str) -> Optional[str]:
    """
    校验 cron 表达式（5 字段：分 时 日 月 周）。

    Returns:
        None 表示合法；否则返回错误描述。
    """
    if not expr or not isinstance(expr, str):
        return "表达式不能为空"
    fields = expr.strip().split()
    if len(fields) != 5:
        return f"需要 5 个空格分隔字段，实际 {len(fields)} 个"
    # 基础 token 校验（数字、通配符、枚举、范围、步长）
    for f in fields:
        ok_chars = set("0123456789*,-/")
        if not set(f) <= ok_chars or not f:
            return f"非法字段: {f!r}"
    return None


def _cleanup_old_tasks() -> None:
    """清理超过 _TASK_MAX_AGE_HOURS 小时的已完成任务，防止内存泄漏。"""
    now = time.time()
    # ★ 修复（round 5 audit）：cancelled 状态必须也清理——之前漏掉导致 cancel 过的任务
    #   永远占着 _tasks 槽位，长跑 server 上累积
    terminal_statuses = ("completed", "failed", "timeout", "cancelled")
    expired = [
        tid for tid, t in _tasks.items()
        if t.get("status") in terminal_statuses
        and now - t.get("_created_ts", now) > _TASK_MAX_AGE_HOURS * 3600
    ]
    for tid in expired:
        _tasks.pop(tid, None)
    # ★ 修复（round 5 audit）：同步清理 _DROPPED_EVENTS_LOG，否则 task_id 永远累积
    for tid in expired:
        _DROPPED_EVENTS_LOG.pop(tid, None)
    if expired:
        logger.info("[cleanup] 清理了 %d 个过期任务", len(expired))


# ---------------------------------------------------------------------------
# Claude Code (CC) 设置读取 —— 让热分析复用 CC switch 切换的模型配置
# ---------------------------------------------------------------------------
def _cc_settings_path() -> Path:
    """返回 Claude Code 全局 settings.json 的路径。"""
    return Path.home() / ".claude" / "settings.json"


def load_cc_env() -> Dict[str, str]:
    """
    从 ~/.claude/settings.json 的 env 块中读取与 LLM 相关的环境变量。

    CC settings.json 示例::

        {
          "env": {
            "ANTHROPIC_AUTH_TOKEN": "sk-xxx",
            "ANTHROPIC_BASE_URL": "https://...",
            "ANTHROPIC_MODEL": "claude-sonnet-4-20250514"
          }
        }

    返回的 dict 可能包含以下 key（均小写，供内部映射用）：
      - ``model``: CC 当前选中的模型名
      - ``auth_token``: CC 的 API 认证 token
      - ``base_url``: CC 的 API base URL
      - ``api_timeout_ms``: 超时毫秒数
    以及所有原始的 env 变量（保留原始 key 大小写）。
    """
    settings_file = _cc_settings_path()
    if not settings_file.exists():
        return {}
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug("[cc_settings] 读取 %s 失败: %s", settings_file, exc)
        return {}

    env_block = data.get("env") or {}
    if not isinstance(env_block, dict):
        return {}

    result: Dict[str, str] = {}
    for k, v in env_block.items():
        if isinstance(v, str):
            result[k] = v

    # 提取便捷字段（小写 key，供内部逻辑使用）
    # 模型档位回退链：cc-switch / `/model` 通常写 ANTHROPIC_MODEL；缺失时按
    # Opus → Sonnet → Haiku → Fable 的 ANTHROPIC_DEFAULT_*_MODEL 顺序取第一个
    # 非空档位（用户要求市场分析跟随 CC switch 的当前主力模型）。
    model = (result.get("ANTHROPIC_MODEL") or "").strip()
    if not model:
        for _tier_key in (
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_DEFAULT_FABLE_MODEL",
        ):
            _tier_value = (result.get(_tier_key) or "").strip()
            if _tier_value:
                model = _tier_value
                break
    result["model"] = model
    result["auth_token"] = result.get("ANTHROPIC_AUTH_TOKEN", "")
    result["base_url"] = result.get("ANTHROPIC_BASE_URL", "")
    result["api_timeout_ms"] = result.get("API_TIMEOUT_MS", "")

    return result


def _cc_model_to_litellm_model(cc_model: str) -> str:
    """
    将 CC 的模型名转换为 LiteLLM 兼容的模型引用。

    - 如果模型名已带 provider 前缀（如 ``anthropic/claude-sonnet-4-20250514``），原样返回。
    - 如果是 Claude 模型名（如 ``claude-sonnet-4-20250514``），加上 ``anthropic/`` 前缀。
    - 如果是其他自定义模型名（如 ``mimo-v2.5-pro``），也加上 ``anthropic/`` 前缀
      （因为 CC 的 base_url 指向的是 Anthropic 兼容接口）。
    - ★ 剥离 ``[1M]`` 之类的上下文窗口后缀（如 ``claude-opus-4-8[1M]``）：
      litellm 经 CC 代理调用带该后缀的模型名会间歇性返回空内容，剥离后更稳定。
    """
    if not cc_model:
        return ""
    # 剥离 [..] 上下文窗口后缀
    cleaned = re.sub(r"\[[^\]]*\]", "", cc_model).strip()
    if not cleaned:
        return ""
    if "/" in cleaned:
        return cleaned
    # CC 默认走 Anthropic 兼容接口，所有模型都加 anthropic/ 前缀
    return f"anthropic/{cleaned}"


def _has_main_project_llm_config() -> bool:
    """
    检查主项目是否已配置 LLM（.env 中有 LITELLM_MODEL 或任意 API Key）。

    如果主项目已有可用配置，则不需要注入 CC 设置。
    """
    if os.getenv("LITELLM_MODEL", "").strip():
        return True
    for key in (
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY",
        "ANSPIRE_API_KEYS",
        "AIHUBMIX_KEY",
    ):
        if os.getenv(key, "").strip():
            return True
    return False


@contextlib.contextmanager
def _cc_env_override() -> Generator[None, None, None]:
    """
    上下文管理器：在进入时将 CC 设置注入环境变量并重置 Config 单例，
    退出时恢复原始值和单例。

    使用方式::

        with _cc_env_override():
            config = get_config()          # 读到的是注入后的环境变量
            pipeline = StockAnalysisPipeline(config=config, ...)

    注入策略：
    - **始终**读取 CC settings.json（如果存在）。
    - ``LITELLM_MODEL``：始终用 CC 的模型覆盖（用户通过 CC switch 选择的模型优先）。
    - ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_API_BASE``：仅在当前环境未设置时注入
      （不覆盖主项目 .env 中已有的 API 配置）。
    - 重置 Config 单例，使其在上下文内重新从环境变量加载。
    - 退出时恢复所有原始值和单例。
    """
    cc_env = load_cc_env()
    if not cc_env or not cc_env.get("auth_token"):
        logger.debug("[cc_env] CC 设置为空或无 auth_token，跳过注入")
        yield
        return

    cc_model = cc_env.get("model", "")
    cc_token = cc_env.get("auth_token", "")
    cc_base = cc_env.get("base_url", "")
    cc_timeout = cc_env.get("api_timeout_ms", "")

    # ---- 构建要注入的 env 变量 ----
    # 用户要求：LLM 设置必须随时跟随 CC switch。因此只要 CC 有 auth_token，
    # 就用 CC 当前的 token / base_url / model **强制覆盖**（不再"仅未设置时注入"），
    # 避免 .env 或系统残留配置把 LLM 拉回旧供应商。
    inject: Dict[str, str] = {}
    if cc_model:
        inject["LITELLM_MODEL"] = _cc_model_to_litellm_model(cc_model)
    if cc_token:
        inject["ANTHROPIC_API_KEY"] = cc_token
        inject["ANTHROPIC_AUTH_TOKEN"] = cc_token
    if cc_base:
        inject["ANTHROPIC_API_BASE"] = cc_base
        inject["ANTHROPIC_BASE_URL"] = cc_base
    if cc_timeout:
        inject["API_TIMEOUT_MS"] = cc_timeout

    # 走 legacy 单 key 路径：临时移除多通道配置，避免 channel Router（每部署自带
    # api_key / api_base，不读 env）绕过上面注入的 ANTHROPIC_* / LITELLM_MODEL。
    # 仅在 CC 配置存在时移除；退出时恢复。
    _CHANNEL_ENV_KEYS = ("LLM_CHANNELS", "LITELLM_CONFIG", "LITELLM_FALLBACK_MODELS")
    removed_channel_env: Dict[str, Optional[str]] = {}
    for _ck in _CHANNEL_ENV_KEYS:
        removed_channel_env[_ck] = os.environ.pop(_ck, None)

    # ---- 保存原始值并注入 ----
    saved: Dict[str, Optional[str]] = {}
    for key, value in inject.items():
        saved[key] = os.environ.get(key)
        os.environ[key] = value

    if inject:
        logger.info(
            "[cc_env] 已注入 CC 设置: %s (model=%s, base_url=%s)",
            sorted(inject), cc_model, cc_base,
        )

    # ---- 重置 Config 单例，使其在上下文内重新从环境变量加载 ----
    from src.config import Config

    old_instance = Config._instance
    Config._instance = None

    try:
        yield
    finally:
        # 恢复 Config 单例
        Config._instance = old_instance
        # 恢复原始环境变量
        for key, original in saved.items():
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original
        for _ck, _original in removed_channel_env.items():
            if _original is None:
                os.environ.pop(_ck, None)
            else:
                os.environ[_ck] = _original
        logger.debug("[cc_env] 已恢复环境变量和 Config 单例")


# ---------------------------------------------------------------------------
# 过滤规则
# ---------------------------------------------------------------------------
# 任务原文：投资建议均为"买入"或"持有"（不允许出现"卖出"）
_SELL_KEYWORDS = ("卖出", "减仓", "SELL", "REDUCE")
_BUY_HOLD_KEYWORDS = ("买入", "加仓", "持有", "BUY", "HOLD", "ADD")


def _check_pass(
    analyses: List[Dict[str, Any]],
    *,
    min_score: float = 60.0,
    allowed_advice: Optional[List[str]] = None,
    exclude_keywords: Optional[List[str]] = None,
    require_all_rounds: bool = True,
) -> tuple[str, bool]:
    """
    判断 N 轮分析是否满足保留条件（支持自定义过滤规则）。

    Args:
        analyses: 单只股票的所有轮结果列表
        min_score: 最低评分阈值
        allowed_advice: 允许的建议类型列表
        exclude_keywords: 排除关键词列表
        require_all_rounds: 是否要求所有轮都通过

    Returns:
        (pass_reason, passed)
    """
    if allowed_advice is None:
        allowed_advice = list(_BUY_HOLD_KEYWORDS)
    if exclude_keywords is None:
        exclude_keywords = list(_SELL_KEYWORDS)

    valid = [a for a in analyses if not a.get("error")]
    if not valid:
        return "incomplete", False

    scores = [a.get("sentiment_score") for a in valid if a.get("sentiment_score") is not None]
    advices = [str(a.get("operation_advice") or "") for a in valid]

    # 硬性：只要有任何一轮出现排除关键词，直接淘汰
    if any(any(k in a for k in exclude_keywords) for a in advices):
        return "rejected_sell", False

    if require_all_rounds:
        # 所有轮都需满足条件
        pass_score = bool(scores) and all(s >= min_score for s in scores)
        pass_advice = bool(advices) and all(
            any(k in a for k in allowed_advice) for a in advices
        )
    else:
        # 多数轮满足即可
        score_pass_count = sum(1 for s in scores if s >= min_score) if scores else 0
        advice_pass_count = sum(
            1 for a in advices if any(k in a for k in allowed_advice)
        ) if advices else 0
        total = len(valid)
        pass_score = bool(scores) and score_pass_count > total / 2
        pass_advice = bool(advices) and advice_pass_count > total / 2

    if pass_score and pass_advice:
        return "score+advice", True
    if pass_score:
        return "score", True
    if pass_advice:
        return "advice", True
    return "rejected_filter", False


def _is_meaningful_round(round_result: Dict[str, Any]) -> bool:
    """
    一轮分析"有意义" = 没出错 + 有 score 或 advice（至少一个非空）

    为什么需要：有 LLM 调用"没报错"但返回空数据的情况（比如 model 走通
    了但内容为空字符串），这种不能算成功轮。
    """
    if round_result.get("error"):
        return False
    has_score = round_result.get("sentiment_score") is not None
    has_advice = bool((round_result.get("operation_advice") or "").strip())
    return has_score or has_advice


def _majority_advice(advices: List[str]) -> str:
    if not advices:
        return ""
    counter = Counter(advices)
    return counter.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# 单次 LLM 分析（同步包装）
# ---------------------------------------------------------------------------
def _run_one_analysis_sync(
    *,
    pipeline: Any,
    code: str,
    report_type: Any,
    query_id: str,
    round_idx: int,
    round_started_at: datetime,
) -> Dict[str, Any]:
    """
    同步执行一次 analyze_stock，把所有异常降级为 error 字段。

    注：温度扰动通过 current_time 微小偏移 + LLM 自身非确定性实现，
    不去修改 analyzer 内部 LLM 调用（避免侵入现有架构）。
    """
    try:
        result = pipeline.analyze_stock(
            code=code,
            report_type=report_type,
            query_id=query_id,
            current_time=round_started_at,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("analyze_stock 异常 code=%s round=%d", code, round_idx)
        return {
            "round": round_idx,
            "sentiment_score": None,
            "operation_advice": None,
            "decision_type": None,
            "trend_prediction": None,
            "model_used": None,
            "timestamp": round_started_at.isoformat(),
            "error": f"{type(e).__name__}: {e}",
        }

    if result is None:
        return {
            "round": round_idx,
            "sentiment_score": None,
            "operation_advice": None,
            "decision_type": None,
            "trend_prediction": None,
            "model_used": None,
            "timestamp": round_started_at.isoformat(),
            "error": "analyze_stock 返回 None",
        }

    return {
        "round": round_idx,
        "sentiment_score": getattr(result, "sentiment_score", None),
        "operation_advice": getattr(result, "operation_advice", None),
        "decision_type": getattr(result, "decision_type", None),
        "trend_prediction": getattr(result, "trend_prediction", None),
        "model_used": getattr(result, "model_used", None),
        "timestamp": round_started_at.isoformat(),
        "error": None,
    }


# ---------------------------------------------------------------------------
# 辅助函数：抓取 + 求交集（preview 和 run 共享）
# ---------------------------------------------------------------------------
def _compute_intersection_stage(
    req: HotAnalysisRunRequest,
    query_id: str,
) -> Dict[str, Any]:
    """
    执行"抓榜 → 求交集 → 过滤 → 应用 max_intersection 上限" 阶段。

    每次用户启动分析前清空进程内缓存，确保数据是 fresh 的。
    """
    # ★ 用户要求：每次启动分析前重新抓榜
    clear_cache()
    logger.info("[%s] 缓存已清空，重新抓榜", query_id)


    # Step 1: 抓三榜
    try:
        lists = fetch_all_four(req.hot_list_n)
    except Exception as e:
        logger.exception("[%s] 抓取三榜异常", query_id)
        return {"error": f"抓取三榜失败: {e}"}

    summary = {k: len(v) for k, v in lists.items()}
    failed_lists = [k for k, v in lists.items() if not v]
    if failed_lists:
        logger.warning("[%s] 以下榜单抓取为空: %s", query_id, failed_lists)

    # 状态 + 注解（与 /run 保持一致）
    hot_lists_status: Dict[str, str] = {}
    for source_name in ("东方财富", "雪球", "同花顺", "全市场活跃"):
        if source_name in failed_lists:
            hot_lists_status[source_name] = "failed"
        elif source_name == "同花顺":
            hot_lists_status[source_name] = "fallback"
        else:
            hot_lists_status[source_name] = "ok"

    hot_lists_notes: Dict[str, str] = {
        "东方财富": (
            "主榜 ak.stock_hot_rank_em；如返回空/失败，降级到「全市场活跃榜」（按换手率排序）"
        ),
        "同花顺": (
            "用 Playwright 渲染同花顺排行榜（持续放量/持续高/新上榜）；"
            "失败时回退到 akshare stock_hot_deal_xq（雪球 deals）"
        ),
        "全市场活跃": (
            "akshare stock_zh_a_spot（5526+ 只 A 股）按换手率排序；"
            "是真正独立的第三视角（资金面活跃度）"
        ),
    }

    non_empty_lists = {k: v for k, v in lists.items() if v}
    summary["used_lists_count"] = len(non_empty_lists)

    if len(non_empty_lists) < 2:
        return {
            "error": (
                f"可用榜单不足（{failed_lists}为空），至少需要 2 份榜单有数据。"
                f" 当前非空: {list(non_empty_lists.keys()) or '无'}"
            ),
            "summary": summary,
            "failed_lists": failed_lists,
            "hot_lists_status": hot_lists_status,
            "hot_lists_notes": hot_lists_notes,
        }

    logger.info(
        "[%s] 有效榜单 %d 个: %s",
        query_id, len(non_empty_lists), list(non_empty_lists.keys()),
    )

    raw_intersection = compute_intersection(non_empty_lists)
    filtered_intersection = filter_a_share(
        raw_intersection, exclude_kechuang=req.exclude_kechuang
    )
    target_stocks = filtered_intersection[: req.max_intersection]

    return {
        "summary": summary,
        "failed_lists": failed_lists,
        "non_empty_lists": non_empty_lists,
        "hot_lists_status": hot_lists_status,
        "hot_lists_notes": hot_lists_notes,
        "raw_intersection": raw_intersection,
        "filtered_intersection": filtered_intersection,
        "target_stocks": target_stocks,
        "raw_intersection_count": len(raw_intersection),
        "filtered_intersection_count": len(filtered_intersection),
        "target_count": len(target_stocks),
    }


def _estimate_seconds(target_count: int, rounds: int, inter_round_sleep: float, max_concurrency: int = 1) -> int:
    """
    粗略预估 LLM 分析总耗时（秒）。

    假设条件（基于本项目历史实测）:
      - 单只股票单轮分析平均耗时 ~140 秒（Agent 模式：一次 analyze_stock 内部
        包含 4-9 步 LLM 调用 + 数据获取 + 搜索；2026-08 Round 47 实测 44 个样本
        平均 110.3s/次 Agent 调用，加数据准备后约 143s/轮，取保守值 140）
      - 轮间 sleep 0.6 秒（默认）
      - 同一只股票每轮分析串行
      - 多只股票并发跑：max_concurrency=1 时全串行；>1 时多只同时分析，
        总耗时近似除以并发数（Round 16 实现的信号量限流模型）

    公式: ceil(target_count / max_concurrency) * (rounds * LLM_AVG_SEC + (rounds-1) * inter_round_sleep)
    """
    # ★ Round 48 修复：LLM_AVG_SEC 从 50 → 140。旧值只假设"单次 LLM 调用"，
    #   但 analyze_stock 实际是 Agent 多步流程（4-9 步），Round 47 实测
    #   预估 756s（12.6 分钟）实际 4282s（71 分钟），偏差 5.7 倍。
    #   新值贴近实测（10 只 × 3 轮 × 140s ≈ 4212s ≈ 实际 4282s）。
    LLM_AVG_SEC = 140
    if target_count <= 0 or rounds <= 0:
        return 0
    # ★ 本轮修复(M4)：并发摊薄总耗时，max_concurrency=1 等价旧行为（向后兼容）
    conc = max(1, int(max_concurrency or 1))
    per_stock = rounds * LLM_AVG_SEC + max(0, rounds - 1) * inter_round_sleep
    # 用 ceil 向上取整，避免并发刚好整除时低估
    import math
    batches = math.ceil(target_count / conc)
    return int(batches * per_stock)


def _format_human_seconds(total_sec: int) -> str:
    """把秒数转为人类可读字符串（"约 8 分钟" / "约 30 秒"）"""
    if total_sec < 60:
        return f"约 {total_sec} 秒"
    if total_sec < 3600:
        minutes = total_sec / 60.0
        return f"约 {minutes:.1f} 分钟"
    hours = total_sec / 3600.0
    return f"约 {hours:.1f} 小时"


# ---------------------------------------------------------------------------
# 阶段 1 端点：预览（仅抓榜 + 计算交集，不调 LLM）
# ---------------------------------------------------------------------------
@router.post("/preview", response_model=HotAnalysisPreviewResponse)
async def preview_hot_analysis(req: HotAnalysisPreviewRequest) -> HotAnalysisPreviewResponse:
    """
    阶段 1 端点：仅抓三榜 + 计算交集 + 预估耗时，**不调用任何 LLM**。

    用于前端在用户点击"开始分析"前先展示：
      - 抓取条数
      - 实际进入 LLM 分析的目标股票数
      - 预估总耗时（人类可读）

    让用户在确认是否值得花 N 分钟跑分析。
    """
    started_at_dt = datetime.now()
    started_at_iso = started_at_dt.isoformat()
    query_id = f"preview_{uuid.uuid4().hex[:8]}"

    # 复用 RunRequest（字段子集等价），让 helper 用统一接口
    run_req_like = HotAnalysisRunRequest(
        hot_list_n=req.hot_list_n,
        max_intersection=req.max_intersection,
        exclude_kechuang=req.exclude_kechuang,
        rounds=req.rounds,
        inter_round_sleep=req.inter_round_sleep,
        simple_report=req.simple_report,
        llm_timeout_seconds=req.llm_timeout_seconds,
        min_score=req.min_score,
        allowed_advice=req.allowed_advice,
        exclude_keywords=req.exclude_keywords,
        require_all_rounds=req.require_all_rounds,
        # ★ 本轮修复(M4)：max_concurrency 透传到 RunRequest，
        #   让 _compute_intersection_stage 内的下游逻辑与前端预览一致
        max_concurrency=req.max_concurrency,
    )

    info = await asyncio.to_thread(_compute_intersection_stage, run_req_like, query_id)
    finished_at_dt = datetime.now()

    if "error" in info:
        return HotAnalysisPreviewResponse(
            status="failed",
            started_at=started_at_iso,
            finished_at=finished_at_dt.isoformat(),
            duration_seconds=round((finished_at_dt - started_at_dt).total_seconds(), 2),
            hot_lists_summary=info.get("summary", {}),
            hot_lists_status=info.get("hot_lists_status", {}),
            hot_lists_notes=info.get("hot_lists_notes", {}),
            error=info["error"],
        )

    target_count = info["target_count"]
    # ★ 本轮修复(M4)：把 max_concurrency 纳入预估，否则前端选 4 并发时仍按串行估算 → 失真
    estimated = _estimate_seconds(
        target_count, req.rounds, req.inter_round_sleep, req.max_concurrency,
    )

    logger.info(
        "[%s] preview: target=%d filtered=%d raw=%d estimated=%ds",
        query_id, target_count, info["filtered_intersection_count"],
        info["raw_intersection_count"], estimated,
    )

    # 构建交集股票列表（代码+名称+来源）
    intersection_stocks = []
    for stock in info.get("target_stocks", []):
        intersection_stocks.append({
            "code": stock.get("code", ""),
            "name": stock.get("name", stock.get("code", "")),
            "in_sources": stock.get("in_sources", []),
        })

    return HotAnalysisPreviewResponse(
        status="ok",
        started_at=started_at_iso,
        finished_at=finished_at_dt.isoformat(),
        duration_seconds=round((finished_at_dt - started_at_dt).total_seconds(), 2),
        hot_lists_summary=info["summary"],
        hot_lists_status=info["hot_lists_status"],
        hot_lists_notes=info["hot_lists_notes"],
        raw_intersection_count=info["raw_intersection_count"],
        filtered_intersection_count=info["filtered_intersection_count"],
        target_count=target_count,
        estimated_seconds=estimated,
        estimated_human=_format_human_seconds(estimated),
        rounds=req.rounds,
        intersection_stocks=intersection_stocks,
    )


# ---------------------------------------------------------------------------
# 主端点
# ---------------------------------------------------------------------------
@router.post("/run", response_model=HotAnalysisRunResponse)
async def run_hot_analysis(req: HotAnalysisRunRequest) -> HotAnalysisRunResponse:
    """
    触发三榜交集 + 三轮分析 + 过滤 全流程。

    注：这是一个**长任务**，单次调用可能耗时 1-180 分钟（取决于 LLM 响应速度）。
    建议前端先调用 /preview 预览，确认后再触发 /run。
    """
    started_at_dt = datetime.now()
    started_at_iso = started_at_dt.isoformat()
    query_id = f"hot_{uuid.uuid4().hex[:8]}"
    logger.info(
        "[%s] 三榜分析开始 hot_list_n=%d max_intersection=%d rounds=%d",
        query_id, req.hot_list_n, req.max_intersection, req.rounds,
    )

    # ------------------------------------------------------------------
    # Steps 1-2: 抓取榜单 + 求交集 + 过滤（复用 _compute_intersection_stage）
    # ------------------------------------------------------------------
    info = await asyncio.to_thread(_compute_intersection_stage, req, query_id)

    if "error" in info:
        return HotAnalysisRunResponse(
            status="failed",
            started_at=started_at_iso,
            finished_at=datetime.now().isoformat(),
            duration_seconds=round((datetime.now() - started_at_dt).total_seconds(), 2),
            hot_lists_summary=info.get("summary", {}),
            hot_lists_status=info.get("hot_lists_status", {}),
            hot_lists_notes=info.get("hot_lists_notes", {}),
            error=info["error"],
            error_code="INTERSECTION_FAILED",
        )

    summary = info["summary"]
    hot_lists_status = info["hot_lists_status"]
    hot_lists_notes = info["hot_lists_notes"]
    raw_intersection = info["raw_intersection"]
    filtered_intersection = info["filtered_intersection"]
    target_stocks = info["target_stocks"]

    logger.info(
        "[%s] 交集: raw=%d filtered=%d target=%d",
        query_id, len(raw_intersection), len(filtered_intersection), len(target_stocks),
    )

    if not target_stocks:
        return HotAnalysisRunResponse(
            status="ok",
            started_at=started_at_iso,
            finished_at=datetime.now().isoformat(),
            duration_seconds=round((datetime.now() - started_at_dt).total_seconds(), 2),
            hot_lists_summary=summary,
            hot_lists_status=hot_lists_status,
            hot_lists_notes=hot_lists_notes,
            raw_intersection_count=len(raw_intersection),
            filtered_intersection_count=len(filtered_intersection),
            analyzed_stock_count=0,
            passed_stock_count=0,
            results=[],
        )

    # ------------------------------------------------------------------
    # Step 3+4: 初始化 Pipeline 并执行分析（CC 设置在整个分析期间保持生效）
    # ------------------------------------------------------------------
    try:
        from src.config import get_config
        from src.core.pipeline import StockAnalysisPipeline
        from src.enums import ReportType

        # _cc_env_override 必须覆盖整个分析流程（不仅是初始化），
        # 因为 LLM 调用发生在 analyze_stock() 内部，需要 CC 设置持续生效。
        with _cc_env_override():
            config = get_config()
            logger.info(
                "[%s] Pipeline 使用模型: %s",
                query_id,
                config.litellm_model or "(由 .env 默认)",
            )
            pipeline = StockAnalysisPipeline(
                config=config,
                max_workers=1,  # 每只股票内部已串行 3 轮；stock 间保持串行避免 LLM 限流
                query_id=query_id,
                query_source="hot-analysis",
            )
            report_type = ReportType.SIMPLE if req.simple_report else ReportType.FULL

            # ------------------------------------------------------------------
            # Step 4: 串行分析（每只股票内 3 轮）—— 在 CC 上下文内执行
            # ------------------------------------------------------------------
            deadline_ts = started_at_dt.timestamp() + req.max_runtime_seconds
            results: List[HotAnalysisStockResult] = []
            filtered_stocks: List[FilteredStockItem] = []
            analyzed_stock_count = 0
            timed_out = False
            # ★ Round 16：并发分析股票数（默认 1 串行；2-3 提速，更高并发可能触发 LLM 限流）
            max_conc = max(1, min(getattr(req, "max_concurrency", 1) or 1, 8))
            # ★ Round 16：分析并发用可降级限流器（LLM 失败过多 → 降级到串行）
            sem = DynamicAsyncLimiter(max_conc)
            llm_fail_count = 0
            # ★ Round 16：数据源并发独立于 LLM 并发，上限 min(max_conc, 4)，失败自动降级
            try:
                pipeline.fetcher_manager.set_data_concurrency(min(max_conc, MAX_DATA_FETCH_CONCURRENCY))
            except Exception as _de:
                logger.warning("[%s] 设置数据源并发失败: %s", query_id, _de)

            async def analyze_one(stock: Dict[str, Any]) -> None:
                """分析单只股票（★ Round 16：并发安全，asyncio 单线程协作，共享累加器不跨 await 变更）。"""
                nonlocal analyzed_stock_count, timed_out, llm_fail_count
                async with sem:
                    if time.time() > deadline_ts:
                        timed_out = True
                        return

                    code = stock["code"]
                    name = stock.get("name") or code
                    analyses_raw: List[Dict[str, Any]] = []

                    # ★ per-round LLM 超时：由请求参数控制（避免远端 socket 卡死导致整个流程永远等）
                    llm_timeout = req.llm_timeout_seconds

                    # ★ 重新设计的轮询逻辑：
                    #   - 最多尝试 MAX_ANALYSIS_ATTEMPTS 次
                    #   - 累积到 MIN_SUCCESS_REQUIRED 次"有意义"成功就提前终止
                    #   - 6 次都不够 3 次有意义成功 → 数据不充足 → 过滤掉
                    MIN_SUCCESS_REQUIRED = req.rounds  # 字段语义：从"刚好 N 轮"变为"至少 N 轮有数据"

                    attempt_idx = 0
                    success_count = 0
                    while attempt_idx < MAX_ANALYSIS_ATTEMPTS:
                        attempt_idx += 1
                        if time.time() > deadline_ts:
                            timed_out = True
                            break
                        if success_count >= MIN_SUCCESS_REQUIRED:
                            # 已收集到足够有意义成功，提前进入过滤判定
                            break
                        round_started = datetime.now()
                        try:
                            one = await asyncio.wait_for(
                                asyncio.to_thread(
                                    _run_one_analysis_sync,
                                    pipeline=pipeline,
                                    code=code,
                                    report_type=report_type,
                                    query_id=query_id,
                                    round_idx=attempt_idx,
                                    round_started_at=round_started,
                                ),
                                timeout=llm_timeout,
                            )
                        except asyncio.TimeoutError:
                            logger.error(
                                "[%s] %s(%s) 第 %d 次 LLM 调用超时（>%ds），标记为失败",
                                query_id, name, code, attempt_idx, int(llm_timeout),
                            )
                            one = {
                                "round": attempt_idx,
                                "sentiment_score": None,
                                "operation_advice": None,
                                "decision_type": None,
                                "trend_prediction": None,
                                "model_used": None,
                                "timestamp": round_started.isoformat(),
                                "error": f"LLM_TIMEOUT ({int(llm_timeout)}s)",
                            }
                            # 不 break，继续下一次尝试
                        analyses_raw.append(one)
                        if _is_meaningful_round(one):
                            success_count += 1
                            llm_fail_count = 0
                            logger.info(
                                "[%s] %s(%s) 第 %d 次成功（%d/%d）",
                                query_id, name, code, attempt_idx, success_count, MIN_SUCCESS_REQUIRED,
                            )
                        else:
                            # ★ Round 16：LLM 连续失败 → 分析并发降一级（直至串行）
                            llm_fail_count += 1
                            if llm_fail_count >= LLM_FAILURE_DEGRADE_THRESHOLD:
                                if await sem.degrade():
                                    logger.warning(
                                        "[%s] LLM 连续失败 %d 次，分析并发降为 %d",
                                        query_id, LLM_FAILURE_DEGRADE_THRESHOLD, sem.limit,
                                    )
                                llm_fail_count = 0
                        # 轮间间隔（最后一次尝试后不 sleep）
                        if (
                            attempt_idx < MAX_ANALYSIS_ATTEMPTS
                            and success_count < MIN_SUCCESS_REQUIRED
                            and req.inter_round_sleep > 0
                        ):
                            await asyncio.sleep(req.inter_round_sleep)

                    # ★ 修复：每只股票只 +1（按"是否有任何非 error 轮"算）
                    if any(not a.get("error") for a in analyses_raw):
                        analyzed_stock_count += 1

                    # ★ 数据不充足淘汰：6 次尝试后 < 3 次有意义成功
                    if success_count < MIN_SUCCESS_REQUIRED:
                        logger.info(
                            "[%s] %s(%s) 数据不充足，过滤掉（%d/%d 次有意义成功，%d 次尝试）",
                            query_id, name, code,
                            success_count, MIN_SUCCESS_REQUIRED, attempt_idx,
                        )
                        # 计算平均分用于展示
                        valid_a = [a for a in analyses_raw if not a.get("error")]
                        scs = [a.get("sentiment_score", 0) for a in valid_a if a.get("sentiment_score") is not None]
                        advs = [str(a.get("operation_advice") or "") for a in valid_a]
                        filtered_stocks.append(FilteredStockItem(
                            code=code, name=name,
                            in_sources=stock.get("in_sources", []),
                            filter_reason="incomplete",
                            avg_score=round(sum(scs) / len(scs), 2) if scs else 0.0,
                            final_advice=_majority_advice(advs),
                            analyses=[AnalysisRunItem(**a) for a in analyses_raw],
                        ))
                        return  # 跳过这只股票，不进入 _check_pass

                    pass_reason, passed = _check_pass(
                        analyses_raw,
                        min_score=req.min_score,
                        allowed_advice=req.allowed_advice,
                        exclude_keywords=req.exclude_keywords,
                        require_all_rounds=req.require_all_rounds,
                    )
                    if not passed:
                        logger.info("[%s] %s(%s) 过滤掉: %s", query_id, name, code, pass_reason)
                        valid_a = [a for a in analyses_raw if not a.get("error")]
                        scs = [a.get("sentiment_score", 0) for a in valid_a if a.get("sentiment_score") is not None]
                        advs = [str(a.get("operation_advice") or "") for a in valid_a]
                        filtered_stocks.append(FilteredStockItem(
                            code=code, name=name,
                            in_sources=stock.get("in_sources", []),
                            filter_reason=pass_reason,
                            avg_score=round(sum(scs) / len(scs), 2) if scs else 0.0,
                            final_advice=_majority_advice(advs),
                            analyses=[AnalysisRunItem(**a) for a in analyses_raw],
                        ))
                        return

                    # 聚合
                    valid_analyses = [a for a in analyses_raw if not a.get("error")]
                    scores = [a.get("sentiment_score", 0) for a in valid_analyses if a.get("sentiment_score") is not None]
                    advices = [str(a.get("operation_advice") or "") for a in valid_analyses]
                    avg_score = round(sum(scores) / len(scores), 2) if scores else 0.0
                    final_advice = _majority_advice(advices)

                    results.append(HotAnalysisStockResult(
                        code=code,
                        name=name,
                        in_sources=stock.get("in_sources", []),
                        analyses=[AnalysisRunItem(**a) for a in analyses_raw],
                        pass_reason=pass_reason,
                        avg_score=avg_score,
                        final_advice=final_advice,
                    ))

            await asyncio.gather(*[analyze_one(s) for s in target_stocks])

        finished_at = datetime.now()
        duration = (finished_at - started_at_dt).total_seconds()

        # 构建过滤配置快照
        filter_cfg = {
            "min_score": req.min_score,
            "allowed_advice": req.allowed_advice,
            "exclude_keywords": req.exclude_keywords,
            "require_all_rounds": req.require_all_rounds,
        }

        # 保存到历史数据库
        try:
            from update_version.db.history import save_analysis_run
            save_analysis_run(
                started_at=started_at_iso,
                finished_at=finished_at.isoformat(),
                duration_seconds=round(duration, 2),
                status="timeout" if timed_out else "ok",
                hot_lists_summary=summary,
                hot_lists_status=hot_lists_status,
                hot_lists_notes=hot_lists_notes,
                raw_intersection_count=len(raw_intersection),
                filtered_intersection_count=len(filtered_intersection),
                analyzed_stock_count=analyzed_stock_count,
                passed_stock_count=len(results),
                filter_config=filter_cfg,
                results=[r.model_dump() for r in results],
            )
        except Exception as db_err:
            logger.warning("[%s] 保存历史记录失败: %s", query_id, db_err)

        return HotAnalysisRunResponse(
            status="timeout" if timed_out else "ok",
            started_at=started_at_iso,
            finished_at=finished_at.isoformat(),
            duration_seconds=round(duration, 2),
            hot_lists_summary=summary,
            hot_lists_status=hot_lists_status,
            hot_lists_notes=hot_lists_notes,
            raw_intersection_count=len(raw_intersection),
            filtered_intersection_count=len(filtered_intersection),
            analyzed_stock_count=analyzed_stock_count,
            passed_stock_count=len(results),
            results=results,
            filtered_stocks=filtered_stocks,
            filter_config=filter_cfg,
        )

    except ImportError as e:
        logger.exception("[%s] Pipeline 依赖缺失", query_id)
        return HotAnalysisRunResponse(
            status="failed",
            started_at=started_at_iso,
            finished_at=datetime.now().isoformat(),
            duration_seconds=round((datetime.now() - started_at_dt).total_seconds(), 2),
            hot_lists_summary=summary,
            hot_lists_status=hot_lists_status,
            hot_lists_notes=hot_lists_notes,
            raw_intersection_count=info.get("raw_intersection_count", 0),
            filtered_intersection_count=info.get("filtered_intersection_count", 0),
            error=f"Pipeline 依赖缺失: {e}",
            error_code="PIPELINE_DEPENDENCY_MISSING",
        )
    except (ConnectionError, TimeoutError, OSError) as e:
        logger.exception("[%s] LLM 连接或超时失败", query_id)
        return HotAnalysisRunResponse(
            status="failed",
            started_at=started_at_iso,
            finished_at=datetime.now().isoformat(),
            duration_seconds=round((datetime.now() - started_at_dt).total_seconds(), 2),
            hot_lists_summary=summary,
            hot_lists_status=hot_lists_status,
            hot_lists_notes=hot_lists_notes,
            raw_intersection_count=info.get("raw_intersection_count", 0),
            filtered_intersection_count=info.get("filtered_intersection_count", 0),
            error=f"LLM 连接或超时失败: {e}",
            error_code="LLM_CONNECTION_FAILED",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("[%s] Pipeline 初始化或分析失败", query_id)
        return HotAnalysisRunResponse(
            status="failed",
            started_at=started_at_iso,
            finished_at=datetime.now().isoformat(),
            duration_seconds=round((datetime.now() - started_at_dt).total_seconds(), 2),
            hot_lists_summary=summary,
            hot_lists_status=hot_lists_status,
            hot_lists_notes=hot_lists_notes,
            raw_intersection_count=info.get("raw_intersection_count", 0),
            filtered_intersection_count=info.get("filtered_intersection_count", 0),
            error=f"Pipeline 初始化或分析失败: {e}",
            error_code="PIPELINE_INIT_FAILED",
        )


# ---------------------------------------------------------------------------
# 健康检查（仅 hot-analysis 模块）
# ---------------------------------------------------------------------------
@router.get("/health")
async def health() -> Dict[str, Any]:
    """轻量健康检查：返回 akshare 是否可用"""
    try:
        import akshare as ak  # noqa: F401
        return {
            "status": "ok",
            "akshare_available": True,
            "module": "hot_analysis",
        }
    except Exception as e:  # noqa: BLE001
        return {
            "status": "degraded",
            "akshare_available": False,
            "error": str(e),
            "module": "hot_analysis",
        }


# ---------------------------------------------------------------------------
# Phase 1: 单股分析（不走四榜交集，直接分析指定股票）
# ---------------------------------------------------------------------------
def _fetch_stock_profile(code: str) -> Tuple[str, str]:
    """★ Round 26：best-effort 拉取股票真实名称 + 行业。

    1) akshare stock_individual_info_em（含 股票简称/行业，最准）；
    2) efinance get_belong_board（一次调用同时给 股票名称 + 板块名称，取一级行业）；
    3) 名称最后回退主项目数据源（tencent）。
    """
    name = ""
    industry = ""
    try:
        import akshare as ak
        df = ak.stock_individual_info_em(symbol=code)
        if df is not None and not df.empty and "item" in df.columns:
            info = dict(zip(df["item"], df["value"]))
            name = str(info.get("股票简称", "") or "")
            industry = str(info.get("行业", "") or "")
            if name and industry:
                return name.replace(" ", ""), industry
    except Exception:
        pass
    if not name or not industry:
        try:
            from data_provider.efinance_fetcher import EfinanceFetcher
            boards = EfinanceFetcher().get_belong_board(code)
            if boards is not None and not boards.empty:
                if not name and "股票名称" in boards.columns:
                    raw = boards.iloc[0].get("股票名称")
                    if raw:
                        name = str(raw).strip()
                if not industry and "板块名称" in boards.columns:
                    bnames = [str(v).strip() for v in boards["板块名称"].tolist() if v]
                    # 一级行业：不带 Ⅰ-Ⅴ 罗马后缀
                    base = next((n for n in bnames if not any(c in n for c in "ⅠⅡⅢⅣⅤ")), None)
                    if not base:
                        base = bnames[0] if bnames else ""
                    industry = base
        except Exception:
            pass
    if not name:
        try:
            from data_provider.base import DataFetcherManager
            q = DataFetcherManager().get_realtime_quote(code)
            if q is not None and getattr(q, "name", None):
                name = str(q.name).strip()
        except Exception:
            pass
    if name:
        name = name.replace(" ", "")
    return name, industry


def _fetch_stock_price_change(code: str) -> Tuple[Optional[float], Optional[float]]:
    """★ Round 27：best-effort 拉取最新股价 + 涨跌幅。

    优先 akshare stock_bid_ask_em；回退主项目数据源 DataFetcherManager（tencent/efinance，
    本环境可靠）。都失败返回 (None, None)。
    """
    try:
        import akshare as ak
        df = ak.stock_bid_ask_em(symbol=code)
        if df is not None and not df.empty and "item" in df.columns:
            info = dict(zip(df["item"], df["value"]))
            price = float(info.get("最新价") or 0) or None
            chg = info.get("涨幅")
            change_pct = float(str(chg).replace("%", "")) if chg is not None else None
            if price:
                return price, change_pct
    except Exception:
        pass
    try:
        from data_provider.base import DataFetcherManager
        q = DataFetcherManager().get_realtime_quote(code)
        if q is not None:
            price = getattr(q, "price", None)
            change_pct = getattr(q, "change_pct", None)
            if price is not None:
                return float(price), (float(change_pct) if change_pct is not None else None)
    except Exception:
        pass
    return None, None


def _build_single_stock_summary(
    *,
    name: str, industry: str, price: Optional[float], change_pct: Optional[float],
    avg_score: float, final_advice: str, trend: Optional[str], valid_rounds: int,
) -> str:
    """★ Round 26：生成可读的汇总操作建议（确定性规则，不额外调 LLM）。

    综合评分、建议、趋势，给出用户"怎么操作"的指导。
    """
    lines: List[str] = []
    head = name or "该股票"
    if industry:
        head += f"（{industry}）"
    if price:
        head += f" 现价 ¥{price:.2f}"
        if change_pct is not None:
            head += f"（{change_pct:+.2f}%）"
    lines.append(head)

    if avg_score >= 80:
        score_txt = "强势，市场情绪积极"
    elif avg_score >= 60:
        score_txt = "中性偏多"
    elif avg_score >= 40:
        score_txt = "偏弱，需谨慎"
    else:
        score_txt = "弱势，风险较高"
    lines.append(
        f"多轮 LLM 分析（{valid_rounds} 次有效）综合评分 {avg_score:.1f} 分（{score_txt}），"
        f"综合建议：{final_advice or '—'}。"
    )

    advice_map = {
        "买入": "可考虑分批建仓，但注意控制仓位、预留回调缓冲。",
        "加仓": "已持有者可逢低适当加仓；未持有者建议等待回调再介入。",
        "持有": "可继续持有观望，重点跟踪成交与基本面变化。",
        "观望": "暂不急于操作，等待更明确的趋势或事件信号。",
        "卖出": "建议考虑减仓或离场，锁定收益、控制风险。",
        "减仓": "建议逐步减仓、降低仓位，防范下行风险。",
    }
    matched = next((v for k, v in advice_map.items() if k in (final_advice or "")), None)
    if matched:
        lines.append(f"操作建议：{matched}")
    else:
        lines.append("操作建议：控制仓位、审慎操作，关注个股基本面与大盘节奏。")
    if trend:
        lines.append(f"趋势参考：{trend}")
    lines.append("（以上基于多轮情绪与趋势分析，不构成投资建议。）")
    return "\n".join(lines)


@router.post("/analyze-single", response_model=SingleStockResponse)
async def analyze_single_stock(req: SingleStockRequest) -> SingleStockResponse:
    """
    单股分析端点：直接对指定股票进行 N 轮 LLM 分析。

    不经过四榜交集流程，适合用户手动指定股票代码/名称。
    """
    query_id = f"single_{uuid.uuid4().hex[:8]}"
    raw_input = req.code.strip()
    code = raw_input
    logger.info("[%s] 单股分析开始 code=%s rounds=%d", query_id, code, req.rounds)

    # UI 占位符允许"代码或名称"输入（见 SingleStockInput.vue），所以必须先解析为合法代码
    # 否则会把"潍柴动力"当代码传给 analyze_stock，导致三轮全部 LLM_TIMEOUT
    # resolve_name_to_code 内部可能触发 AkShare 在线调用（冷启动 ~2-5s），所以加 3s 上限
    try:
        resolved_code = await asyncio.wait_for(
            asyncio.to_thread(_resolve_code_input, code),
            timeout=_NAME_RESOLVE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning("[%s] 单股分析 name→code 解析超时 (>%ss) code=%s",
                       query_id, _NAME_RESOLVE_TIMEOUT_SECONDS, code)
        resolved_code = None

    if not resolved_code:
        logger.warning("[%s] 单股分析无法解析输入 code=%s", query_id, code)
        return SingleStockResponse(
            status="failed",
            code=raw_input,
            name=raw_input,
            error=(
                f"无法识别股票代码或名称: {raw_input!r}。"
                "请使用形如 000338 / 600519 的代码，或准确的股票中文名称。"
            ),
            error_code="INVALID_INPUT_CODE_OR_NAME",
        )
    # 仅当输入不是合法代码（即从名称解析得到）时才记录 name→code 映射
    from src.services.stock_code_utils import is_code_like as _icl
    if not _icl(code) and resolved_code != code:
        logger.info("[%s] 单股分析 name→code 解析: %r -> %s", query_id, code, resolved_code)
    code = resolved_code
    # 解析后保留原始输入，便于响应里告诉用户"你输入了什么 + 解析到了什么"
    display_name = raw_input if raw_input != resolved_code else code

    try:
        from src.config import get_config
        from src.core.pipeline import StockAnalysisPipeline
        from src.enums import ReportType

        with _cc_env_override():
            config = get_config()
            pipeline = StockAnalysisPipeline(
                config=config,
                max_workers=1,
                query_id=query_id,
                query_source="single-analysis",
            )
            report_type = ReportType.SIMPLE if req.simple_report else ReportType.FULL

            analyses_raw: List[Dict[str, Any]] = []
            # ★ round 6 audit：与 /run / /run-async / scheduled_task 对齐——最多重试
            #   MAX_ANALYSIS_ATTEMPTS 次，累积到 req.rounds 次有意义成功即提前终止
            #   此前直接 `for round_idx in range(1, req.rounds+1)` 失败就放弃，单股路径比批量
            #   路径更脆弱；与 /run 的 attempt/success 行为对齐
            attempt_idx = 0
            success_count = 0
            while attempt_idx < MAX_ANALYSIS_ATTEMPTS:
                attempt_idx += 1
                round_idx = len(analyses_raw) + 1
                round_started = datetime.now()
                try:
                    one = await asyncio.wait_for(
                        asyncio.to_thread(
                            _run_one_analysis_sync,
                            pipeline=pipeline,
                            code=code,
                            report_type=report_type,
                            query_id=query_id,
                            round_idx=round_idx,
                            round_started_at=round_started,
                        ),
                        timeout=req.llm_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    one = {
                        "round": round_idx,
                        "sentiment_score": None,
                        "operation_advice": None,
                        "decision_type": None,
                        "trend_prediction": None,
                        "model_used": None,
                        "timestamp": round_started.isoformat(),
                        "error": f"LLM_TIMEOUT ({int(req.llm_timeout_seconds)}s)",
                    }
                analyses_raw.append(one)
                if not one.get("error"):
                    success_count += 1
                # 提前终止：成功轮数已达需求
                if success_count >= req.rounds:
                    break
                # 轮间间隔（最后一次尝试后不 sleep）
                if (
                    attempt_idx < MAX_ANALYSIS_ATTEMPTS
                    and req.inter_round_sleep > 0
                ):
                    await asyncio.sleep(req.inter_round_sleep)

            # 聚合
            valid_analyses = [a for a in analyses_raw if not a.get("error")]
            scores = [a.get("sentiment_score", 0) for a in valid_analyses if a.get("sentiment_score") is not None]
            advices = [str(a.get("operation_advice") or "") for a in valid_analyses]
            avg_score = round(sum(scores) / len(scores), 2) if scores else 0.0
            final_advice = _majority_advice(advices)
            pass_reason, _ = _check_pass(analyses_raw, min_score=60.0)

            # ★ Round 26：真实名称 + 行业 + 价格（best-effort），汇总操作建议
            real_name, industry = _fetch_stock_profile(code)
            # 用户输入若是名称（如"贵州茅台"）且已解析出真实名，用真实名；否则用输入
            name = real_name or display_name
            # ★ Round 27：最近股价 + 最新涨跌幅（akshare + DataFetcherManager 双回退）
            price, change_pct = _fetch_stock_price_change(code)
            trend = None
            for a in reversed(analyses_raw):
                if a.get("trend_prediction"):
                    trend = a["trend_prediction"]
                    break
            summary = _build_single_stock_summary(
                name=name, industry=industry, price=price, change_pct=change_pct,
                avg_score=avg_score, final_advice=final_advice, trend=trend,
                valid_rounds=len(valid_analyses),
            )

            return SingleStockResponse(
                status="ok",
                code=code,
                name=name,
                analyses=[AnalysisRunItem(**a) for a in analyses_raw],
                avg_score=avg_score,
                final_advice=final_advice,
                pass_reason=pass_reason,
                industry=industry,
                summary=summary,
                # ★ Round 27：独立返回最近股价 + 最新涨跌幅（前端单独展示）
                price=price,
                change_pct=change_pct,
            )

    except Exception as e:
        logger.exception("[%s] 单股分析失败 code=%s", query_id, code)
        return SingleStockResponse(
            status="failed",
            code=code,
            name=display_name,
            error=f"{type(e).__name__}: {e}",
        )


# ---------------------------------------------------------------------------
# Phase 2: 后台异步任务 + SSE 进度推送
# ---------------------------------------------------------------------------
@router.post("/run-async")
async def run_async_analysis(req: HotAnalysisRunRequest) -> Dict[str, Any]:
    """
    启动后台分析任务（非阻塞），返回 task_id。

    前端可通过 /run/{task_id}/stream 监听 SSE 进度推送。
    """
    task_id = _generate_task_id()
    _cleanup_old_tasks()

    async with _task_lock:
        _tasks[task_id] = {
            "status": "running",
            "phase": "fetching",
            "total_stocks": 0,
            "completed_stocks": 0,
            "current_stock": "",
            "current_round": 0,
            "passed_count": 0,
            "error": None,
            "started_at": datetime.now().isoformat(),
            "elapsed_seconds": 0.0,
            "progress_queue": asyncio.Queue(maxsize=_PROGRESS_QUEUE_MAXSIZE),
            "result": None,
            # ★ Round 18：即时取消——取消时 set，后台循环 / 每轮 LLM 立即响应
            "cancel_event": asyncio.Event(),
            "_created_ts": time.time(),
        }

    # 启动后台任务
    asyncio.create_task(_run_analysis_background(task_id, req))

    return {
        "task_id": task_id,
        "status": "started",
        "message": "后台分析任务已启动",
    }


# ★ Round 16：可动态降级的 asyncio 并发限流器（LLM 失败过多时降级到串行）
class DynamicAsyncLimiter:
    """并发上限可运行中下调的 asyncio 限流器。

    用于分析（LLM）并发：当连续失败轮次超过阈值，调用 degrade() 降一级并发，
    直到串行（minimum=1）。支持 async with。
    """

    def __init__(self, initial: int, minimum: int = 1) -> None:
        self._limit = max(1, int(initial))
        self._minimum = max(1, int(minimum))
        self._count = 0
        self._cond = asyncio.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    async def acquire(self) -> None:
        async with self._cond:
            while self._count >= self._limit:
                await self._cond.wait()
            self._count += 1

    async def release(self) -> None:
        async with self._cond:
            self._count = max(0, self._count - 1)
            self._cond.notify()

    async def degrade(self) -> bool:
        """降一级并发；返回是否真的降了（已到 minimum 则返回 False）。"""
        async with self._cond:
            if self._limit > self._minimum:
                self._limit -= 1
                self._cond.notify_all()
                return True
            return False

    async def __aenter__(self):
        await self.acquire()

    async def __aexit__(self, *args):
        await self.release()


# ★ Round 16：LLM 连续失败达到该阈值 → 分析并发降一级（数据源并发由数据层节流器独立降级）
LLM_FAILURE_DEGRADE_THRESHOLD = 3
# ★ Round 16：数据源并发上限（分析并发的一个子集，数据源更脆弱）
MAX_DATA_FETCH_CONCURRENCY = 4


async def _run_analysis_background(task_id: str, req: HotAnalysisRunRequest) -> None:
    """后台执行分析任务，通过 progress_queue 推送进度。"""
    task = _tasks.get(task_id)
    if not task:
        return

    queue: asyncio.Queue = task["progress_queue"]
    started_at_dt = datetime.now()

    try:
        # Step 1: 抓榜
        _safe_queue_put(queue, {"type": "progress", "data": {
            "task_id": task_id, "status": "running", "phase": "fetching",
            "total_stocks": 0, "completed_stocks": 0, "current_stock": "",
            "current_round": 0, "passed_count": 0, "started_at": task["started_at"],
            "elapsed_seconds": 0.0,
        }})

        query_id = f"async_{uuid.uuid4().hex[:8]}"
        info = await asyncio.to_thread(_compute_intersection_stage, req, query_id)

        # 抓榜后立即检查取消，避免无谓启动 LLM
        if _task_cancelled(task):
            logger.info("[%s] 抓榜阶段已取消", task_id)
            finished_at = datetime.now()
            duration = (finished_at - started_at_dt).total_seconds()
            result_data = {
                "status": "cancelled",
                "started_at": task["started_at"],
                "finished_at": finished_at.isoformat(),
                "duration_seconds": round(duration, 2),
                "hot_lists_summary": info.get("summary", {}),
                "hot_lists_status": info.get("hot_lists_status", {}),
                "hot_lists_notes": info.get("hot_lists_notes", {}),
                "raw_intersection_count": len(info.get("raw_intersection", [])),
                "filtered_intersection_count": len(info.get("filtered_intersection", [])),
                "analyzed_stock_count": 0,
                "passed_stock_count": 0,
                "results": [],
                "filtered_stocks": [],
                "filter_config": {
                    "min_score": req.min_score, "allowed_advice": req.allowed_advice,
                    "exclude_keywords": req.exclude_keywords,
                    "require_all_rounds": req.require_all_rounds,
                },
            }
            try:
                from update_version.db.history import save_analysis_run
                save_analysis_run(
                    started_at=task["started_at"],
                    finished_at=finished_at.isoformat(),
                    duration_seconds=round(duration, 2),
                    status="cancelled",
                    hot_lists_summary=result_data["hot_lists_summary"],
                    hot_lists_status=result_data["hot_lists_status"],
                    hot_lists_notes=result_data["hot_lists_notes"],
                    raw_intersection_count=result_data["raw_intersection_count"],
                    filtered_intersection_count=result_data["filtered_intersection_count"],
                    analyzed_stock_count=0,
                    passed_stock_count=0,
                    filter_config=result_data["filter_config"],
                    results=[],
                )
            except Exception as db_err:
                logger.warning("[%s] 保存历史记录失败: %s", task_id, db_err)
            task["status"] = "cancelled"
            task["phase"] = "completed"
            task["result"] = result_data
            _safe_queue_put(queue, {"type": "partial", "data": result_data}, task_id=task_id)
            _safe_queue_put(queue, {"type": "done"}, task_id=task_id)
            return

        if "error" in info:
            task["status"] = "failed"
            task["error"] = info["error"]
            _safe_queue_put(queue, {"type": "error", "data": {"error": info["error"]}}, task_id=task_id)
            _safe_queue_put(queue, {"type": "done"}, task_id=task_id)
            return

        target_stocks = info["target_stocks"]
        task["total_stocks"] = len(target_stocks)
        summary = info["summary"]
        hot_lists_status = info["hot_lists_status"]
        hot_lists_notes = info["hot_lists_notes"]
        raw_intersection = info["raw_intersection"]
        filtered_intersection = info["filtered_intersection"]

        if not target_stocks:
            task["status"] = "completed"
            task["phase"] = "completed"
            result_data = {
                "status": "ok", "started_at": task["started_at"],
                "finished_at": datetime.now().isoformat(),
                "duration_seconds": round((datetime.now() - started_at_dt).total_seconds(), 2),
                "hot_lists_summary": summary, "hot_lists_status": hot_lists_status,
                "hot_lists_notes": hot_lists_notes,
                "raw_intersection_count": len(raw_intersection),
                "filtered_intersection_count": len(filtered_intersection),
                "analyzed_stock_count": 0, "passed_stock_count": 0, "results": [],
                "filtered_stocks": [],
            }
            task["result"] = result_data
            _safe_queue_put(queue, {"type": "complete", "data": result_data}, task_id=task_id)
            _safe_queue_put(queue, {"type": "done"}, task_id=task_id)
            return

        # Step 2: 初始化 Pipeline
        from src.config import get_config
        from src.core.pipeline import StockAnalysisPipeline
        from src.enums import ReportType

        with _cc_env_override():
            config = get_config()
            pipeline = StockAnalysisPipeline(
                config=config, max_workers=1,
                query_id=query_id, query_source="hot-analysis-async",
            )
            report_type = ReportType.SIMPLE if req.simple_report else ReportType.FULL

            # Step 3: 逐只分析（★ Round 16：支持 max_concurrency 并发）
            deadline_ts = started_at_dt.timestamp() + req.max_runtime_seconds
            results: List[HotAnalysisStockResult] = []
            # ★ Round 22 修复(M8)：async 路径此前不收集 filtered_stocks，导致"分析但未通过"的
            #   股票在结果/历史里丢失（同步路径有 filtered_stocks）。这里对齐同步路径。
            filtered_stocks: List[Dict[str, Any]] = []
            analyzed_stock_count = 0
            timed_out = False
            cancelled = False
            total_stocks = len(target_stocks)
            max_conc = max(1, min(getattr(req, "max_concurrency", 1) or 1, 8))
            # ★ Round 16：分析并发用可降级限流器（LLM 失败过多 → 降级到串行）
            sem = DynamicAsyncLimiter(max_conc)
            llm_fail_count = 0
            # ★ Round 16：数据源并发独立于 LLM 并发，上限 min(max_conc, 4)，失败自动降级
            try:
                pipeline.fetcher_manager.set_data_concurrency(min(max_conc, MAX_DATA_FETCH_CONCURRENCY))
            except Exception as _de:
                logger.warning("[%s] 设置数据源并发失败: %s", task_id, _de)

            def _emit_progress(current_stock: str, current_round: int) -> None:
                elapsed = round((datetime.now() - started_at_dt).total_seconds(), 1)
                _safe_queue_put(queue, {"type": "progress", "data": {
                    "task_id": task_id, "status": "running", "phase": "analyzing",
                    "total_stocks": total_stocks,
                    "completed_stocks": task["completed_stocks"],
                    "current_stock": current_stock,
                    "current_round": current_round, "passed_count": task["passed_count"],
                    "started_at": task["started_at"], "elapsed_seconds": elapsed,
                }})

            async def analyze_one(stock: Dict[str, Any]) -> None:
                """分析单只股票（并发安全：asyncio 单线程协作，共享累加器不跨 await 变更）。"""
                nonlocal analyzed_stock_count, timed_out, cancelled, llm_fail_count
                async with sem:
                    # 响应前端取消 / 达到 deadline：直接返回，已收集 results 后续推 partial 事件
                    if _task_cancelled(task):
                        cancelled = True
                        return
                    if time.time() > deadline_ts:
                        timed_out = True
                        return

                    code = stock["code"]
                    name = stock.get("name") or code
                    cur_label = f"{name}({code})"
                    task["current_stock"] = cur_label
                    task["phase"] = "analyzing"
                    _emit_progress(cur_label, 0)

                    analyses_raw: List[Dict[str, Any]] = []
                    MIN_SUCCESS_REQUIRED = req.rounds
                    attempt_idx = 0
                    success_count = 0

                    while attempt_idx < MAX_ANALYSIS_ATTEMPTS:
                        attempt_idx += 1
                        # ★ Round 18：每轮顶部检查取消，取消后立即放弃后续轮次
                        if _task_cancelled(task):
                            cancelled = True
                            break
                        if time.time() > deadline_ts:
                            break
                        if success_count >= MIN_SUCCESS_REQUIRED:
                            break
                        round_started = datetime.now()
                        # ★ Round 18：LLM 调用与取消事件竞速——用户点取消立即放弃当前轮，
                        #   不必等当前 to_thread（最长 llm_timeout_seconds）跑完。
                        _cancel_event = task.get("cancel_event")
                        _cancel_waiter_coro = (
                            _cancel_event.wait() if _cancel_event is not None
                            else asyncio.sleep(3600)  # 兼容异常/测试路径：永不取消
                        )
                        llm_task = asyncio.ensure_future(
                            asyncio.to_thread(
                                _run_one_analysis_sync, pipeline=pipeline, code=code,
                                report_type=report_type, query_id=query_id,
                                round_idx=attempt_idx, round_started_at=round_started,
                            )
                        )
                        cancel_waiter = asyncio.ensure_future(_cancel_waiter_coro)
                        _done, _pending = await asyncio.wait(
                            {llm_task, cancel_waiter},
                            timeout=req.llm_timeout_seconds,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if cancel_waiter in _done:
                            # 取消：立即放弃当前轮，不再等 LLM（线程仍在后台跑，但协程已返回）
                            llm_task.cancel()
                            cancelled = True
                            break
                        cancel_waiter.cancel()
                        if llm_task not in _done:
                            # 超时（timeout 到期但 LLM 未返回）
                            one = {
                                "round": attempt_idx, "sentiment_score": None,
                                "operation_advice": None, "decision_type": None,
                                "trend_prediction": None, "model_used": None,
                                "timestamp": round_started.isoformat(),
                                "error": f"LLM_TIMEOUT ({int(req.llm_timeout_seconds)}s)",
                            }
                        else:
                            try:
                                one = llm_task.result()
                            except Exception as _le:  # noqa: BLE001
                                one = {
                                    "round": attempt_idx, "sentiment_score": None,
                                    "operation_advice": None, "decision_type": None,
                                    "trend_prediction": None, "model_used": None,
                                    "timestamp": round_started.isoformat(),
                                    "error": f"{type(_le).__name__}: {_le}",
                                }
                        analyses_raw.append(one)
                        if _is_meaningful_round(one):
                            success_count += 1
                            llm_fail_count = 0
                        else:
                            # ★ Round 16：LLM 连续失败 → 分析并发降一级（直至串行）
                            llm_fail_count += 1
                            if llm_fail_count >= LLM_FAILURE_DEGRADE_THRESHOLD:
                                if await sem.degrade():
                                    logger.warning(
                                        "[%s] LLM 连续失败 %d 次，分析并发降为 %d",
                                        task_id, LLM_FAILURE_DEGRADE_THRESHOLD, sem.limit,
                                    )
                                llm_fail_count = 0
                        task["current_round"] = attempt_idx
                        _emit_progress(cur_label, attempt_idx)
                        if attempt_idx < MAX_ANALYSIS_ATTEMPTS and success_count < MIN_SUCCESS_REQUIRED and req.inter_round_sleep > 0:
                            await asyncio.sleep(req.inter_round_sleep)

                    task["completed_stocks"] += 1
                    if any(not a.get("error") for a in analyses_raw):
                        analyzed_stock_count += 1
                    _emit_progress(cur_label, task["current_round"])

                    if success_count < MIN_SUCCESS_REQUIRED:
                        # ★ Round 22 修复(M8)：数据不充足淘汰 → 收集进 filtered_stocks
                        _valid_a = [a for a in analyses_raw if not a.get("error")]
                        _scs = [a.get("sentiment_score", 0) for a in _valid_a if a.get("sentiment_score") is not None]
                        _advs = [str(a.get("operation_advice") or "") for a in _valid_a]
                        filtered_stocks.append({
                            "code": code, "name": name,
                            "in_sources": stock.get("in_sources", []),
                            "filter_reason": "incomplete",
                            "avg_score": round(sum(_scs) / len(_scs), 2) if _scs else 0.0,
                            "final_advice": _majority_advice(_advs),
                            "analyses": [dict(a) for a in analyses_raw],
                        })
                        return

                    pass_reason, passed = _check_pass(
                        analyses_raw, min_score=req.min_score,
                        allowed_advice=req.allowed_advice, exclude_keywords=req.exclude_keywords,
                        require_all_rounds=req.require_all_rounds,
                    )
                    if not passed:
                        # ★ Round 22 修复(M8)：未通过过滤 → 收集进 filtered_stocks
                        _valid_a = [a for a in analyses_raw if not a.get("error")]
                        _scs = [a.get("sentiment_score", 0) for a in _valid_a if a.get("sentiment_score") is not None]
                        _advs = [str(a.get("operation_advice") or "") for a in _valid_a]
                        filtered_stocks.append({
                            "code": code, "name": name,
                            "in_sources": stock.get("in_sources", []),
                            "filter_reason": pass_reason,
                            "avg_score": round(sum(_scs) / len(_scs), 2) if _scs else 0.0,
                            "final_advice": _majority_advice(_advs),
                            "analyses": [dict(a) for a in analyses_raw],
                        })
                        return

                    valid_analyses = [a for a in analyses_raw if not a.get("error")]
                    scores = [a.get("sentiment_score", 0) for a in valid_analyses if a.get("sentiment_score") is not None]
                    advices = [str(a.get("operation_advice") or "") for a in valid_analyses]
                    avg_score = round(sum(scores) / len(scores), 2) if scores else 0.0
                    final_advice = _majority_advice(advices)

                    results.append(HotAnalysisStockResult(
                        code=code, name=name,
                        in_sources=stock.get("in_sources", []),
                        analyses=[AnalysisRunItem(**a) for a in analyses_raw],
                        pass_reason=pass_reason, avg_score=avg_score, final_advice=final_advice,
                    ))
                    task["passed_count"] = len(results)

            await asyncio.gather(*[analyze_one(s) for s in target_stocks])

            # 完成 / 取消：构造结果 + 落库 + 推送
            finished_at = datetime.now()
            duration = (finished_at - started_at_dt).total_seconds()
            filter_cfg = {
                "min_score": req.min_score, "allowed_advice": req.allowed_advice,
                "exclude_keywords": req.exclude_keywords, "require_all_rounds": req.require_all_rounds,
            }

            final_status = (
                "cancelled" if cancelled
                else "timeout" if timed_out
                else "ok"
            )
            result_data = {
                "status": final_status, "started_at": task["started_at"],
                "finished_at": finished_at.isoformat(),
                "duration_seconds": round(duration, 2),
                "hot_lists_summary": summary, "hot_lists_status": hot_lists_status,
                "hot_lists_notes": hot_lists_notes,
                "raw_intersection_count": len(raw_intersection),
                "filtered_intersection_count": len(filtered_intersection),
                "analyzed_stock_count": analyzed_stock_count,
                "passed_stock_count": len(results),
                "results": [r.model_dump() for r in results],
                # ★ Round 22 修复(M8)：async 结果补齐 filtered_stocks（"未通过过滤"卡片此前永不显示）
                "filtered_stocks": filtered_stocks,
                "filter_config": filter_cfg,
            }

            # 保存到历史数据库（含 cancelled / partial 状态）
            try:
                from update_version.db.history import save_analysis_run
                save_analysis_run(
                    started_at=task["started_at"], finished_at=finished_at.isoformat(),
                    duration_seconds=round(duration, 2), status=final_status,
                    hot_lists_summary=summary, hot_lists_status=hot_lists_status,
                    hot_lists_notes=hot_lists_notes,
                    raw_intersection_count=len(raw_intersection),
                    filtered_intersection_count=len(filtered_intersection),
                    analyzed_stock_count=analyzed_stock_count,
                    passed_stock_count=len(results), filter_config=filter_cfg,
                    results=[r.model_dump() for r in results],
                )
            except Exception as db_err:
                logger.warning("[%s] 保存历史记录失败: %s", task_id, db_err)

            task["status"] = final_status
            task["phase"] = "completed"
            task["result"] = result_data
            # 取消 / 超时：推 partial 事件，前端用来“展示已完成部分”；
            # 完成：推 complete 事件，前端用来“全量展示”。
            event_type = "complete" if final_status == "ok" else "partial"
            _safe_queue_put(queue, {"type": event_type, "data": result_data}, task_id=task_id)
            _safe_queue_put(queue, {"type": "done"}, task_id=task_id)

    except Exception as e:
        logger.exception("[%s] 后台分析任务失败", task_id)
        task["status"] = "failed"
        task["error"] = str(e)
        # ★ 修复：异常路径也要把 phase 置为 completed；否则 SSE 监听者会无限等
        task["phase"] = "completed"
        task["result"] = {"status": "failed", "error": str(e)}
        _safe_queue_put(queue, {"type": "error", "data": {"error": str(e)}}, task_id=task_id)
        _safe_queue_put(queue, {"type": "done"}, task_id=task_id)


@router.post("/run/{task_id}/cancel")
async def cancel_analysis_task(task_id: str) -> Dict[str, Any]:
    """取消后台分析任务

    仅标记 task["status"] = "cancelled"。
    不在 handler 中 push done 事件，避免打断后端 _run_analysis_background
    即将发出的 partial 事件。前端 SSE 监听 partial 后再收到 done。
    """
    async with _task_lock:
        task = _tasks.get(task_id)
        if not task:
            return {"status": "not_found", "message": f"任务 {task_id} 不存在"}
        task["status"] = "cancelled"
        # ★ Round 18：触发 cancel_event，让正在跑的 analyze_one 立即放弃当前轮
        _cancel_event = task.get("cancel_event")
        if _cancel_event is not None:
            _cancel_event.set()
    logger.info("[%s] 任务已被前端取消", task_id)
    return {"status": "ok", "message": "任务已取消"}


@router.get("/run/{task_id}/stream")
async def stream_analysis_progress(task_id: str):
    """
    SSE 端点：实时推送分析进度。

    事件类型：
    - progress: 进度更新（包含 current_stock, completed_stocks 等）
    - complete: 分析完成（包含完整结果）
    - error: 分析失败
    """
    task = _tasks.get(task_id)
    if not task:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    queue: asyncio.Queue = task["progress_queue"]

    async def event_generator():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                except asyncio.TimeoutError:
                    # 心跳（25s：避免被 corporate proxy idle-timeout 杀掉 EventSource）
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue

                if event.get("type") == "done":
                    break

                event_type = event.get("type", "progress")
                data = json.dumps(event.get("data", {}), ensure_ascii=False)
                yield f"event: {event_type}\ndata: {data}\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Phase 2: 历史记录 CRUD
# ---------------------------------------------------------------------------
@router.get("/history", response_model=AnalysisHistoryResponse)
async def get_history(page: int = 1, page_size: int = 20) -> AnalysisHistoryResponse:
    """查询分析历史记录（分页）。"""
    try:
        from update_version.db.history import get_analysis_history
        items, total = get_analysis_history(page=page, page_size=page_size)
        return AnalysisHistoryResponse(total=total, page=page, page_size=page_size, items=items)
    except Exception as e:
        logger.exception("查询历史记录失败")
        return AnalysisHistoryResponse(total=0, page=page, page_size=page_size, items=[])


@router.get("/history/{run_id}")
async def get_history_run(run_id: int) -> Dict[str, Any]:
    """查询单次历史记录详情。"""
    from fastapi import HTTPException
    try:
        from update_version.db.history import get_analysis_run
        item = get_analysis_run(run_id)
        if not item:
            raise HTTPException(status_code=404, detail=f"记录 {run_id} 不存在")
        return item
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("查询历史记录失败 run_id=%d", run_id)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/history/{run_id}")
async def delete_history_run(run_id: int) -> Dict[str, Any]:
    """删除历史记录。"""
    from fastapi import HTTPException
    try:
        from update_version.db.history import delete_analysis_run
        deleted = delete_analysis_run(run_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"记录 {run_id} 不存在")
        return {"status": "ok", "message": f"记录 {run_id} 已删除"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("删除历史记录失败 run_id=%d", run_id)
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Phase 3: 对比分析
# ---------------------------------------------------------------------------
@router.post("/compare", response_model=CompareResult)
async def compare_analysis_runs(req: CompareRequest) -> CompareResult:
    """对比两次分析结果。"""
    try:
        from update_version.db.history import get_analysis_run

        run1 = get_analysis_run(req.run_id_1)
        run2 = get_analysis_run(req.run_id_2)

        if not run1:
            return CompareResult(
                run_1=AnalysisHistoryItem(id=0, started_at="", finished_at="", duration_seconds=0, status="not_found"),
                run_2=AnalysisHistoryItem(id=0, started_at="", finished_at="", duration_seconds=0, status="not_found"),
                error=f"记录 {req.run_id_1} 不存在",
            )
        if not run2:
            return CompareResult(
                run_1=AnalysisHistoryItem(id=0, started_at="", finished_at="", duration_seconds=0, status="not_found"),
                run_2=AnalysisHistoryItem(id=0, started_at="", finished_at="", duration_seconds=0, status="not_found"),
                error=f"记录 {req.run_id_2} 不存在",
            )

        # 计算差异
        codes1 = {r["code"] for r in run1.get("results", [])}
        codes2 = {r["code"] for r in run2.get("results", [])}

        new_stocks = sorted(codes2 - codes1)
        removed_stocks = sorted(codes1 - codes2)

        # 评分变化
        results1_map = {r["code"]: r for r in run1.get("results", [])}
        results2_map = {r["code"]: r for r in run2.get("results", [])}

        score_changes = []
        for code in sorted(codes1 & codes2):
            r1 = results1_map[code]
            r2 = results2_map[code]
            if r1.get("avg_score") != r2.get("avg_score") or r1.get("final_advice") != r2.get("final_advice"):
                score_changes.append({
                    "code": code,
                    "name": r2.get("name", code),
                    "old_score": r1.get("avg_score", 0),
                    "new_score": r2.get("avg_score", 0),
                    "old_advice": r1.get("final_advice", ""),
                    "new_advice": r2.get("final_advice", ""),
                })

        return CompareResult(
            run_1=AnalysisHistoryItem(**run1),
            run_2=AnalysisHistoryItem(**run2),
            new_stocks=new_stocks,
            removed_stocks=removed_stocks,
            score_changes=score_changes,
        )

    except Exception as e:
        logger.exception("对比分析失败")
        return CompareResult(
            run_1=AnalysisHistoryItem(id=0, started_at="", finished_at="", duration_seconds=0, status="error"),
            run_2=AnalysisHistoryItem(id=0, started_at="", finished_at="", duration_seconds=0, status="error"),
            error=str(e),
        )


# ============================================================
# 自选股跟踪
# ============================================================
@router.get("/watchlist", response_model=WatchlistResponse)
async def get_watchlist() -> WatchlistResponse:
    """获取自选股列表"""
    try:
        from update_version.db.history import get_watchlist as _get_wl
        items = _get_wl()
        return WatchlistResponse(items=[WatchlistItem(**i) for i in items])
    except Exception as e:
        logger.exception("获取自选股失败")
        return WatchlistResponse()


@router.post("/watchlist")
async def add_watchlist(req: WatchlistRequest) -> Dict[str, Any]:
    """添加自选股"""
    try:
        from update_version.db.history import add_to_watchlist
        add_to_watchlist(req.code.strip(), req.name.strip(), req.notes.strip())
        return {"status": "ok", "message": f"{req.code} 已加入自选"}
    except Exception as e:
        logger.exception("添加自选股失败")
        return {"error": str(e)}


@router.delete("/watchlist/{code}")
async def remove_watchlist(code: str) -> Dict[str, Any]:
    """删除自选股"""
    try:
        from update_version.db.history import remove_from_watchlist
        removed = remove_from_watchlist(code)
        if removed:
            return {"status": "ok", "message": f"{code} 已移除自选"}
        # ★ round 5 audit：not-found 必须 404，不返回 200-with-error
        raise HTTPException(status_code=404, detail=f"{code} 不在自选中")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("删除自选股失败")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 分析模板
# ============================================================
@router.get("/templates", response_model=TemplateListResponse)
async def get_templates() -> TemplateListResponse:
    """获取分析模板列表"""
    try:
        from update_version.db.history import get_templates as _get_tpl
        items = _get_tpl()
        return TemplateListResponse(items=[AnalysisTemplate(**i) for i in items])
    except Exception as e:
        logger.exception("获取模板失败")
        return TemplateListResponse()


@router.post("/templates")
async def save_template(req: TemplateRequest) -> Dict[str, Any]:
    """保存分析模板"""
    try:
        from update_version.db.history import save_template as _save_tpl
        tid = _save_tpl(req.name, req.config, req.description, req.is_default)
        return {"status": "ok", "id": tid}
    except Exception as e:
        logger.exception("保存模板失败")
        return {"error": str(e)}


@router.delete("/templates/{template_id}")
async def delete_template(template_id: int) -> Dict[str, Any]:
    """删除分析模板"""
    try:
        from update_version.db.history import delete_template as _del_tpl
        deleted = _del_tpl(template_id)
        if deleted:
            return {"status": "ok"}
        # ★ round 5 audit：not-found 必须 404
        raise HTTPException(status_code=404, detail="模板不存在")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("删除模板失败")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# 定时任务
# ============================================================
@router.get("/scheduled-tasks", response_model=ScheduledTaskListResponse)
async def get_scheduled_tasks() -> ScheduledTaskListResponse:
    """获取定时任务列表"""
    try:
        from update_version.db.history import get_scheduled_tasks as _get_tasks
        items = _get_tasks()
        return ScheduledTaskListResponse(items=[ScheduledTask(**i) for i in items])
    except Exception as e:
        logger.exception("获取定时任务失败")
        return ScheduledTaskListResponse()


@router.post("/scheduled-tasks")
async def save_scheduled_task(req: ScheduledTaskRequest) -> Dict[str, Any]:
    """创建/更新定时任务"""
    # ★ 修复：在保存时校验 cron 表达式，避免无效字符串落库
    cron_error = _validate_cron_expr(req.cron_expr)
    if cron_error:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"cron 表达式无效: {cron_error}")
    try:
        from update_version.db.history import save_scheduled_task as _save_task
        tid = _save_task(req.name, req.cron_expr, req.config, req.enabled, req.task_id)
        return {"status": "ok", "id": tid}
    except Exception as e:
        logger.exception("保存定时任务失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/scheduled-tasks/{task_id}")
async def delete_scheduled_task(task_id: int) -> Dict[str, Any]:
    """删除定时任务"""
    try:
        from update_version.db.history import delete_scheduled_task as _del_task
        deleted = _del_task(task_id)
        if deleted:
            return {"status": "ok"}
        # ★ round 5 audit：not-found 必须 404
        raise HTTPException(status_code=404, detail="任务不存在")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("删除定时任务失败")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/scheduled-tasks/{task_id}/run")
async def run_scheduled_task(task_id: int) -> Dict[str, Any]:
    """手动触发定时任务执行"""
    try:
        from update_version.db.history import get_scheduled_task, update_task_run
        task = get_scheduled_task(task_id)
        if not task:
            return {"error": "任务不存在"}

        # 用任务配置执行分析
        config = task.get("config", {})
        req = HotAnalysisRunRequest(**config)
        # 异步执行
        asyncio.create_task(_execute_scheduled_task(task_id, req))
        return {"status": "ok", "message": "任务已触发执行"}
    except Exception as e:
        logger.exception("触发定时任务失败")
        return {"error": str(e)}


async def _execute_scheduled_task(task_id: int, req: HotAnalysisRunRequest) -> None:
    """执行定时任务：完整运行分析流程并保存结果"""
    try:
        from update_version.db.history import update_task_run, save_analysis_run
        query_id = f"scheduled_{task_id}_{uuid.uuid4().hex[:8]}"
        started_at_dt = datetime.now()

        # Step 1: 抓榜+交集
        info = await asyncio.to_thread(_compute_intersection_stage, req, query_id)
        if "error" in info:
            update_task_run(task_id, "failed")
            return

        target_stocks = info.get("target_stocks", [])
        if not target_stocks:
            update_task_run(task_id, "ok")
            return

        # Step 2: 初始化 Pipeline 并分析
        from src.config import get_config
        from src.core.pipeline import StockAnalysisPipeline
        from src.enums import ReportType

        with _cc_env_override():
            config = get_config()
            pipeline = StockAnalysisPipeline(
                config=config, max_workers=1,
                query_id=query_id, query_source="scheduled-task",
            )
            report_type = ReportType.SIMPLE if req.simple_report else ReportType.FULL
            deadline_ts = started_at_dt.timestamp() + req.max_runtime_seconds
            results = []
            analyzed_count = 0

            for stock in target_stocks[:req.max_intersection]:
                if time.time() > deadline_ts:
                    break
                code = stock["code"]
                name = stock.get("name") or code
                analyses_raw = []
                # ★ 修复（round 5 audit）：与 /run 路径对齐——最多 MAX_ANALYSIS_ATTEMPTS 次，
                #   累积 req.rounds 次"有意义"成功即提前终止；超时/失败重试
                attempt_idx = 0
                success_count = 0
                while attempt_idx < MAX_ANALYSIS_ATTEMPTS:
                    attempt_idx += 1
                    round_idx = len(analyses_raw) + 1
                    round_started = datetime.now()
                    try:
                        one = await asyncio.wait_for(
                            asyncio.to_thread(
                                _run_one_analysis_sync, pipeline=pipeline, code=code,
                                report_type=report_type, query_id=query_id,
                                round_idx=round_idx, round_started_at=round_started,
                            ), timeout=req.llm_timeout_seconds,
                        )
                    except asyncio.TimeoutError:
                        one = {"round": round_idx, "error": f"LLM_TIMEOUT ({int(req.llm_timeout_seconds)}s)"}
                    analyses_raw.append(one)
                    if not one.get("error"):
                        success_count += 1
                    # 提前终止：成功轮数已达需求
                    if success_count >= req.rounds:
                        break
                    # 轮间间隔（最后一次尝试后不 sleep）
                    if (
                        attempt_idx < MAX_ANALYSIS_ATTEMPTS
                        and req.inter_round_sleep > 0
                    ):
                        await asyncio.sleep(req.inter_round_sleep)

                if any(not a.get("error") for a in analyses_raw):
                    analyzed_count += 1

                pass_reason, passed = _check_pass(
                    analyses_raw, min_score=req.min_score,
                    allowed_advice=req.allowed_advice, exclude_keywords=req.exclude_keywords,
                    require_all_rounds=req.require_all_rounds,
                )
                if not passed:
                    continue

                valid = [a for a in analyses_raw if not a.get("error")]
                scores = [a.get("sentiment_score", 0) for a in valid if a.get("sentiment_score") is not None]
                advices = [str(a.get("operation_advice") or "") for a in valid]
                results.append(HotAnalysisStockResult(
                    code=code, name=name,
                    in_sources=stock.get("in_sources", []),
                    analyses=[AnalysisRunItem(**a) for a in analyses_raw],
                    pass_reason=pass_reason,
                    avg_score=round(sum(scores) / len(scores), 2) if scores else 0.0,
                    final_advice=_majority_advice(advices),
                ))

        finished_at = datetime.now()
        duration = (finished_at - started_at_dt).total_seconds()
        run_id = save_analysis_run(
            started_at=started_at_dt.isoformat(), finished_at=finished_at.isoformat(),
            duration_seconds=round(duration, 2), status="ok",
            hot_lists_summary=info["summary"], hot_lists_status=info["hot_lists_status"],
            hot_lists_notes=info["hot_lists_notes"],
            raw_intersection_count=info["raw_intersection_count"],
            filtered_intersection_count=info["filtered_intersection_count"],
            analyzed_stock_count=analyzed_count, passed_stock_count=len(results),
            filter_config={"min_score": req.min_score, "allowed_advice": req.allowed_advice,
                           "exclude_keywords": req.exclude_keywords, "require_all_rounds": req.require_all_rounds},
            results=[r.model_dump() for r in results],
        )
        update_task_run(task_id, "ok", result_id=run_id)
        logger.info("定时任务 %d 完成: run_id=%d, 通过 %d 只", task_id, run_id, len(results))

    except Exception as e:
        logger.exception("定时任务执行失败 task_id=%d", task_id)
        try:
            from update_version.db.history import update_task_run
            update_task_run(task_id, "failed")
        except Exception:
            pass


# ============================================================
# 行情数据
# ============================================================
@router.get("/market-data", response_model=MarketDataResponse)
async def get_market_data(codes: str = "") -> MarketDataResponse:
    """
    获取个股实时行情数据。

    codes: 逗号分隔的股票代码，如 "600519,000858,300750"

    上限：单次最多 50 个 code（避免长 codes 阻塞线程 + 防 DoS）。
    """
    if not codes:
        return MarketDataResponse()

    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if not code_list:
        return MarketDataResponse()

    # ★ round 6 audit：硬上限，避免恶意/误用触发全表扫描 + 14s akshare 拉取阻塞 event loop
    if len(code_list) > 50:
        raise HTTPException(
            status_code=400,
            detail=f"单次最多 50 个代码，实际 {len(code_list)} 个",
        )

    try:
        items = await asyncio.to_thread(_fetch_market_data, code_list)
        return MarketDataResponse(items=items, updated_at=datetime.now().isoformat())
    except Exception as e:
        # ★ round 8 audit：之前这里静默 return MarketDataResponse() 用户看不到错误
        # 现在日志写明（logger.exception）但仍返回 200+空，避免前端无数据时崩溃；
        # 前端已能优雅展示 "暂无数据"
        logger.exception("[market-data] 获取行情数据失败 codes=%s", code_list)
        return MarketDataResponse(items=[], updated_at=datetime.now().isoformat())


def _fetch_market_data(codes: List[str]) -> List[MarketDataItem]:
    """从 akshare 获取实时行情（向量化过滤）"""
    import akshare as ak

    # ★ round 8 audit：akshare stock_zh_a_spot() 偶发 RemoteDisconnected
    # 加指数退避重试（最多 3 次：1s, 2s, 4s 退避），避免静默返回空
    last_error: Optional[str] = None
    df = None
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_spot()
            if df is not None and not (hasattr(df, "empty") and df.empty):
                break
            last_error = "empty dataframe"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "[market-data] akshare stock_zh_a_spot 第 %d 次失败: %s",
                attempt + 1, last_error,
            )
            if attempt < 2:
                import time as _t
                _t.sleep(1.5 ** attempt)  # 1.5s, 3s
    if df is None or (hasattr(df, "empty") and df.empty):
        logger.warning("[market-data] akshare 3 次重试后仍为空，最后错误: %s", last_error)
        return []

    # ★ round 6 audit：使用 _find_col 容错 akshare 列名变化（之前 df["代码"] 直接索引
    #   若 akshare 改名则 KeyError；market_review_service 已有同名 helper）
    from update_version.services.market_review_service import _find_col
    col_code = _find_col(df, "代码", "code")
    if not col_code:
        logger.warning("[market-data] akshare 返回的 df 没有 '代码' 列：%s", list(df.columns)[:10])
        return []
    col_name = _find_col(df, "名称", "name") or col_code
    col_price = _find_col(df, "最新价", "current", "price")
    col_change_pct = _find_col(df, "涨跌幅", "percent", "change_pct")
    col_volume = _find_col(df, "成交量", "volume")
    col_amount = _find_col(df, "成交额", "amount", "turnover")
    col_high = _find_col(df, "最高", "high")
    col_low = _find_col(df, "最低", "low")
    col_open = _find_col(df, "今开", "open")
    col_market_cap = _find_col(df, "总市值", "market_cap")

    # df 中的代码形如 "sh600519" / "sz000001" / "bj920000"（带 sh/sz/bj 前缀），
    # 入参 codes 可能是 "600519" / "sh600519" 两种格式。
    # 统一映射：输入也补上前缀后与 df 的「代码」列做 isin。
    prefix_map = {
        "5": "sh", "6": "sh", "7": "sh",  # 沪市主板
        "0": "sz", "3": "sz", "1": "sz", "2": "sz",  # 深市主板/创业板
        "4": "bj", "8": "bj", "9": "bj",  # 北交所（含 920xxx 创新层等）
    }

    def _normalize(code: str) -> List[str]:
        c = code.strip()
        # 已带 sh./sz./bj. 前缀
        if c.lower().startswith(("sh", "sz", "bj")):
            return [c.lower()]
        # 纯数字
        if c.isdigit() and len(c) == 6:
            pfx = prefix_map.get(c[0])
            return [f"{pfx}{c}", c] if pfx else [c]
        return [c]

    code_set: set = set()
    for c in codes:
        code_set.update(_normalize(c))

    # 向量化过滤：先筛选再遍历，避免 O(n*m) 全表扫描
    mask = df[col_code].astype(str).str.lower().isin(code_set)
    matched = df.loc[mask]

    items = []
    for _, row in matched.iterrows():
        items.append(MarketDataItem(
            code=str(row.get(col_code, "")),
            name=str(row.get(col_name, "")),
            price=float(row.get(col_price, 0) or 0) if col_price else 0.0,
            change_pct=float(row.get(col_change_pct, 0) or 0) if col_change_pct else 0.0,
            volume=float(row.get(col_volume, 0) or 0) if col_volume else 0.0,
            turnover=float(row.get(col_amount, 0) or 0) if col_amount else 0.0,
            high=float(row.get(col_high, 0) or 0) if col_high else 0.0,
            low=float(row.get(col_low, 0) or 0) if col_low else 0.0,
            open=float(row.get(col_open, 0) or 0) if col_open else 0.0,
            market_cap=float(row.get(col_market_cap, 0) or 0) if col_market_cap else 0.0,
        ))
    return items


# ============================================================
# PDF 报告
# ============================================================
@router.post("/pdf-report")
async def generate_pdf_report(req: PDFReportRequest) -> Dict[str, Any]:
    """生成 PDF 分析报告"""
    try:
        from update_version.db.history import get_analysis_run, get_analysis_history

        if req.run_id:
            run = get_analysis_run(req.run_id)
        else:
            items, total = get_analysis_history(page=1, page_size=1)
            run = items[0] if items else None

        if not run:
            return {"error": "没有可用的分析记录"}

        pdf_bytes = await asyncio.to_thread(_generate_pdf, run)
        import base64
        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

        return {
            "status": "ok",
            "filename": f"hot-analysis-{run['id']}.pdf",
            "pdf_base64": pdf_b64,
            "size_bytes": len(pdf_bytes),
        }
    except ImportError:
        return {"error": "PDF 生成需要 reportlab 库，请运行: pip install reportlab"}
    except Exception as e:
        logger.exception("PDF 报告生成失败")
        return {"error": str(e)}


def _generate_pdf(run: Dict[str, Any]) -> bytes:
    """生成 PDF 字节流"""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from io import BytesIO

    # 注册中文字体
    font_paths = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    font_name = "Helvetica"
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                pdfmetrics.registerFont(TTFont("ChineseFont", fp))
                font_name = "ChineseFont"
                break
            except Exception:
                continue

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()

    # 自定义样式
    title_style = ParagraphStyle("TitleCN", parent=styles["Title"], fontName=font_name, fontSize=18, spaceAfter=12)
    normal_style = ParagraphStyle("NormalCN", parent=styles["Normal"], fontName=font_name, fontSize=10)
    small_style = ParagraphStyle("SmallCN", parent=styles["Normal"], fontName=font_name, fontSize=8, textColor=colors.grey)

    elements = []

    # 标题
    elements.append(Paragraph("四榜交集分析报告", title_style))
    elements.append(Spacer(1, 6*mm))

    # 摘要信息
    summary_text = f"""
    <b>分析时间:</b> {run.get('started_at', '')} ~ {run.get('finished_at', '')}<br/>
    <b>耗时:</b> {run.get('duration_seconds', 0):.1f} 秒<br/>
    <b>状态:</b> {run.get('status', '')}<br/>
    <b>四榜交集:</b> {run.get('raw_intersection_count', 0)} 只 (剔688后 {run.get('filtered_intersection_count', 0)} 只)<br/>
    <b>分析数:</b> {run.get('analyzed_stock_count', 0)} 只 | <b>通过数:</b> {run.get('passed_stock_count', 0)} 只
    """
    elements.append(Paragraph(summary_text, normal_style))
    elements.append(Spacer(1, 8*mm))

    # 结果表格
    results = run.get("results", [])
    if results:
        elements.append(Paragraph("<b>通过过滤的股票</b>", normal_style))
        elements.append(Spacer(1, 4*mm))

        table_data = [["代码", "名称", "均分", "建议", "命中榜", "通过原因"]]
        for r in results:
            table_data.append([
                r.get("code", ""),
                r.get("name", ""),
                f"{r.get('avg_score', 0):.1f}",
                r.get("final_advice", ""),
                "+".join(r.get("in_sources", [])),
                r.get("pass_reason", ""),
            ])

        table = Table(table_data, colWidths=[60, 80, 40, 50, 120, 60])
        table.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3b82f6")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(table)
    else:
        elements.append(Paragraph("本轮分析未发现符合条件的股票。", normal_style))

    elements.append(Spacer(1, 10*mm))
    elements.append(Paragraph(f"报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", small_style))

    doc.build(elements)
    return buf.getvalue()


# ============================================================
# Phase 4: 大盘复盘（每日报告）
# ============================================================
@router.post("/market-review/generate", response_model=MarketReviewItem)
async def generate_market_review(req: MarketReviewRequest) -> MarketReviewItem:
    """
    同步生成一份大盘复盘（akshare 抓指数 + 全市场宽度 + 板块聚合）。
    适合短任务；若 akshare 慢可能阻塞 5-15s。

    ★ round 9：进程内 5min 缓存（按 review_date 维度），防止用户双击或前端
      自动重试导致的重复抓 akshare。DB 已有的记录永远不被缓存覆盖。
    """
    from datetime import datetime as _dt
    from fastapi import HTTPException
    from update_version.services.market_review_service import generate_market_review

    target_date = req.review_date or _dt.now().strftime("%Y-%m-%d")
    cache_key = f"generate:{target_date}:{req.region}"
    now_ts = time.time()
    cached = _MARKET_REVIEW_GENERATE_CACHE.get(cache_key)
    if cached and (now_ts - cached["ts"]) < 300:  # 5min 缓存
        logger.info("[market-review/generate] 命中 5min 缓存 %s, age=%.1fs", cache_key, now_ts - cached["ts"])
        return _to_market_review_item(cached["payload"], review_date=target_date)

    try:
        payload = await asyncio.to_thread(
            generate_market_review,
            review_date=req.review_date,
            region=req.region,
            persist=req.persist,
        )
    except Exception as exc:
        logger.exception("大盘复盘生成失败")
        raise HTTPException(status_code=500, detail=str(exc))
    # 写入缓存（仅当 status=ok 或 degraded 都缓存；failed 不缓存以便重试）
    if payload.get("status") in ("ok", "degraded"):
        _MARKET_REVIEW_GENERATE_CACHE[cache_key] = {"ts": now_ts, "payload": payload}
    return _to_market_review_item(payload, review_date=target_date)


# round 9：5min TTL 进程内缓存（防止用户双击或前端自动轮询时重复抓 akshare）
_MARKET_REVIEW_GENERATE_CACHE: Dict[str, Dict[str, Any]] = {}


@router.get("/market-review/list", response_model=MarketReviewListResponse)
async def list_market_reviews(
    page: int = 1,
    page_size: int = 20,
    region: Optional[str] = None,
) -> MarketReviewListResponse:
    """分页获取大盘复盘记录（按 review_date DESC, id DESC）。"""
    from fastapi import HTTPException
    from update_version.db.history import get_market_review_list
    try:
        items, total = get_market_review_list(page=page, page_size=page_size, region=region)
    except Exception as exc:
        logger.exception("获取复盘列表失败")
        raise HTTPException(status_code=500, detail=str(exc))
    return MarketReviewListResponse(
        items=[_to_market_review_item(it) for it in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/market-review/latest", response_model=MarketReviewItem)
async def get_market_review_latest(region: Optional[str] = None) -> MarketReviewItem:
    """
    获取最近一次复盘记录。

    ★ round 8：DB 为空时若距离上次自动生成 >= 5min，则后台异步触发"今天"的复盘生成；
      同步响应仍 404（避免阻塞），但前端可观察到后台任务进行中后很快 /latest 就有数据。
      否则直接 404 提示用户主动 generate。
    """
    from fastapi import HTTPException
    from update_version.db.history import get_latest_market_review
    item = get_latest_market_review(region=region)
    if item:
        return _to_market_review_item(item)

    # 404 fallback: 节流式自动 generate
    now_ts = time.time()
    key = f"auto_gen:{region or 'cn'}"
    last = _MARKET_REVIEW_AUTO_GEN_TRIGGERED.get(key, 0.0)
    if now_ts - last >= 300:  # 5 分钟节流
        _MARKET_REVIEW_AUTO_GEN_TRIGGERED[key] = now_ts
        asyncio.create_task(
            _auto_generate_today_review(region=region or "cn"),
            name=f"auto_market_review_{int(now_ts)}",
        )
        raise HTTPException(
            status_code=404,
            detail=(
                "尚无大盘复盘记录，已自动触发今日复盘生成（首次约 15-25s，"
                "请稍后刷新页面）。后续 5 分钟内重复访问不再重复触发。"
            ),
        )
    raise HTTPException(status_code=404, detail="尚无大盘复盘记录，请先调用 /market-review/generate")


# round 8：/latest 404 时自动触发今日复盘（节流 5min）
_MARKET_REVIEW_AUTO_GEN_TRIGGERED: Dict[str, float] = {}


async def _auto_generate_today_review(*, region: str = "cn") -> None:
    """后台任务：自动生成今日大盘复盘（静默错误，不影响 404 主路径）。"""
    try:
        from update_version.services.market_review_service import generate_market_review
        payload = await asyncio.to_thread(
            generate_market_review,
            review_date=None,  # 默认今天
            region=region,
            persist=True,
        )
        logger.info(
            "[auto-market-review] 自动生成完成: status=%s summary=%s",
            payload.get("status"), payload.get("summary"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[auto-market-review] 自动生成失败: %s", exc)


# ============================================================
# round 6 新功能：导出 / 对比 / 批量生成（必须在 /{review_date} 之前注册，
#   否则 FastAPI 按声明顺序匹配，/compare 会被当成 review_date="compare"）
# ============================================================
@router.get("/market-review/compare")
async def compare_market_reviews(
    date_a: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="对比基期 YYYY-MM-DD"),
    date_b: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="对比目标期 YYYY-MM-DD"),
) -> Dict[str, Any]:
    """
    对比两个日期的大盘复盘：返回结构化 diff + 关键指标摘要。
    """
    from update_version.db.history import get_market_review
    from update_version.services.market_review_service import compare_two_reviews

    if date_a == date_b:
        raise HTTPException(status_code=400, detail="date_a 和 date_b 不能相同")
    a = get_market_review(date_a)
    b = get_market_review(date_b)
    if not a:
        raise HTTPException(status_code=404, detail=f"复盘记录 {date_a} 不存在")
    if not b:
        raise HTTPException(status_code=404, detail=f"复盘记录 {date_b} 不存在")
    return compare_two_reviews(a, b)


@router.post("/market-review/batch")
async def batch_generate_market_reviews(req: "MarketReviewBatchRequest") -> Dict[str, Any]:
    """
    批量生成最近 N 天的大盘复盘（默认 5 天，跳过今天和已有记录）。
    同步执行：每个复盘 15-25s，5 天最长 2 分钟；前端可调小 days 或自行分批。
    """
    from update_version.services.market_review_service import batch_generate_recent_reviews

    days = max(1, min(req.days, 30))  # 硬上限 30 天，避免滥用
    try:
        results = await asyncio.to_thread(
            batch_generate_recent_reviews,
            days=days, region=req.region, persist=req.persist,
        )
    except Exception as exc:
        logger.exception("批量生成复盘失败")
        raise HTTPException(status_code=500, detail=str(exc))
    return {
        "status": "ok",
        "requested_days": days,
        "completed": sum(1 for r in results if r["status"] in ("ok", "skipped")),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
    }


@router.get("/market-review/{review_date}", response_model=MarketReviewItem)
async def get_market_review_by_date(review_date: str) -> MarketReviewItem:
    """按 review_date (YYYY-MM-DD) 获取复盘记录。"""
    from fastapi import HTTPException
    from update_version.db.history import get_market_review
    item = get_market_review(review_date)
    if not item:
        raise HTTPException(status_code=404, detail=f"复盘记录 {review_date} 不存在")
    return _to_market_review_item(item)


@router.delete("/market-review/{review_date}")
async def delete_market_review_by_date(review_date: str) -> Dict[str, Any]:
    """按 review_date 删除一次复盘记录。"""
    from fastapi import HTTPException
    from update_version.db.history import delete_market_review
    deleted = delete_market_review(review_date)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"复盘记录 {review_date} 不存在")
    return {"status": "ok", "message": f"复盘记录 {review_date} 已删除"}


@router.get("/market-review/{review_date}/export", response_class=PlainTextResponse)
async def export_market_review(review_date: str) -> PlainTextResponse:
    """
    导出单次大盘复盘为 markdown 文件下载。
    Content-Disposition 让浏览器直接保存为 .md。
    """
    from fastapi.responses import PlainTextResponse as _PTR
    from update_version.db.history import get_market_review
    from update_version.services.market_review_service import export_review_as_markdown

    record = get_market_review(review_date)
    if not record:
        raise HTTPException(status_code=404, detail=f"复盘记录 {review_date} 不存在")
    md_text = export_review_as_markdown(record)
    return _PTR(
        content=md_text,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="market_review_{review_date}.md"',
        },
    )


def _generate_market_review_pdf(record: Dict[str, Any]) -> bytes:
    """
    round 8：大盘复盘 PDF 生成（参考 /pdf-report 的实现样式）。
    复用中文字体注册逻辑 + reportlab Table。
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from io import BytesIO

    # 注册中文字体（与 _generate_pdf 共享）
    font_paths = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    font_name = "Helvetica"
    for fp in font_paths:
        if os.path.exists(fp):
            try:
                pdfmetrics.registerFont(TTFont("ChineseFont", fp))
                font_name = "ChineseFont"
                break
            except Exception:
                continue

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleCN", parent=styles["Title"], fontName=font_name, fontSize=18, spaceAfter=12)
    normal_style = ParagraphStyle("NormalCN", parent=styles["Normal"], fontName=font_name, fontSize=10)
    small_style = ParagraphStyle("SmallCN", parent=styles["Normal"], fontName=font_name, fontSize=8, textColor=colors.grey)
    h2_style = ParagraphStyle("H2CN", parent=styles["Heading2"], fontName=font_name, fontSize=14, spaceBefore=8, spaceAfter=6)

    elements: List[Any] = []
    elements.append(Paragraph(f"大盘复盘报告 - {record.get('review_date', '?')}", title_style))
    elements.append(Spacer(1, 4 * mm))

    # 摘要
    signals = record.get("signals") or {}
    breadth = record.get("breadth") or {}
    summary_text = (
        f"<b>状态:</b> {record.get('status', '-')}<br/>"
        f"<b>盘面信号:</b> {signals.get('score', '-')}/100（{signals.get('tag', '-')}）<br/>"
        f"<b>操作建议:</b> {signals.get('advice', '-')}<br/>"
        f"<b>上涨/下跌/涨停/跌停:</b> {breadth.get('advance', 0)} / {breadth.get('decline', 0)} / {breadth.get('limit_up', 0)} / {breadth.get('limit_down', 0)}<br/>"
        f"<b>成交额:</b> {breadth.get('total_turnover_yi', 0)} 亿元<br/>"
        f"<b>耗时:</b> {record.get('duration_seconds', 0):.1f} 秒"
    )
    elements.append(Paragraph(summary_text, normal_style))
    elements.append(Spacer(1, 6 * mm))

    # 指数
    indices = record.get("indices") or {}
    if indices:
        elements.append(Paragraph("指数结构", h2_style))
        idx_rows = [["指数", "最新", "涨跌幅"]]
        for name in ("上证指数", "深证成指", "创业板指", "科创50", "上证50", "沪深300"):
            if name in indices:
                row = indices[name]
                idx_rows.append([
                    name,
                    f"{row.get('price', '-'):.2f}" if isinstance(row.get("price"), (int, float)) else str(row.get("price", "-")),
                    f"{row.get('change_pct', 0):+.2f}%" if isinstance(row.get("change_pct"), (int, float)) else "-",
                ])
        if len(idx_rows) > 1:
            t = Table(idx_rows, colWidths=[80, 80, 80])
            t.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), font_name),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3b82f6")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ]))
            elements.append(t)
            elements.append(Spacer(1, 6 * mm))

    # 行业板块
    sectors = record.get("sectors") or {}
    industries = sectors.get("industries") or {}
    if industries.get("top_gainers") or industries.get("top_losers"):
        elements.append(Paragraph("行业板块", h2_style))
        for label, rows, color in [
            ("领涨 Top 5", industries.get("top_gainers", []), colors.HexColor("#16a34a")),
            ("领跌 Top 5", industries.get("top_losers", []), colors.HexColor("#dc2626")),
        ]:
            if not rows:
                continue
            elements.append(Paragraph(f"<font color='{color.hexval()}'>{label}</font>", normal_style))
            table_data = [["行业", "涨跌幅"]]
            for s in rows:
                table_data.append([s.get("name", ""), f"{s.get('change_pct', 0):+.2f}%"])
            t = Table(table_data, colWidths=[120, 80])
            t.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), font_name),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
            ]))
            elements.append(t)
            elements.append(Spacer(1, 4 * mm))

    elements.append(Spacer(1, 8 * mm))
    elements.append(Paragraph(
        f"报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        small_style,
    ))
    doc.build(elements)
    return buf.getvalue()


@router.get("/market-review/{review_date}/pdf", response_class=Response)
async def export_market_review_pdf(review_date: str) -> Response:
    """
    round 8：大盘复盘 PDF 导出（复用 reportlab 与现有 _generate_pdf 风格）。
    客户端浏览器下载 `market_review_{date}.pdf`。
    """
    from update_version.db.history import get_market_review
    record = get_market_review(review_date)
    if not record:
        raise HTTPException(status_code=404, detail=f"复盘记录 {review_date} 不存在")
    try:
        pdf_bytes = _generate_market_review_pdf(record)
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="reportlab 未安装。请 `pip install reportlab` 后重试。",
        )
    except Exception as exc:
        logger.exception("[market-review-pdf] 生成失败: %s", exc)
        raise HTTPException(status_code=500, detail=f"PDF 生成失败: {exc}")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="market_review_{review_date}.pdf"',
        },
    )


# 动态定义的 request model（放在 file 底部避免循环引用）
class MarketReviewBatchRequest(BaseModel):  # type: ignore[no-redef]
    """批量生成大盘复盘请求"""
    days: int = Field(default=5, ge=1, le=30, description="回溯天数（含今天），上限 30")
    region: str = Field(default="cn", pattern="^(cn)$")
    persist: bool = Field(default=True)


def _to_market_review_item(
    payload: Dict[str, Any], review_date: Optional[str] = None
) -> MarketReviewItem:
    """将 DB/service 返回的 dict 规整为 MarketReviewItem（处理持久化后的字段映射）。"""
    breadth = payload.get("breadth") or payload.get("breadth_json") or {}
    indices = payload.get("indices") or payload.get("indices_json") or {}
    sectors = payload.get("sectors") or payload.get("sectors_json") or {}
    signals = payload.get("signals") or payload.get("signals_json") or {}
    if isinstance(breadth, str):
        try:
            breadth = json.loads(breadth)
        except Exception:
            breadth = {}
    if isinstance(indices, str):
        try:
            indices = json.loads(indices)
        except Exception:
            indices = {}
    if isinstance(sectors, str):
        try:
            sectors = json.loads(sectors)
        except Exception:
            sectors = {}
    if isinstance(signals, str):
        try:
            signals = json.loads(signals)
        except Exception:
            signals = {}

    # warnings: 兼容 service 内嵌的 warnings 列表与 DB error 字段
    warnings_raw = payload.get("warnings")
    if isinstance(warnings_raw, list):
        warnings_list = [str(w) for w in warnings_raw]
    elif isinstance(warnings_raw, str) and warnings_raw:
        warnings_list = [warnings_raw]
    else:
        warnings_list = []

    return MarketReviewItem(
        id=int(payload.get("id", 0) or 0),
        review_date=payload.get("review_date") or review_date or "",
        region=payload.get("region", "cn"),
        status=payload.get("status", "ok"),
        summary=payload.get("summary", ""),
        breadth=breadth if isinstance(breadth, dict) else {},
        indices=indices if isinstance(indices, dict) else {},
        sectors=sectors if isinstance(sectors, dict) else {},
        signals=signals if isinstance(signals, dict) else {},
        warnings=warnings_list,
        report_markdown=payload.get("report_markdown", ""),
        duration_seconds=float(payload.get("duration_seconds", 0.0) or 0.0),
        error=payload.get("error", "") or "",
        created_at=payload.get("created_at", "") or "",
        stocks_fetch_seconds=float(payload.get("stocks_fetch_seconds", 0.0) or 0.0),
    )


