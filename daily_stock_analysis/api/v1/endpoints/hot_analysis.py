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
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from api.v1.schemas.hot_analysis import (
    AnalysisRunItem,
    HotAnalysisPreviewRequest,
    HotAnalysisPreviewResponse,
    HotAnalysisRunRequest,
    HotAnalysisRunResponse,
    HotAnalysisStockResult,
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


# ---------------------------------------------------------------------------
# 过滤规则
# ---------------------------------------------------------------------------
# 任务原文：投资建议均为"买入"或"持有"（不允许出现"卖出"）
_SELL_KEYWORDS = ("卖出", "减仓", "SELL", "REDUCE")
_BUY_HOLD_KEYWORDS = ("买入", "加仓", "持有", "BUY", "HOLD", "ADD")


def _check_pass(
    analyses: List[Dict[str, Any]],
) -> tuple[str, bool]:
    """
    判断 N 轮分析是否满足保留条件。

    保留条件（任一即可）：
    - 全部有效轮 score ≥ 60
    - 全部有效轮 advice ∈ {买入/加仓/持有}（且无任何一轮出现"卖出/减仓"）

    Args:
        analyses: 单只股票的所有轮结果列表

    Returns:
        (pass_reason, passed)
        pass_reason ∈ {score / advice / score+advice / incomplete / rejected_sell / rejected_filter}
    """
    valid = [a for a in analyses if not a.get("error")]
    if not valid:
        return "incomplete", False

    scores = [a.get("sentiment_score") for a in valid if a.get("sentiment_score") is not None]
    advices = [str(a.get("operation_advice") or "") for a in valid]

    # 硬性：只要有任何一轮出现"卖出/减仓"语义，直接淘汰
    if any(any(k in a for k in _SELL_KEYWORDS) for a in advices):
        return "rejected_sell", False

    pass_score = bool(scores) and all(s >= 60 for s in scores)
    pass_advice = bool(advices) and all(
        any(k in a for k in _BUY_HOLD_KEYWORDS) for a in advices
    )

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


def _estimate_seconds(target_count: int, rounds: int, inter_round_sleep: float) -> int:
    """
    粗略预估 LLM 分析总耗时（秒）。

    假设条件（基于本项目历史实测）:
      - 单次 LLM 调用平均耗时 ~15 秒（Gemini/Claude 等，保守值）
      - 轮间 sleep 0.6 秒（默认）
      - 同一只股票每轮分析串行

    公式: target_count * (rounds * LLM_AVG_SEC + (rounds-1) * inter_round_sleep)
    """
    LLM_AVG_SEC = 15
    if target_count <= 0 or rounds <= 0:
        return 0
    per_stock = rounds * LLM_AVG_SEC + max(0, rounds - 1) * inter_round_sleep
    return int(target_count * per_stock)


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
    estimated = _estimate_seconds(target_count, req.rounds, req.inter_round_sleep)

    logger.info(
        "[%s] preview: target=%d filtered=%d raw=%d estimated=%ds",
        query_id, target_count, info["filtered_intersection_count"],
        info["raw_intersection_count"], estimated,
    )

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
    # Step 1: 抓取三榜
    # ------------------------------------------------------------------
    try:
        lists = await asyncio.to_thread(fetch_all_four, req.hot_list_n)
    except Exception as e:
        logger.exception("[%s] 抓取三榜异常", query_id)
        return HotAnalysisRunResponse(
            status="failed",
            started_at=started_at_iso,
            finished_at=datetime.now().isoformat(),
            duration_seconds=(datetime.now() - started_at_dt).total_seconds(),
            hot_lists_summary={},
            error=f"抓取三榜失败: {e}",
        )

    summary = {k: len(v) for k, v in lists.items()}
    failed_lists = [k for k, v in lists.items() if not v]
    if failed_lists:
        logger.warning("[%s] 以下榜单抓取为空: %s", query_id, failed_lists)

    # 为每个榜单计算 status（ok / failed / fallback）
    # 当前 akshare 不提供 stock_hot_ths_*d 等原生 THS 接口，
    # 所以"同花顺"目前 100% 走 stock_hot_deal_xq fallback（保留"同花顺"label 是为了 UI 契约稳定）
    hot_lists_status: Dict[str, str] = {}
    for source_name in ("东方财富", "雪球", "同花顺", "全市场活跃"):
        if source_name in failed_lists:
            hot_lists_status[source_name] = "failed"
        elif source_name == "同花顺":
            # 始终为 fallback（除非未来 akshare 复活原生 THS 接口）
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

    # 过滤掉空的榜单后再求交集（数学：空集 ∩ 任何 = 空集）
    # 同时报告前端"本次实际用了 N 榜"。
    non_empty_lists = {k: v for k, v in lists.items() if v}
    summary["used_lists_count"] = len(non_empty_lists)

    if len(non_empty_lists) < 2:
        # 至少需要 2 个非空榜单（不能空对空求交）
        return HotAnalysisRunResponse(
            status="failed",
            started_at=started_at_iso,
            finished_at=datetime.now().isoformat(),
            duration_seconds=(datetime.now() - started_at_dt).total_seconds(),
            hot_lists_summary=summary,
            hot_lists_status=hot_lists_status,
            hot_lists_notes=hot_lists_notes,
            error=(
                f"可用榜单不足（{failed_lists}为空），至少需要 2 份榜单有数据。"
                f" 当前非空: {list(non_empty_lists.keys()) or '无'}"
            ),
        )

    logger.info(
        "[%s] 有效榜单 %d 个: %s",
        query_id, len(non_empty_lists), list(non_empty_lists.keys()),
    )

    # ------------------------------------------------------------------
    # Step 2: 求交集 + 过滤（只对非空榜单求交集）
    # ------------------------------------------------------------------
    raw_intersection = compute_intersection(non_empty_lists)
    filtered_intersection = filter_a_share(
        raw_intersection, exclude_kechuang=req.exclude_kechuang
    )
    # 取 max_intersection 只
    target_stocks = filtered_intersection[: req.max_intersection]
    logger.info(
        "[%s] 交集: raw=%d filtered=%d target=%d",
        query_id, len(raw_intersection), len(filtered_intersection), len(target_stocks),
    )

    if not target_stocks:
        return HotAnalysisRunResponse(
            status="ok",
            started_at=started_at_iso,
            finished_at=datetime.now().isoformat(),
            duration_seconds=(datetime.now() - started_at_dt).total_seconds(),
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
    # Step 3: 初始化 Pipeline
    # ------------------------------------------------------------------
    try:
        from src.config import get_config
        from src.core.pipeline import StockAnalysisPipeline
        from src.enums import ReportType

        config = get_config()
        pipeline = StockAnalysisPipeline(
            config=config,
            max_workers=1,  # 每只股票内部已串行 3 轮；stock 间保持串行避免 LLM 限流
            query_id=query_id,
            query_source="hot-analysis",
        )
        report_type = ReportType.SIMPLE if req.simple_report else ReportType.FULL
    except Exception as e:  # noqa: BLE001
        logger.exception("[%s] Pipeline 初始化失败", query_id)
        raise HTTPException(status_code=500, detail=f"Pipeline 初始化失败: {e}")

    # ------------------------------------------------------------------
    # Step 4: 串行分析（每只股票内 3 轮）
    # ------------------------------------------------------------------
    deadline_ts = started_at_dt.timestamp() + req.max_runtime_seconds
    results: List[HotAnalysisStockResult] = []
    analyzed_stock_count = 0
    timed_out = False

    for stock in target_stocks:
        if time.time() > deadline_ts:
            timed_out = True
            logger.warning(
                "[%s] 达到最大运行时间 %ds，停止后续股票",
                query_id, req.max_runtime_seconds,
            )
            break

        code = stock["code"]
        name = stock.get("name") or code
        analyses_raw: List[Dict[str, Any]] = []

        # ★ per-round LLM 硬超时：避免远端 socket 卡死导致整个流程永远等
        LLM_TIMEOUT_SECONDS = 180.0

        # ★ 重新设计的轮询逻辑：
        #   - 最多尝试 MAX_ANALYSIS_ATTEMPTS 次
        #   - 累积到 MIN_SUCCESS_REQUIRED 次"有意义"成功就提前终止
        #   - 6 次都不够 3 次有意义成功 → 数据不充足 → 过滤掉
        MAX_ANALYSIS_ATTEMPTS = 6
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
                    timeout=LLM_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.error(
                    "[%s] %s(%s) 第 %d 次 LLM 调用超时（>%ds），标记为失败",
                    query_id, name, code, attempt_idx, int(LLM_TIMEOUT_SECONDS),
                )
                one = {
                    "round": attempt_idx,
                    "sentiment_score": None,
                    "operation_advice": None,
                    "decision_type": None,
                    "trend_prediction": None,
                    "model_used": None,
                    "timestamp": round_started.isoformat(),
                    "error": f"LLM_TIMEOUT ({int(LLM_TIMEOUT_SECONDS)}s)",
                }
                # 不 break，继续下一次尝试
            analyses_raw.append(one)
            if _is_meaningful_round(one):
                success_count += 1
                logger.info(
                    "[%s] %s(%s) 第 %d 次成功（%d/%d）",
                    query_id, name, code, attempt_idx, success_count, MIN_SUCCESS_REQUIRED,
                )
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
            continue  # 跳过这只股票，不进入 _check_pass

        pass_reason, passed = _check_pass(analyses_raw)
        if not passed:
            logger.info("[%s] %s(%s) 过滤掉: %s", query_id, name, code, pass_reason)
            continue

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

    finished_at = datetime.now()
    duration = (finished_at - started_at_dt).total_seconds()

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
