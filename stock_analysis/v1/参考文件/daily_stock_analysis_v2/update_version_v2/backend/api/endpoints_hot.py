# -*- coding: utf-8 -*-
"""
v2 后端 — hot_analysis + market_review 端点
- 复用 update_version_v2/db/history.py（SQLite）
- 复用 update_version_v2/services/market_review_service.py
- 复用主项目的 analyze_stock() 做单股分析
"""
import asyncio
import importlib.util
import json
import logging
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

# 把 update_version_v2/ 加入 sys.path 以访问根目录的 db/ 和 services/
_V2_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_V2_ROOT) not in sys.path:
    sys.path.insert(0, str(_V2_ROOT))


def _import_from_path(module_name: str, file_path: Path):
    """避免与 backend 自带的 db 包冲突，用 importlib 按路径导入"""
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod  # 注册到 sys.modules 防再次按包导入
    spec.loader.exec_module(mod)
    return mod


# 显式按路径导入根目录的 db.history（避免被 backend/db 包抢占）
db_history = _import_from_path("v2_root_db_history", _V2_ROOT / "db" / "history.py")
market_review_service = _import_from_path(
    "v2_root_market_review_service", _V2_ROOT / "services" / "market_review_service.py"
)

from fastapi import APIRouter, HTTPException, Query, Request  # noqa: E402

logger = logging.getLogger(__name__)
router = APIRouter()


# ============================================================================
# 单股分析 + 四榜（复用主项目 analyzer）
# ============================================================================
@router.post("/hot-analysis/analyze-single")
def analyze_single(payload: dict):
    """
    单股分析
    - payload: {code_or_name | code | name, rounds?, simple_report?, llm_timeout_seconds?}
    - 返回: SingleStockResponse 兼容结构
      {status, code, name, analyses, avg_score, final_advice, pass_reason, error}
    """
    code_or_name = payload.get("code_or_name") or payload.get("code") or payload.get("name")
    if not code_or_name:
        raise HTTPException(400, "code_or_name 必填")

    simple_report = payload.get("simple_report", True)
    # ★ Round 11 修复：前端发的是 llm_timeout_seconds / rounds（见 hotAnalysis.ts），
    #   之前读 llm_timeout（默认 30s）导致每轮 30s 超时 → 全部 LLM_TIMEOUT → 空骨架。
    llm_timeout = payload.get("llm_timeout_seconds") or payload.get("llm_timeout") or 180
    rounds = payload.get("rounds") or 3

    try:
        try:
            # 把主项目根加入 sys.path，再用普通 import（避免 backend.api 与 api 冲突）
            _MAIN_ROOT = Path(__file__).resolve().parent.parent.parent.parent
            if str(_MAIN_ROOT) not in sys.path:
                sys.path.insert(0, str(_MAIN_ROOT))
            # 把 api 顶层包注册到 sys.modules（如果 v2 backend 已经先 import 了 api 包，需要替换）
            import importlib
            # 强制重新加载主项目的 api 包
            for name in list(sys.modules.keys()):
                if name.startswith("api.") or name == "api":
                    if "update_version_v2" in str(getattr(sys.modules[name], "__file__", "") or ""):
                        del sys.modules[name]
            from api.v1.endpoints.hot_analysis import analyze_single_stock, SingleStockRequest
            req = SingleStockRequest(
                code=code_or_name,
                rounds=rounds,
                simple_report=simple_report,
                llm_timeout_seconds=llm_timeout,
            )
            # analyze_single_stock 是 async，需要在事件循环中跑
            try:
                loop = asyncio.new_event_loop()
                resp = loop.run_until_complete(analyze_single_stock(req))
                loop.close()
            except RuntimeError:
                # 已有 loop 在跑（FastAPI 请求上下文）
                # timeout 需覆盖 llm_timeout × MAX_ANALYSIS_ATTEMPTS(6) + 余量
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as ex:
                    fut = ex.submit(lambda: asyncio.run(analyze_single_stock(req)))
                    resp = fut.result(timeout=max(900.0, float(llm_timeout) * 7))
            if hasattr(resp, "model_dump"):
                result = resp.model_dump()
            elif hasattr(resp, "dict"):
                result = resp.dict()
            else:
                result = dict(resp)
        except Exception as e:
            logger.warning("Main project analyze_single_stock not available: %s; using mock", e)
            result = {}

        # ★ Round 11 修复：检测"空骨架"返回（sandbox / 网络受限时 pipeline 跑完但
        #   analyses 为空或全部轮次 error）→ 用 mock 行情补一份可读结果。
        #   注意：SingleStockResponse schema 为 {status, code, name, analyses,
        #   avg_score, final_advice, pass_reason, error}——没有 summary/score 字段，
        #   判空必须基于真实 schema，否则会把真实分析结果误判为空并覆盖。
        #   status="failed" 的响应原样透传（前端展示真实错误，如"无法识别代码"）。
        _analyses = result.get("analyses") or []
        _all_errored = bool(_analyses) and all(a.get("error") for a in _analyses)
        _empty_skeleton = result.get("status") != "failed" and (
            not result or not _analyses or _all_errored
        )
        if _empty_skeleton:
            logger.warning(
                "analyze-single returned empty payload for %s (sandbox/offline). Using mock fallback.",
                code_or_name,
            )
            mock_quote = None
            try:
                from core import data_source as _ds
                mock_quote = _ds.fetch_quote_with_fallback(code_or_name)
            except Exception as _mqe:
                logger.warning("mock quote fetch failed: %s", _mqe)
            price = float(mock_quote.get("price", 0)) if mock_quote else 0.0
            chg = float(mock_quote.get("change_pct", 0)) if mock_quote else 0.0
            name = (mock_quote or {}).get("name") or code_or_name
            advice = "持有"
            if chg >= 1.5:
                advice = "可关注"
            elif chg <= -1.5:
                advice = "观望"
            result = {
                "status": "ok",
                "code": code_or_name,
                "name": name,
                "analyses": [],
                "avg_score": max(0, min(100, round(60 + chg * 5, 2))),
                "final_advice": advice,
                "pass_reason": (
                    f"{name}（{code_or_name}）当前价格 ¥{price:.2f}，"
                    f"涨跌幅 {chg:+.2f}%。"
                    "（注：当前为沙箱/离线环境，使用合成数据；如需完整 LLM 分析，"
                    "请确保网络可达 akshare / eastmoney / LLM 服务）"
                ),
                "error": None,
                "price": price,
                "change_pct": chg,
                "industry": (mock_quote or {}).get("industry", ""),
                "source": (mock_quote or {}).get("source", "mock"),
                "offline_fallback": True,
            }
        elif isinstance(result.get("avg_score"), (int, float)):
            # clamp 防止越界
            result["avg_score"] = max(0, min(100, result["avg_score"]))
    except Exception as e:
        logger.exception("analyze-single failed: %s", e)
        raise HTTPException(500, f"分析失败: {str(e)[:200]}")
    return result


@router.post("/hot-analysis/preview")
def hot_analysis_preview(payload: dict):
    """四榜预览（不分析，只抓榜）"""
    try:
        # 先把主项目根加入 sys.path，确保 importlib 加载时能找到 api.v1 包
        _MAIN_ROOT = Path(__file__).resolve().parent.parent.parent.parent
        if str(_MAIN_ROOT) not in sys.path:
            sys.path.insert(0, str(_MAIN_ROOT))
        # 清理 v2 backend.api 在 sys.modules 中的注册（防止覆盖主项目 api 包）
        for n in list(sys.modules.keys()):
            if n.startswith("api.") or n == "api":
                f = getattr(sys.modules[n], "__file__", "") or ""
                if "update_version_v2" in str(f):
                    del sys.modules[n]
        # 用普通 import 让 api.v1 注册正确包路径
        import importlib
        import api.v1.endpoints.hot_analysis as _hot_mod
        from api.v1.schemas.hot_analysis import HotAnalysisPreviewRequest as _Req
        req = _Req(
            hot_list_n=payload.get("hot_list_n", 200),
            max_intersection=payload.get("max_intersection", 30),
        )
        try:
            loop = asyncio.new_event_loop()
            resp = loop.run_until_complete(_hot_mod.preview_hot_analysis(req))
            loop.close()
        except RuntimeError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as ex:
                resp = ex.submit(lambda: asyncio.run(_hot_mod.preview_hot_analysis(req))).result(timeout=120)
        return resp.dict() if hasattr(resp, "dict") else dict(resp)
    except Exception as e:
        import traceback
        logger.error("preview FAILED: %s\n%s", e, traceback.format_exc())
        return {
            "intersection": [],
            "warning": f"preview failed: {type(e).__name__}: {str(e)[:300]}",
        }


@router.post("/hot-analysis/run")
def hot_analysis_run(payload: dict, request: Request = None):
    """
    同步运行完整流程（四榜 + 三轮 LLM + 过滤）
    payload 字段：见 README
    ★ Round 4 限流 1/min/IP（非常贵）
    """
    from core import ratelimit
    if request is not None:
        ip = ratelimit.get_client_ip(request)
        allowed, retry = ratelimit.check_limit(ip, "/api/v2/hot-analysis/run")
        if not allowed:
            raise HTTPException(429, f"限流，{retry}s 后重试")
    try:
        _MAIN_ROOT = Path(__file__).resolve().parent.parent.parent.parent
        if str(_MAIN_ROOT) not in sys.path:
            sys.path.insert(0, str(_MAIN_ROOT))
        for n in list(sys.modules.keys()):
            if n.startswith("api.") or n == "api":
                f = getattr(sys.modules[n], "__file__", "") or ""
                if "update_version_v2" in str(f):
                    del sys.modules[n]
        import api.v1.endpoints.hot_analysis as _hot_mod
        from api.v1.schemas.hot_analysis import HotAnalysisRunRequest as _RunReq
        req = _RunReq(
            hot_list_n=payload.get("hot_list_n", 200),
            max_intersection=payload.get("max_intersection", 10),
            exclude_kechuang=payload.get("exclude_kechuang", True),
            rounds=payload.get("rounds", 3),
            max_runtime_seconds=payload.get("max_runtime_seconds", 600),
            inter_round_sleep=payload.get("inter_round_sleep", 0.6),
            simple_report=payload.get("simple_report", True),
            llm_timeout_seconds=payload.get("llm_timeout_seconds", 180),
            min_score=payload.get("min_score", 60),
            allowed_advice=payload.get("allowed_advice", ["买入", "加仓", "持有"]),
            exclude_keywords=payload.get("exclude_keywords", ["卖出", "减仓"]),
            require_all_rounds=payload.get("require_all_rounds", True),
        )
        try:
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(_hot_mod.run_hot_analysis(req))
            loop.close()
        except RuntimeError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as ex:
                result = ex.submit(lambda: asyncio.run(_hot_mod.run_hot_analysis(req))).result(timeout=600)
        result = result.dict() if hasattr(result, "dict") else dict(result)
        # 写历史
        try:
            db_history.save_analysis_run(
                started_at=datetime.now().isoformat(),
                finished_at=datetime.now().isoformat(),
                duration_seconds=result.get("duration", 0),
                status="success",
                hot_lists_summary=result.get("hot_lists_summary", {}),
                hot_lists_status=result.get("hot_lists_status", {}),
                hot_lists_notes=result.get("hot_lists_notes", {}),
                raw_intersection_count=result.get("raw_intersection_count", 0),
                filtered_intersection_count=result.get("filtered_intersection_count", 0),
                analyzed_stock_count=result.get("analyzed_stock_count", 0),
                passed_stock_count=result.get("passed_stock_count", 0),
                filter_config=payload,
                results=result.get("results", []),
            )
        except Exception as e:
            logger.warning("save_analysis_run failed: %s", e)
        return result
    except ImportError:
        raise HTTPException(503, "hot_list_runner not available")
    except Exception as e:
        logger.exception("hot-analysis run failed: %s", e)
        raise HTTPException(500, f"运行失败: {str(e)[:200]}")


# ============================================================================
# 异步任务 + SSE 进度推送
# ============================================================================
_TASKS: Dict[str, Dict[str, Any]] = {}
# ★ Round 4 修复：task TTL 自动清理（默认 30 分钟）
_TASK_TTL_SEC = 1800


def _cleanup_tasks() -> int:
    """清理已完成/失败超过 TTL 的 task entry，避免长跑后内存膨胀。"""
    now = time.time()
    expired = [tid for tid, t in _TASKS.items()
               if t.get("finished_at") and (now - t["finished_at"]) > _TASK_TTL_SEC]
    for tid in expired:
        _TASKS.pop(tid, None)
    return len(expired)


def _ensure_background_task_ref(task_id: str, task: asyncio.Task) -> None:
    """保留 asyncio task 引用并 done callback 记录失败（防 GC + 异常丢失）"""
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)

    def _on_done(t: asyncio.Task):
        try:
            exc = t.exception()
            if exc:
                logger.exception("[bg-task %s] failed: %s", task_id, exc)
        except (asyncio.CancelledError, Exception):
            pass
    task.add_done_callback(_on_done)


_bg_tasks: "set[asyncio.Task]" = set()


@router.post("/hot-analysis/run-async")
def hot_analysis_run_async(payload: dict, request: Request = None):
    """异步任务（返回 task_id）★ Round 4 限流 3/min/IP"""
    from core import ratelimit
    if request is not None:
        ip = ratelimit.get_client_ip(request)
        allowed, retry = ratelimit.check_limit(ip, "/api/v2/hot-analysis/run-async")
        if not allowed:
            raise HTTPException(429, f"限流，{retry}s 后重试")
    task_id = uuid.uuid4().hex[:16]
    _TASKS[task_id] = {
        "status": "pending",
        "started_at": time.time(),
        "progress": 0,
        "message": "排队中",
        "result": None,
    }

    async def _run():
        _TASKS[task_id].update({"status": "running", "message": "开始四榜抓取"})
        try:
            # ★ 懒加载：v2 backend 不一定有 update_version 父项目
            #   → ImportError 时降级为 503 而不是 500
            try:
                from update_version.services.hot_list_runner import run_full_pipeline
            except ImportError as ie:
                logger.warning("update_version.services.hot_list_runner not available: %s", ie)
                _TASKS[task_id].update({
                    "status": "failed",
                    "message": "主项目 update_version 不可用，无法启动分析。请在主项目根目录运行。",
                    "finished_at": time.time(),
                })
                return
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: run_full_pipeline(**{k: v for k, v in payload.items()}))
            _TASKS[task_id].update({
                "status": "success",
                "progress": 100,
                "message": "完成",
                "result": result,
                "finished_at": time.time(),
            })
        except Exception as e:
            logger.exception("async task failed: %s", e)
            _TASKS[task_id].update({
                "status": "failed",
                "message": str(e)[:200],
                "finished_at": time.time(),
            })

    # ★ Round 4 修复：保留 asyncio task 引用 + 30 分钟后自动清理 task entry
    _ensure_background_task_ref(task_id, asyncio.create_task(_run()))
    return {"task_id": task_id}


@router.get("/hot-analysis/run/{task_id}/stream")
async def hot_analysis_stream(task_id: str):
    """SSE 进度推送"""
    from fastapi.responses import StreamingResponse

    async def event_gen():
        last_msg = ""
        try:
            for _ in range(600):  # 最多 10 分钟
                # ★ Round 4 修复：周期性清理过期任务，避免 _TASKS 无限增长
                if _cleanup_tasks() > 0:
                    logger.debug("Cleaned %d expired tasks from _TASKS", _cleanup_tasks())
                t = _TASKS.get(task_id, {"status": "unknown"})
                msg = f"{t.get('progress', 0)}%: {t.get('message', '')}"
                if msg != last_msg:
                    yield f"event: progress\ndata: {json.dumps(t, default=str, ensure_ascii=False)}\n\n"
                    last_msg = msg
                if t.get("status") in ("success", "failed"):
                    yield f"event: done\ndata: {json.dumps(t, default=str, ensure_ascii=False)}\n\n"
                    break
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            # 客户端断开
            logger.debug("SSE client disconnected for task_id=%s", task_id)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/hot-analysis/tasks/{task_id}")
def hot_analysis_task_status(task_id: str):
    """轮询查询任务状态（不依赖 SSE 的 fallback）"""
    t = _TASKS.get(task_id)
    if not t:
        raise HTTPException(404, f"task {task_id} 不存在")
    return t


# ============================================================================
# 历史 + 自选 + 模板 + 定时任务
# ============================================================================
@router.get("/hot-analysis/history")
def hot_analysis_history(limit: int = Query(50, le=200), offset: int = 0, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200)):
    # 兼容两种调用：limit+offset（旧）或 page+page_size（新）
    if page_size == 20 and page == 1:
        # 使用 limit+offset 计算
        page_size_eff = limit
        page_eff = (offset // page_size_eff) + 1 if page_size_eff > 0 else 1
    else:
        page_size_eff = page_size
        page_eff = page
    runs, total = db_history.get_analysis_history(page=page_eff, page_size=page_size_eff)
    return {
        "items": runs,
        "total": total,
        "page": page_eff,
        "page_size": page_size_eff,
    }


@router.get("/hot-analysis/history/{run_id}")
def hot_analysis_history_detail(run_id: int):
    run = db_history.get_analysis_run(run_id)
    if not run:
        raise HTTPException(404, "run 不存在")
    return run


@router.delete("/hot-analysis/history/{run_id}")
def hot_analysis_history_delete(run_id: int):
    if not db_history.delete_analysis_run(run_id):
        raise HTTPException(404, "run 不存在")
    return {"deleted": run_id}


@router.get("/hot-analysis/watchlist")
def hot_analysis_watchlist():
    return {"items": db_history.get_watchlist()}


@router.post("/hot-analysis/watchlist")
def hot_analysis_watchlist_add(payload: dict):
    code = payload.get("code")
    name = payload.get("name", "")
    notes = payload.get("notes", "")
    if not code:
        raise HTTPException(400, "code 必填")
    wid = db_history.add_to_watchlist(code, name, notes)
    return {"watchlist_id": wid}


@router.delete("/hot-analysis/watchlist/{code}")
def hot_analysis_watchlist_remove(code: str):
    if not db_history.remove_from_watchlist(code):
        raise HTTPException(404, "不在自选股")
    return {"removed": code}


@router.get("/hot-analysis/templates")
def hot_analysis_templates():
    return {"items": db_history.get_templates()}


@router.post("/hot-analysis/templates")
def hot_analysis_templates_save(payload: dict):
    name = payload.get("name")
    config = payload.get("config", {})
    if not name:
        raise HTTPException(400, "name 必填")
    tid = db_history.save_template(name, config, payload.get("description", ""), payload.get("is_default", False))
    return {"id": tid}


@router.delete("/hot-analysis/templates/{template_id}")
def hot_analysis_templates_delete(template_id: int):
    if not db_history.delete_template(template_id):
        raise HTTPException(404, "template 不存在")
    return {"deleted": template_id}


@router.get("/hot-analysis/scheduled-tasks")
def hot_analysis_scheduled_tasks():
    return {"items": db_history.get_scheduled_tasks()}


@router.post("/hot-analysis/scheduled-tasks")
def hot_analysis_scheduled_tasks_save(payload: dict):
    name = payload.get("name")
    cron_expr = payload.get("cron_expr")
    config = payload.get("config", {})
    enabled = payload.get("enabled", True)
    task_id = payload.get("task_id")
    if not name or not cron_expr:
        raise HTTPException(400, "name + cron_expr 必填")
    tid = db_history.save_scheduled_task(name, cron_expr, config, enabled, task_id)
    return {"id": tid}


@router.delete("/hot-analysis/scheduled-tasks/{task_id}")
def hot_analysis_scheduled_tasks_delete(task_id: int):
    if not db_history.delete_scheduled_task(task_id):
        raise HTTPException(404, "task 不存在")
    return {"deleted": task_id}


@router.post("/hot-analysis/scheduled-tasks/{task_id}/run")
def hot_analysis_scheduled_tasks_run(task_id: int):
    """手动触发定时任务"""
    task = db_history.get_scheduled_task(task_id)
    if not task:
        raise HTTPException(404, "task 不存在")
    config = task.get("config", {})
    # 异步启动
    async def _run():
        try:
            # ★ 懒加载：缺 update_version 父项目时降级处理而非抛 ImportError
            try:
                from update_version.services.hot_list_runner import run_full_pipeline
            except ImportError as ie:
                logger.warning("update_version.services.hot_list_runner not available: %s", ie)
                db_history.update_task_run(task_id, "error")
                return
            await asyncio.get_event_loop().run_in_executor(None, lambda: run_full_pipeline(**config))
        except Exception as e:
            logger.exception("scheduled run failed: %s", e)
    asyncio.create_task(_run())
    db_history.update_task_run(task_id, "running")
    return {"started": task_id}


# ============================================================================
# 大盘复盘（复用 services/market_review_service.py）
# ============================================================================
# 前端历史代码在 /api/v1/hot-analysis/market-review/* 路径下请求大盘复盘
# 这里把 /hot-analysis/market-review/* 全部 alias 到 /market-review/*
# （endpoints_hot.py 同时挂 /api/v1 和 /api/v2 两个前缀，所以两边都生效）

# ---- /hot-analysis/market-review/* 兼容 alias ----
# ★ 重要：先注册具体路径（list / latest / generate / compare / batch），
#   再注册 /{review_date} 通配符，否则 list/latest 等会被当作 review_date。
@router.post("/hot-analysis/market-review/generate")
def market_review_generate_alias_post(payload: dict = {}):
    return market_review_generate(payload)


@router.get("/hot-analysis/market-review")
def market_review_list_alias(limit: int = Query(30, le=100), page: int = 1, page_size: int = Query(20, le=100)):
    """兼容 /hot-analysis/market-review（无 /list 后缀）的旧调用"""
    return market_review_list(limit=limit, page=page, page_size=page_size)


@router.get("/hot-analysis/market-review/list")
def market_review_list_alias2(limit: int = Query(30, le=100), page: int = 1, page_size: int = Query(20, le=100)):
    return market_review_list(limit=limit, page=page, page_size=page_size)


@router.get("/hot-analysis/market-review/latest")
def market_review_latest_alias(region: Optional[str] = None):
    return market_review_latest(region=region)


@router.post("/hot-analysis/market-review/compare")
def market_review_compare_alias(payload: dict):
    return market_review_compare(payload)


@router.post("/hot-analysis/market-review/batch")
def market_review_batch_alias(payload: dict):
    return market_review_batch(payload)


@router.get("/hot-analysis/market-review/{review_date}/export")
def market_review_export_alias(review_date: str):
    return market_review_export(review_date)
    return market_review_export(review_date)


def _safe_review_date(review_date: str) -> str:
    """★ Round 4 修复：防止 review_date 被用于注入 header / 文件名"""
    import re
    m = re.match(r"^\d{4}-\d{2}-\d{2}$", review_date)
    if not m:
        raise HTTPException(400, "review_date 必须是 YYYY-MM-DD")
    return m.group(0)


@router.get("/hot-analysis/market-review/{review_date}/pdf")
def market_review_pdf_alias(review_date: str):
    """PDF 导出 — 委托给主项目端点"""
    from fastapi.responses import PlainTextResponse
    review_date = _safe_review_date(review_date)
    review = db_history.get_market_review(review_date)
    if not review:
        raise HTTPException(404, f"{review_date} 复盘不存在")
    try:
        from api.v1.endpoints.hot_analysis import _generate_review_pdf
        pdf_bytes = _generate_review_pdf(review)
        from fastapi.responses import Response
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=market_review_{review_date}.pdf"},
        )
    except (ImportError, AttributeError):
        md = market_review_service.export_review_as_markdown(review)
        return PlainTextResponse(
            content=md,
            media_type="text/markdown",
            headers={"Content-Disposition": f"attachment; filename=market_review_{review_date}.md"},
        )


@router.delete("/hot-analysis/market-review/{review_date}")
def market_review_delete_alias(review_date: str):
    return market_review_delete(review_date)


# ---- 原生 /market-review/* 路径（保留） ----
@router.post("/market-review/generate")
def market_review_generate(payload: dict = {}, request: Request = None):
    """生成大盘复盘（限流 1/min/IP）"""
    from core import ratelimit
    if request is not None:
        ip = ratelimit.get_client_ip(request)
        allowed, retry = ratelimit.check_limit(ip, "/api/v2/market-review/generate")
        if not allowed:
            raise HTTPException(429, f"限流，{retry}s 后重试")
    review_date = payload.get("review_date")
    region = payload.get("region", "cn")
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                asyncio.to_thread(market_review_service.generate_market_review,
                                  review_date=review_date, region=region)
            )
        finally:
            loop.close()
        return result
    except Exception as e:
        logger.exception("market-review generate failed: %s", e)
        raise HTTPException(500, "复盘生成失败，请稍后重试")


@router.get("/market-review/latest")
def market_review_latest(region: Optional[str] = None):
    """最新复盘"""
    review = db_history.get_latest_market_review(region=region)
    if not review:
        raise HTTPException(404, "尚无复盘，请先调用 /market-review/generate")
    return review


@router.get("/market-review/list")
def market_review_list_with_suffix(limit: int = Query(30, le=100), page: int = 1, page_size: int = Query(20, le=100)):
    """★ Round 10：显式 /list 路由，必须在 /{review_date} 前声明（否则 {review_date}=list 占位）"""
    return market_review_list(limit=limit, page=page, page_size=page_size)


@router.get("/market-review")
def market_review_list(limit: int = Query(30, le=100), page: int = 1, page_size: int = Query(20, le=100)):
    """复盘历史列表（兼容 limit 与 page_size 两种命名）

    返回字段与 v1 主项目 MarketReviewListResponse 一致：items + total + page + page_size。
    """
    effective_size = page_size if page_size != 20 else limit
    items, total = db_history.get_market_review_list(page=page, page_size=effective_size)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": effective_size,
    }


@router.get("/market-review/{review_date}")
def market_review_get(review_date: str):
    """取某日复盘"""
    review = db_history.get_market_review(review_date)
    if not review:
        raise HTTPException(404, f"{review_date} 复盘不存在")
    return review


@router.delete("/market-review/{review_date}")
def market_review_delete(review_date: str):
    if not db_history.delete_market_review(review_date):
        raise HTTPException(404, f"{review_date} 复盘不存在")
    return {"deleted": review_date}


@router.get("/market-review/{review_date}/export")
def market_review_export(review_date: str):
    """导出 markdown"""
    review_date = _safe_review_date(review_date)
    review = db_history.get_market_review(review_date)
    if not review:
        raise HTTPException(404, f"{review_date} 复盘不存在")
    md = market_review_service.export_review_as_markdown(review)
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        content=md,
        media_type="text/markdown",
        headers={"Content-Disposition": f"attachment; filename=market_review_{review_date}.md"},
    )


@router.post("/market-review/compare")
def market_review_compare(payload: dict):
    """两日对比"""
    date_a = payload.get("date_a")
    date_b = payload.get("date_b")
    if not date_a or not date_b:
        raise HTTPException(400, "date_a + date_b 必填")
    a = db_history.get_market_review(date_a)
    b = db_history.get_market_review(date_b)
    if not a or not b:
        raise HTTPException(404, "其中一日不存在")
    return market_review_service.compare_two_reviews(a, b)


@router.post("/market-review/batch")
def market_review_batch(payload: dict, request: Request = None):
    """批量生成近 N 天（限流 1/10min/IP）"""
    from core import ratelimit
    if request is not None:
        ip = ratelimit.get_client_ip(request)
        allowed, retry = ratelimit.check_limit(ip, "/api/v2/market-review/batch")
        if not allowed:
            raise HTTPException(429, f"限流，{retry}s 后重试")
    days = payload.get("days", 7)
    return market_review_service.batch_generate_recent_reviews(days=days)


# ============================================================================
# /health（v2 已有，保留即可）
# ============================================================================