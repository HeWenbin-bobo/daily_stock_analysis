# -*- coding: utf-8 -*-
"""
v2 后端 — hot_analysis + market_review 端点
- 复用 update_version_v4/db/history.py（SQLite）
- 复用 update_version_v4/services/market_review_service.py
- 复用主项目的 analyze_stock() 做单股分析
"""
import asyncio
import importlib.util
import json
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

# 把 update_version_v4/ 加入 sys.path 以访问根目录的 db/ 和 services/
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


# 主项目 hot_analysis 模块缓存（★ 修复(M5)：避免每次请求重新 exec_module + 清 sys.modules）
# 旧实现：10+ 调用点每次请求都重新 import api.v1.endpoints.hot_analysis 并 del sys.modules 中的
# v2 api 包。并发请求下 del sys.modules[name] 会让另一个线程正在使用的模块对象被替换，
# 可能引发偶发 AttributeError / ModuleNotFoundError 500；且每次全量 re-exec 的 CPU 开销在并发下放大。
_MAIN_HOT_MOD = None
_MAIN_HOT_MOD_LOCK = threading.Lock()


def _import_main_hot_analysis():
    """一次性 import 主项目 api.v1.endpoints.hot_analysis + schemas。

    ★ 迭代4 修复：原先各调用点"清理 v4 api.* → import 主项目链"后不恢复，
      主项目 api 包从此占住 sys.modules["api"] 坑位，此后任何
      `import api.endpoints_v2`（app.py 懒加载、测试）都会
      ModuleNotFoundError。现在：
      - 快照 v4 的 api.* → 主项目根强制提到 sys.path[0]（原先
        `if not in sys.path: insert(0)` 在主项目根已存在但不在 [0]
        ——如 pytest 注入——时跳过重排，import 仍失败）→ 清 v4 api.* →
        import 主项目链 → finally 恢复 v4 api.*；
      - 主项目子模块（api.v1.*）按全名留在 sys.modules，之后 import 走
        全名缓存命中；全新子模块经父包 __path__（指向主项目）解析，
        与顶层 api 坑位归谁无关。
    """
    _MAIN_ROOT = Path(__file__).resolve().parent.parent.parent.parent
    v4_snap = {
        n: m for n, m in sys.modules.items()
        if (n == "api" or n.startswith("api."))
        and "update_version_v4" in str(getattr(m, "__file__", "") or "")
    }
    while str(_MAIN_ROOT) in sys.path:
        sys.path.remove(str(_MAIN_ROOT))
    sys.path.insert(0, str(_MAIN_ROOT))
    for n in list(v4_snap):
        del sys.modules[n]
    try:
        import api.v1.endpoints.hot_analysis as _hot_mod
        import api.v1.schemas.hot_analysis as _schemas_mod
    finally:
        sys.modules.update(v4_snap)
    return _hot_mod, _schemas_mod


def _load_main_hot_analysis():
    """把主项目 api.v1.endpoints.hot_analysis 载入并返回模块（进程内只加载一次）。

    与 analyze_single / preview / run 使用同一套加载机制，结果缓存到
    _MAIN_HOT_MOD，线程锁保证并发安全。
    """
    global _MAIN_HOT_MOD
    if _MAIN_HOT_MOD is not None:
        return _MAIN_HOT_MOD
    with _MAIN_HOT_MOD_LOCK:
        # 双重检查：等待锁期间可能已被其他线程加载
        if _MAIN_HOT_MOD is not None:
            return _MAIN_HOT_MOD
        _MAIN_HOT_MOD = _import_main_hot_analysis()[0]
        return _MAIN_HOT_MOD


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
            # ★ 迭代4：统一走 _import_main_hot_analysis（快照/恢复 v4 api.*，
            #   主项目根强制提到 sys.path[0]），不再散落各端点手工手术。
            import importlib
            _hot_mod, _schemas_mod = _import_main_hot_analysis()
            analyze_single_stock = _hot_mod.analyze_single_stock
            SingleStockRequest = _schemas_mod.SingleStockRequest
            req = SingleStockRequest(
                code=code_or_name,
                rounds=rounds,
                simple_report=simple_report,
                llm_timeout_seconds=llm_timeout,
                # ★ 修复：透传轮间间隔（前端 composable 已传，此前端点丢弃
                #   → 用户把轮间间隔调大对单股分析不生效，且每轮之间 0 间隔
                #   快速连续调 LLM 更容易触发限流）
                inter_round_sleep=float(payload.get("inter_round_sleep") or 0.6),
            )
            # analyze_single_stock 是 async，需要在事件循环中跑
            # ★ Round 19 修复(M10)：统一用 asyncio.run() 委托线程执行，
            #   自动创建/关闭 loop（协程抛异常也会正确清理），避免手动 new_event_loop 泄漏
            #   + 旧的 except RuntimeError 分支在 new_event_loop 下永远不会命中（死代码）
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
                # ★ Round 26：mock 回退也提供汇总操作建议
                "summary": (
                    f"{name}（{code_or_name}）现价 ¥{price:.2f}（{chg:+.2f}%），"
                    f"综合建议：{advice}。"
                    "（当前为离线合成数据，建议在网络可用时重新分析获取完整结论。）"
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
        # ★ 迭代4：统一走 _import_main_hot_analysis（快照/恢复 v4 api.*，
        #   主项目根强制提到 sys.path[0]）
        import importlib
        _hot_mod, _schemas_mod = _import_main_hot_analysis()
        _Req = _schemas_mod.HotAnalysisPreviewRequest
        # ★ Round 19 修复(C1)：透传前端全部过滤配置，否则 min_score/rounds 等
        #   只按默认值估算 target_count / 耗时，预览数字与真实运行严重不符
        req = _Req(
            hot_list_n=payload.get("hot_list_n", 200),
            max_intersection=payload.get("max_intersection", 30),
            exclude_kechuang=payload.get("exclude_kechuang", True),
            rounds=payload.get("rounds", 3),
            inter_round_sleep=payload.get("inter_round_sleep", 0.6),
            simple_report=payload.get("simple_report", True),
            llm_timeout_seconds=payload.get("llm_timeout_seconds", 180),
            min_score=payload.get("min_score", 60),
            allowed_advice=payload.get("allowed_advice", ["买入", "加仓", "持有"]),
            exclude_keywords=payload.get("exclude_keywords", ["卖出", "减仓"]),
            require_all_rounds=payload.get("require_all_rounds", True),
            max_concurrency=payload.get("max_concurrency", 1),
        )
        try:
            loop = asyncio.new_event_loop()
            resp = loop.run_until_complete(_hot_mod.preview_hot_analysis(req))
            loop.close()
        except RuntimeError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as ex:
                resp = ex.submit(lambda: asyncio.run(_hot_mod.preview_hot_analysis(req))).result(timeout=120)
        result = resp.dict() if hasattr(resp, "dict") else dict(resp)
        if isinstance(result, dict) and "status" not in result:
            result["status"] = "ok"
        return result
    except Exception as e:
        import traceback
        logger.error("preview FAILED: %s\n%s", e, traceback.format_exc())
        # ★ Round 19 修复(C3)：失败必须返回 status='failed'，前端据此进入错误态
        #   而不是误以为"已预览"、显示 undefined 还可点"确认开始分析"
        return {
            "status": "failed",
            "error": f"preview failed: {type(e).__name__}: {str(e)[:300]}",
            "intersection": [],
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
        # ★ 迭代4：统一走 _import_main_hot_analysis（快照/恢复 v4 api.*，
        #   主项目根强制提到 sys.path[0]）
        _hot_mod, _schemas_mod = _import_main_hot_analysis()
        _RunReq = _schemas_mod.HotAnalysisRunRequest
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
            max_concurrency=payload.get("max_concurrency", 1),
        )
        try:
            loop = asyncio.new_event_loop()
            result = loop.run_until_complete(_hot_mod.run_hot_analysis(req))
            loop.close()
        except RuntimeError:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as ex:
                try:
                    result = ex.submit(lambda: asyncio.run(_hot_mod.run_hot_analysis(req))).result(timeout=600)
                except concurrent.futures.TimeoutError:
                    # ★ Round 20 修复(M11)：同步模式超时不返回 500，也不泄漏孤儿线程；
                    #   分析仍在后台跑，提示用户改用 /run-async + SSE 获取实时进度
                    logger.warning("hot-analysis /run timed out after 600s; analysis continues in background")
                    raise HTTPException(
                        504,
                        "同步分析超时（600s）。分析仍在后台继续，请改用 run-async + SSE 查看实时进度",
                    )
        result = result.dict() if hasattr(result, "dict") else dict(result)
        # 写历史
        try:
            db_history.save_analysis_run(
                started_at=datetime.now().isoformat(),
                finished_at=datetime.now().isoformat(),
                duration_seconds=result.get("duration_seconds", result.get("duration", 0)),
                # ★ 迭代2 修复(H4)：用真实结果状态而非硬编码 success（失败任务此前显示为成功）
                status=result.get("status", "success"),
                hot_lists_summary=result.get("hot_lists_summary", {}),
                hot_lists_status=result.get("hot_lists_status", {}),
                hot_lists_notes=result.get("hot_lists_notes", {}),
                raw_intersection_count=result.get("raw_intersection_count", 0),
                filtered_intersection_count=result.get("filtered_intersection_count", 0),
                analyzed_stock_count=result.get("analyzed_stock_count", 0),
                passed_stock_count=result.get("passed_stock_count", 0),
                filter_config=payload,
                results=result.get("results", []),
                filtered_stocks=result.get("filtered_stocks", []),
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
# 健康检查 + 对比 + PDF 报告 + 实时行情（★ 本轮补全前端已引用但缺失的端点）
# ============================================================================
@router.get("/hot-analysis/health")
def hot_analysis_health():
    """轻量健康检查：akshare 是否可用"""
    try:
        import akshare as ak  # noqa: F401
        return {"status": "ok", "akshare_available": True, "module": "hot_analysis"}
    except Exception as e:
        # ★ 迭代4 修复：不返回 str(e) 原文（可能含路径/Python 版本等内部信息），只返回异常类型
        return {"status": "degraded", "akshare_available": False, "error": type(e).__name__, "module": "hot_analysis"}


@router.post("/hot-analysis/compare")
def hot_analysis_compare(payload: dict):
    """对比两次分析记录（run_id_1 / run_id_2），基于 v2 自己的历史库。"""
    run_id_1 = payload.get("run_id_1")
    run_id_2 = payload.get("run_id_2")
    if not run_id_1 or not run_id_2:
        raise HTTPException(400, "run_id_1 + run_id_2 必填")
    # ★ Round 19 修复(M6)：run_id 类型校验，非法值返回 400 而非 500
    try:
        id_1 = int(run_id_1)
        id_2 = int(run_id_2)
    except (TypeError, ValueError):
        raise HTTPException(400, "run_id_1 / run_id_2 必须是整数")
    run1 = db_history.get_analysis_run(id_1)
    run2 = db_history.get_analysis_run(id_2)
    if not run1:
        raise HTTPException(404, f"记录 {run_id_1} 不存在")
    if not run2:
        raise HTTPException(404, f"记录 {run_id_2} 不存在")
    codes1 = {r["code"] for r in run1.get("results", [])}
    codes2 = {r["code"] for r in run2.get("results", [])}
    new_stocks = sorted(codes2 - codes1)
    removed_stocks = sorted(codes1 - codes2)
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
    return {
        "run_1": run1,
        "run_2": run2,
        "new_stocks": new_stocks,
        "removed_stocks": removed_stocks,
        "score_changes": score_changes,
    }


@router.post("/hot-analysis/pdf-report")
async def hot_analysis_pdf_report(payload: dict = {}):
    """生成 PDF 分析报告（run_id 可选；默认最新一次分析）。

    复用主项目 _generate_pdf（reportlab + 中文字体注册），但数据取自 v2 历史库。
    """
    run_id = payload.get("run_id")
    if run_id:
        # ★ 迭代5 修复(H7)：run_id 类型校验，非数字返回 400 而非 500
        try:
            run_id = int(run_id)
        except (TypeError, ValueError):
            raise HTTPException(400, "run_id 必须是整数")
        run = db_history.get_analysis_run(run_id)
    else:
        items, _total = db_history.get_analysis_history(page=1, page_size=1)
        run = items[0] if items else None
    if not run:
        return {"status": "error", "error": "没有可用的分析记录"}
    try:
        _hot_mod = _load_main_hot_analysis()
        pdf_bytes = await asyncio.to_thread(_hot_mod._generate_pdf, run)
        import base64
        pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")
        return {
            "status": "ok",
            "filename": f"hot-analysis-{run['id']}.pdf",
            "pdf_base64": pdf_b64,
            "size_bytes": len(pdf_bytes),
        }
    except ImportError:
        return {"status": "error", "error": "PDF 生成需要 reportlab 库，请运行: pip install reportlab"}
    except Exception as e:
        logger.exception("PDF 报告生成失败")
        return {"status": "error", "error": str(e)[:300]}


@router.get("/hot-analysis/market-data")
async def hot_analysis_market_data(codes: str = ""):
    """获取个股实时行情（逗号分隔，最多 50 个；复用主项目 _fetch_market_data）"""
    code_list = [c.strip() for c in codes.split(",") if c.strip()] if codes else []
    if not code_list:
        return {"items": [], "updated_at": datetime.now().isoformat()}
    if len(code_list) > 50:
        raise HTTPException(400, f"单次最多 50 个代码，实际 {len(code_list)} 个")
    try:
        _hot_mod = _load_main_hot_analysis()
        items = await asyncio.to_thread(_hot_mod._fetch_market_data, code_list)
        return {"items": items, "updated_at": datetime.now().isoformat()}
    except Exception as e:
        logger.exception("[market-data] 获取行情数据失败 codes=%s", code_list)
        return {"items": [], "updated_at": datetime.now().isoformat()}


# ============================================================================
# 异步任务 + SSE 进度推送
# ★ Round 15 重构：委托主项目 api.v1.endpoints.hot_analysis 的 async 任务机制
#   （run_async_analysis + progress_queue + stream_analysis_progress + cancel_analysis_task）
#   旧 v2 自建 _TASKS/_run() 在整个管线期间不推进度 → 前端进度条恒 0% "抓榜中"，
#   用户看到"前端在跑但实际没进展"。主项目自带真实进度（phase/total/completed/current_stock）。
# ============================================================================

def _main_task(task_id: str):
    """从主项目 _tasks 注册表取任务 dict；取不到返回 None。"""
    try:
        _hot_mod = _load_main_hot_analysis()
        return _hot_mod._tasks.get(task_id)
    except Exception:
        return None


def _persist_async_result(task_id: str, result: dict) -> None:
    """主项目 async 任务完成/取消时，把结果落库到 v2 历史库（与 /run 同步一致）。

    ★ 本轮修复(M3)：失败必须可观测 —— 之前 except 仅 logger.warning 把异常吞掉，
      用户在前端看到"分析完成"，但历史库没记录、且 SSE 早已断开，彻底失去结果。
      现在改为 logger.exception 保留 traceback，并在任务字典上记录 persist_failed / persist_error，
      /hot-analysis/tasks/{task_id} 返回会带上字段供前端感知"分析成功但归档失败"。
    """
    try:
        if not isinstance(result, dict) or not result:
            return
        # ★ 本轮修复：started_at 可能为 None（主项目异常路径 result 仅含 status/error），
        #   schema 是 NOT NULL；优先用任务字典里的 started_at（init 时一定 set），
        #   再退回 result 字段，最后才用 now —— 三级兜底确保 INSERT 不撞 NOT NULL。
        task = _main_task(task_id) or {}
        started_at = (
            result.get("started_at")
            or task.get("started_at")
            or datetime.now().isoformat()
        )
        db_history.save_analysis_run(
            started_at=started_at,
            finished_at=result.get("finished_at", datetime.now().isoformat()),
            duration_seconds=result.get("duration_seconds", 0),
            status=result.get("status", "ok"),
            hot_lists_summary=result.get("hot_lists_summary", {}),
            hot_lists_status=result.get("hot_lists_status", {}),
            hot_lists_notes=result.get("hot_lists_notes", {}),
            raw_intersection_count=result.get("raw_intersection_count", 0),
            filtered_intersection_count=result.get("filtered_intersection_count", 0),
            analyzed_stock_count=result.get("analyzed_stock_count", 0),
            passed_stock_count=result.get("passed_stock_count", 0),
            filter_config=result.get("filter_config", {}),
            results=result.get("results", []),
            # ★ 迭代1：async 结果持久化 filtered_stocks（"未通过过滤"历史不丢失）
            filtered_stocks=result.get("filtered_stocks", []),
        )
    except Exception as e:
        # ★ 本轮修复：traceback 必须保留；写回任务字典让前端 GET /tasks/{task_id} 可见
        logger.exception("[async-persist] task %s save failed: %s", task_id, e)
        try:
            _hot_mod = _load_main_hot_analysis()
            t = _hot_mod._tasks.get(task_id)
            if t is not None:
                t["persist_failed"] = True
                t["persist_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        except Exception as inner_e:
            logger.warning("[async-persist] task %s write-back failed: %s", task_id, inner_e)


async def _watch_main_task_persist(task_id: str, timeout: float = 5400) -> None:
    """轮询主项目任务直到终态，然后落库到 v2（SSE 客户端中途断开的兜底）。"""
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = _main_task(task_id)
            if task is None:
                return
            if task.get("phase") == "completed" or task.get("status") in ("ok", "failed", "cancelled", "timeout"):
                result = task.get("result")
                if isinstance(result, dict):
                    _persist_async_result(task_id, result)
                return
            await asyncio.sleep(3)
    except Exception as e:
        logger.warning("[async-watch] task %s persist failed: %s", task_id, e)


@router.post("/hot-analysis/run-async")
async def hot_analysis_run_async(payload: dict, request: Request = None):
    """异步任务（返回 task_id）★ 委托主项目 run_async_analysis（真实进度推送）"""
    from core import ratelimit
    if request is not None:
        ip = ratelimit.get_client_ip(request)
        allowed, retry = ratelimit.check_limit(ip, "/api/v2/hot-analysis/run-async")
        if not allowed:
            raise HTTPException(429, f"限流，{retry}s 后重试")
    try:
        _hot_mod = _load_main_hot_analysis()
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
            max_concurrency=payload.get("max_concurrency", 1),
        )
        resp = await _hot_mod.run_async_analysis(req)
        task_id = resp.get("task_id")
        if task_id:
            # 后台兜底落库：即使 SSE 客户端中途断开也要写进 v2 历史库
            asyncio.create_task(_watch_main_task_persist(task_id))
        return resp
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("run-async failed: %s", e)
        raise HTTPException(500, f"启动失败: {str(e)[:200]}")


@router.get("/hot-analysis/run/{task_id}/stream")
async def hot_analysis_stream(task_id: str):
    """SSE 进度推送（★ 委托主项目 progress_queue；complete/partial 时落库到 v2）"""
    from fastapi.responses import StreamingResponse
    _hot_mod = _load_main_hot_analysis()
    task = _hot_mod._tasks.get(task_id)
    if not task:
        raise HTTPException(404, f"task {task_id} 不存在")
    queue: asyncio.Queue = task["progress_queue"]

    async def event_gen():
        try:
            # ★ Round 19 修复(M1)：任务已终态【且队列已空】时，直接回放 result 并结束，
            #   否则新连上的 SSE 客户端只会每 25s 收到心跳、永不结束（断线重连场景挂死）。
            #   注意必须判 queue.empty()：若队列里还有未消费的 partial/done 事件（如测试
            #   注入的假任务、或任务刚完成事件还没被消费），仍要正常消费，不能提前 return。
            queue = task["progress_queue"]
            if queue.empty() and (
                task.get("phase") == "completed"
                or task.get("status") in ("ok", "failed", "cancelled", "timeout")
            ):
                result = task.get("result")
                if isinstance(result, dict):
                    yield f"event: complete\ndata: {json.dumps(result, default=str, ensure_ascii=False)}\n\n"
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25)
                except asyncio.TimeoutError:
                    # 心跳（25s：避免 corporate proxy idle-timeout 杀掉 EventSource）
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue
                if event.get("type") == "done":
                    break
                event_type = event.get("type", "progress")
                data = event.get("data", {})
                # ★ Round 15：落库统一由 run-async 后台 _watch_main_task_persist 负责，
                #   这里只转发，避免 SSE 与 watcher 双写产生重复历史记录
                yield f"event: {event_type}\ndata: {json.dumps(data, default=str, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            # 客户端断开
            logger.debug("SSE client disconnected for task_id=%s", task_id)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/hot-analysis/tasks/{task_id}")
async def hot_analysis_task_status(task_id: str):
    """轮询查询任务状态（★ 委托主项目 _tasks）"""
    _hot_mod = _load_main_hot_analysis()
    task = _hot_mod._tasks.get(task_id)
    if not task:
        raise HTTPException(404, f"task {task_id} 不存在")
    return {
        "task_id": task_id,
        "status": task.get("status"),
        "phase": task.get("phase"),
        "total_stocks": task.get("total_stocks", 0),
        "completed_stocks": task.get("completed_stocks", 0),
        "current_stock": task.get("current_stock", ""),
        "current_round": task.get("current_round", 0),
        "passed_count": task.get("passed_count", 0),
        "error": task.get("error"),
        "started_at": task.get("started_at"),
        "elapsed_seconds": task.get("elapsed_seconds", 0.0),
        "result": task.get("result"),
        # ★ 本轮修复(M3)：分析完成但落库失败时透传给前端，避免"假成功"
        "persist_failed": task.get("persist_failed", False),
        "persist_error": task.get("persist_error"),
    }


@router.post("/hot-analysis/run/{task_id}/cancel")
async def hot_analysis_task_cancel(task_id: str):
    """取消后台任务（★ 委托主项目 cancel_analysis_task）"""
    try:
        _hot_mod = _load_main_hot_analysis()
        resp = await _hot_mod.cancel_analysis_task(task_id)
        if resp.get("status") == "not_found":
            raise HTTPException(404, f"task {task_id} 不存在")
        return resp
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("cancel failed: %s", e)
        raise HTTPException(500, f"取消失败: {str(e)[:200]}")


# ============================================================================
# 历史 + 自选 + 模板 + 定时任务
# ============================================================================
@router.get("/hot-analysis/history")
def hot_analysis_history(limit: int = Query(50, le=200), offset: int = 0, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=200)):
    # 兼容两种调用：limit+offset（旧）或 page+page_size（新）
    # ★ Round 19 修复(H3)：旧启发式 (page_size==20 and page==1) 会命中前端默认参数
    #   导致第一页返回 50 条、后续页返回 20 条的分页错乱。
    #   现在仅当显式传了 offset>0 时才走 limit+offset（旧式），否则一律 page/page_size。
    if offset > 0:
        page_size_eff = max(limit, 1)
        page_eff = (offset // page_size_eff) + 1
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
    # ★ Round 49 修复：保存前校验 cron 表达式合法性（与 /cron/user-jobs 对齐）。
    #   旧实现无校验 → 非法 cron 静默入库，resync_user_scheduled_tasks 解析失败仅
    #   log warning，任务永不执行且用户无感知（保存时显示成功）。这里用
    #   APScheduler CronTrigger 解析做权威校验，失败返回 400。
    if isinstance(cron_expr, str) and cron_expr.strip():
        try:
            from apscheduler.triggers.cron import CronTrigger
            CronTrigger.from_crontab(cron_expr.strip())
        except Exception as _cron_err:
            raise HTTPException(400, f"cron 表达式无效: {type(_cron_err).__name__}: {_cron_err}")
    else:
        raise HTTPException(400, "cron 表达式格式错误（需非空字符串）")
    try:
        tid = db_history.save_scheduled_task(name, cron_expr, config, enabled, task_id)
    except ValueError as e:
        # ★ Round 21：save_scheduled_task 更新不存在的 task_id 时抛 ValueError → 400
        raise HTTPException(400, str(e))
    # ★ Round 20 升级：保存后同步 APScheduler，让定时任务按 cron 自动执行
    try:
        from core import cron as _cron_mod
        _cron_mod.resync_user_scheduled_tasks()
    except Exception as e:
        logger.warning("resync scheduled tasks failed: %s", e)
    return {"id": tid}


@router.delete("/hot-analysis/scheduled-tasks/{task_id}")
def hot_analysis_scheduled_tasks_delete(task_id: int):
    if not db_history.delete_scheduled_task(task_id):
        raise HTTPException(404, "task 不存在")
    # ★ Round 20 升级：删除后同步 APScheduler
    try:
        from core import cron as _cron_mod
        _cron_mod.resync_user_scheduled_tasks()
    except Exception as e:
        logger.warning("resync scheduled tasks failed: %s", e)
    return {"deleted": task_id}


async def _watch_scheduled_persist(sched_task_id: int, analysis_task_id: str, timeout: float = 7200) -> None:
    """轮询主项目分析任务，终态时落库 v2 + 更新定时任务 last_status。"""
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = _main_task(analysis_task_id)
            if task is None:
                return
            if task.get("phase") == "completed" or task.get("status") in ("ok", "failed", "cancelled", "timeout"):
                result = task.get("result")
                if isinstance(result, dict):
                    _persist_async_result(analysis_task_id, result)
                    db_history.update_task_run(sched_task_id, result.get("status", "ok"))
                return
            await asyncio.sleep(3)
    except Exception as e:
        logger.warning("[sched-watch] task %s persist failed: %s", analysis_task_id, e)


def _spawn_scheduled_watcher(sched_task_id: int, analysis_task_id: str) -> None:
    """把定时任务的落库 watcher 放到独立守护线程。

    ★ 修复(H1/H2)：旧实现直接在 async 函数里 asyncio.create_task(_watch_scheduled_persist)。
    手动触发（FastAPI 主 loop）没问题；但 APScheduler 的 _run_user_scheduled_task 创建
    临时 event loop，run_until_complete 返回后立刻 loop.close() → watcher 是 pending task
    被销毁，分析结果永不落库。改为独立线程 + 自带 loop，脱离任何临时 loop 的生命周期。
    """
    import threading

    def _runner():
        try:
            asyncio.run(_watch_scheduled_persist(sched_task_id, analysis_task_id))
        except Exception as e:
            logger.exception("[sched-watch] runner crashed: %s", e)

    threading.Thread(target=_runner, daemon=True, name=f"sched-watch-{sched_task_id}").start()


@router.post("/hot-analysis/scheduled-tasks/{task_id}/run")
async def hot_analysis_scheduled_tasks_run(task_id: int):
    """手动触发定时任务（★ Round 15：委托主项目 run_async_analysis，非阻塞 + 真实进度）"""
    task = db_history.get_scheduled_task(task_id)
    if not task:
        raise HTTPException(404, "task 不存在")
    config = task.get("config", {}) or {}
    try:
        _hot_mod = _load_main_hot_analysis()
        from api.v1.schemas.hot_analysis import HotAnalysisRunRequest as _RunReq
        req = _RunReq(
            hot_list_n=config.get("hot_list_n", 200),
            max_intersection=config.get("max_intersection", 10),
            exclude_kechuang=config.get("exclude_kechuang", True),
            rounds=config.get("rounds", 3),
            max_runtime_seconds=config.get("max_runtime_seconds", 600),
            inter_round_sleep=config.get("inter_round_sleep", 0.6),
            simple_report=config.get("simple_report", True),
            llm_timeout_seconds=config.get("llm_timeout_seconds", 180),
            min_score=config.get("min_score", 60),
            allowed_advice=config.get("allowed_advice", ["买入", "加仓", "持有"]),
            exclude_keywords=config.get("exclude_keywords", ["卖出", "减仓"]),
            require_all_rounds=config.get("require_all_rounds", True),
            max_concurrency=config.get("max_concurrency", 1),
        )
        resp = await _hot_mod.run_async_analysis(req)
        main_task_id = resp.get("task_id")
        if main_task_id:
            # 后台兜底：完成后落库 v2 + 更新定时任务 last_status（独立线程，防 loop 销毁）
            _spawn_scheduled_watcher(task_id, main_task_id)
        db_history.update_task_run(task_id, "running")
        return {"started": task_id, "analysis_task_id": main_task_id}
    except Exception as e:
        logger.exception("scheduled trigger failed: %s", e)
        db_history.update_task_run(task_id, "error")
        raise HTTPException(500, f"触发失败: {str(e)[:200]}")


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
def market_review_generate_alias_post(payload: dict = {}, request: Request = None):
    # ★ Round 19 修复(后端C1)：alias 必须透传 request，否则限流逻辑（原生函数只认
    #   request 非 None 才生效）被完全跳过 —— 前端实际走的正是这个 alias 路径
    return market_review_generate(payload, request=request)


@router.get("/hot-analysis/market-review")
def market_review_list_alias(limit: int = Query(30, le=100), page: int = 1, page_size: int = Query(20, le=100), region: Optional[str] = None):
    """兼容 /hot-analysis/market-review（无 /list 后缀）的旧调用"""
    return market_review_list(limit=limit, page=page, page_size=page_size, region=region)


@router.get("/hot-analysis/market-review/list")
def market_review_list_alias2(limit: int = Query(30, le=100), page: int = 1, page_size: int = Query(20, le=100), region: Optional[str] = None):
    return market_review_list(limit=limit, page=page, page_size=page_size, region=region)


@router.get("/hot-analysis/market-review/latest")
def market_review_latest_alias(region: Optional[str] = None):
    return market_review_latest(region=region)


@router.post("/hot-analysis/market-review/compare")
def market_review_compare_alias(payload: dict):
    return market_review_compare(payload)


@router.get("/hot-analysis/market-review/compare")
def market_review_compare_alias_get(date_a: str, date_b: str):
    """GET 版对比（前端 compareMarketReviews 用 GET + query params）"""
    if date_a == date_b:
        raise HTTPException(400, "date_a 和 date_b 不能相同")
    a = db_history.get_market_review(date_a)
    b = db_history.get_market_review(date_b)
    if not a:
        raise HTTPException(404, f"复盘记录 {date_a} 不存在")
    if not b:
        raise HTTPException(404, f"复盘记录 {date_b} 不存在")
    return market_review_service.compare_two_reviews(a, b)


@router.post("/hot-analysis/market-review/batch")
async def market_review_batch_alias(payload: dict, request: Request = None):
    # ★ Round 19 修复(后端C1)：透传 request 使限流生效（同 generate alias）
    # ★ 修复(H6)：market_review_batch 已改 async，alias 必须同步改 async + await，
    #   否则同步 def 返回未执行的 coroutine（batch 永不执行）。
    return await market_review_batch(payload, request=request)


@router.get("/hot-analysis/market-review/{review_date}/export")
def market_review_export_alias(review_date: str):
    return market_review_export(review_date)


def _safe_review_date(review_date: str) -> str:
    """★ Round 4 修复：防止 review_date 被用于注入 header / 文件名"""
    import re
    m = re.fullmatch(r"\d{4}-\d{2}-\d{2}", review_date)
    if not m:
        raise HTTPException(400, "review_date 必须是 YYYY-MM-DD")
    return m.group(0)


@router.get("/hot-analysis/market-review/{review_date}/pdf")
def market_review_pdf_alias(review_date: str):
    """PDF 导出 — 委托给主项目端点

    ★ 修复：此前 `from api.v1.endpoints.hot_analysis import ...` 直连 import，
      但 backend/api 是 regular package，会遮蔽主项目 api 命名空间 → 在后端
      进程内必然 ImportError → 静默 fallback 成 markdown（用户拿到 .md 却
      以为导出了 PDF）。改用 _load_main_hot_analysis()（与 analyze_single /
      preview / run 同一套按路径加载机制），并把 fallback 缩窄到真正的
      reportlab 缺失场景、加 error 日志，不再静默降级。
    """
    review_date = _safe_review_date(review_date)
    review = db_history.get_market_review(review_date)
    if not review:
        raise HTTPException(404, f"{review_date} 复盘不存在")
    try:
        _hot_mod = _load_main_hot_analysis()
        pdf_bytes = _hot_mod._generate_market_review_pdf(review)
        from fastapi.responses import Response
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=market_review_{review_date}.pdf"},
        )
    except ImportError as e:
        logger.error("market_review_pdf_alias: reportlab 缺失，无法生成 PDF: %s", e)
        raise HTTPException(500, "PDF 生成需要 reportlab 库，请运行: pip install reportlab")


@router.get("/hot-analysis/market-review/{review_date}")
def market_review_get_alias(review_date: str):
    """按日期取复盘（前端 getMarketReviewByDate 用 GET）"""
    return market_review_get(review_date)


@router.delete("/hot-analysis/market-review/{review_date}")
def market_review_delete_alias(review_date: str):
    return market_review_delete(review_date)


# ---- 原生 /market-review/* 路径（保留） ----
# ★ Round 19 升级：大盘复盘生成去重 — 同一 date+region 的并发请求复用进行中的结果，
#   避免多个用户同时点生成 → 重复跑昂贵的全市场 akshare 抓取
_market_review_inflight_lock = threading.Lock()
_market_review_inflight: Dict[tuple, dict] = {}


@router.post("/market-review/generate")
def market_review_generate(payload: dict = {}, request: Request = None):
    """生成大盘复盘（限流 1/min/IP + 同 key 并发去重）"""
    from core import ratelimit
    if request is not None:
        ip = ratelimit.get_client_ip(request)
        allowed, retry = ratelimit.check_limit(ip, "/api/v2/market-review/generate")
        if not allowed:
            raise HTTPException(429, f"限流，{retry}s 后重试")
    review_date = payload.get("review_date")
    region = payload.get("region", "cn")
    key = (review_date, region)
    # ★ 本轮修复(M2)：之前 dict 永远空、if key in 恒为 False，去重完全 no-op。
    #   现在持锁期间写入 inflight 标记，finally 保证清理（避免一次异常把 key 永久留在 dict 里
    #   导致该日复盘再也生成不了）。值为元信息字典便于排障。
    with _market_review_inflight_lock:
        if key in _market_review_inflight:
            logger.info("market-review %s already generating, reusing in-flight result", key)
            return {"status": "in_progress", "review_date": review_date, "region": region,
                    "message": "该日复盘正在生成中，请稍后刷新查看"}
        _market_review_inflight[key] = {"started_at": time.time()}
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
    finally:
        # ★ 本轮修复(M2)：finally 保证任何退出路径（成功/异常/将来新增 return）
        #   都清理 inflight 标记，避免一次异常把 key 永久留在 dict 里
        with _market_review_inflight_lock:
            _market_review_inflight.pop(key, None)


@router.get("/market-review/latest")
def market_review_latest(region: Optional[str] = None):
    """最新复盘"""
    review = db_history.get_latest_market_review(region=region)
    if not review:
        raise HTTPException(404, "尚无复盘，请先调用 /market-review/generate")
    return review


@router.get("/market-review/list")
def market_review_list_with_suffix(limit: int = Query(30, le=100), page: int = 1, page_size: int = Query(20, le=100), region: Optional[str] = None):
    """★ Round 10：显式 /list 路由，必须在 /{review_date} 前声明（否则 {review_date}=list 占位）"""
    return market_review_list(limit=limit, page=page, page_size=page_size, region=region)


@router.get("/market-review")
def market_review_list(limit: int = Query(30, le=100), page: int = 1, page_size: Optional[int] = Query(None, le=100), region: Optional[str] = None):
    """复盘历史列表（兼容 limit 与 page_size 两种命名）

    返回字段与 v1 主项目 MarketReviewListResponse 一致：items + total + page + page_size。
    ★ Round 21 修复(M3)：旧启发式 (page_size != 20 else limit) 会把前端显式传的
      page_size=20 静默替换成 limit=30 → 分页错乱。改为 sentinel：仅当未显式传
      page_size 时才回退到 limit。
    """
    effective_size = page_size if page_size is not None else limit
    items, total = db_history.get_market_review_list(page=page, page_size=effective_size, region=region)
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
async def market_review_batch(payload: dict, request: Request = None):
    """批量生成近 N 天（限流 1/10min/IP）

    ★ 修复(H6)：旧实现同步 def 直接调 batch_generate_recent_reviews（分钟级全市场抓取），
    阻塞整个 event loop，所有其他请求排队。改 async + to_thread 委托线程池。
    """
    from core import ratelimit
    if request is not None:
        ip = ratelimit.get_client_ip(request)
        allowed, retry = ratelimit.check_limit(ip, "/api/v2/market-review/batch")
        if not allowed:
            raise HTTPException(429, f"限流，{retry}s 后重试")
    days = payload.get("days", 7)
    region = payload.get("region", "cn")
    persist = payload.get("persist", True)
    return await asyncio.to_thread(
        market_review_service.batch_generate_recent_reviews,
        days=days, region=region, persist=persist,
    )


# ============================================================================
# /health（v2 已有，保留即可）
# ============================================================================