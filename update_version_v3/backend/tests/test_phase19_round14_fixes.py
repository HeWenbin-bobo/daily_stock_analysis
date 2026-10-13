# -*- coding: utf-8 -*-
"""Round 14 修复验证：
1. 补全前端已引用但缺失的端点：compare / pdf-report / run/{id}/cancel / health / market-data
2. market-review GET by-date + GET compare（405 → 200）
3. SSE 事件名对齐（done → complete/partial）+ 取消流
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _v2_db():
    """用 importlib 按路径加载 v2 根目录 db/history.py（避免 backend/db 包抢占）"""
    import importlib.util
    from pathlib import Path
    _v2_root = Path(__file__).resolve().parent.parent.parent  # update_version_v3/
    spec = importlib.util.spec_from_file_location(
        "v2_db_history_test", _v2_root / "db" / "history.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_HOT_MOD = None  # 后端 api.endpoints_hot 模块对象（首次 _client() 时捕获；防止后续被主项目 api 包污染）


def _client():
    global _HOT_MOD
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    if _HOT_MOD is None:
        import sys
        _HOT_MOD = sys.modules.get("api.endpoints_hot")
    return TestClient(app)


# ============================================================================
# 1. 健康检查端点
# ============================================================================
def test_hot_analysis_health_ok():
    client = _client()
    r = client.get("/api/v1/hot-analysis/health")
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") in ("ok", "degraded")
    assert "akshare_available" in data
    print(f"OK /hot-analysis/health: status={data.get('status')}, akshare={data.get('akshare_available')}")


# ============================================================================
# 2. 对比分析（v2 历史库）
# ============================================================================
def _make_runs():
    db = _v2_db()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    r1 = db.save_analysis_run(
        started_at=now, finished_at=now, duration_seconds=1.0, status="ok",
        hot_lists_summary={"东方财富": 10}, hot_lists_status={}, hot_lists_notes={},
        raw_intersection_count=2, filtered_intersection_count=2,
        analyzed_stock_count=2, passed_stock_count=1,
        filter_config={},
        results=[
            {"code": "600519", "name": "贵州茅台", "in_sources": ["东方财富"],
             "analyses": [], "pass_reason": "ok", "avg_score": 80.0, "final_advice": "买入"},
            {"code": "000858", "name": "五粮液", "in_sources": ["东方财富"],
             "analyses": [], "pass_reason": "ok", "avg_score": 70.0, "final_advice": "持有"},
        ],
    )
    r2 = db.save_analysis_run(
        started_at=now, finished_at=now, duration_seconds=1.0, status="ok",
        hot_lists_summary={"东方财富": 10}, hot_lists_status={}, hot_lists_notes={},
        raw_intersection_count=2, filtered_intersection_count=2,
        analyzed_stock_count=2, passed_stock_count=2,
        filter_config={},
        results=[
            {"code": "600519", "name": "贵州茅台", "in_sources": ["东方财富"],
             "analyses": [], "pass_reason": "ok", "avg_score": 85.0, "final_advice": "加仓"},
            {"code": "300750", "name": "宁德时代", "in_sources": ["东方财富"],
             "analyses": [], "pass_reason": "ok", "avg_score": 75.0, "final_advice": "持有"},
        ],
    )
    return r1, r2


def test_compare_runs_returns_structured_diff():
    db = _v2_db()
    r1, r2 = _make_runs()
    try:
        client = _client()
        resp = client.post("/api/v1/hot-analysis/compare", json={"run_id_1": r1, "run_id_2": r2})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # r2 新增 300750（600519 共有但评分变化）
        assert "new_stocks" in data
        assert "removed_stocks" in data
        assert "score_changes" in data
        assert "300750" in data["new_stocks"], f"new_stocks={data['new_stocks']}"
        codes_changed = {c["code"] for c in data["score_changes"]}
        assert "600519" in codes_changed, f"score_changes={data['score_changes']}"
        print(f"OK /compare: new={data['new_stocks']}, changed={codes_changed}")
    finally:
        db.delete_analysis_run(r1)
        db.delete_analysis_run(r2)


def test_compare_requires_both_ids():
    client = _client()
    r = client.post("/api/v1/hot-analysis/compare", json={"run_id_1": 1})
    assert r.status_code == 400
    print(f"OK /compare validation: 400")


# ============================================================================
# 3. PDF 报告
# ============================================================================
def test_pdf_report_generates_valid_pdf():
    import base64
    db = _v2_db()
    r1, r2 = _make_runs()
    try:
        client = _client()
        resp = client.post("/api/v1/hot-analysis/pdf-report", json={"run_id": r1})
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") == "ok", f"pdf status={data.get('status')} error={data.get('error')}"
        pdf = base64.b64decode(data["pdf_base64"])
        assert pdf[:4] == b"%PDF", f"not a PDF: {pdf[:8]}"
        assert data["size_bytes"] == len(pdf)
        print(f"OK /pdf-report: {data['filename']} size={data['size_bytes']} bytes")
    finally:
        db.delete_analysis_run(r1)
        db.delete_analysis_run(r2)


# ============================================================================
# 4. 任务取消 + SSE partial 事件
# ============================================================================
def _main_hot():
    """返回主项目 api.v1.endpoints.hot_analysis 模块（async 任务机制所在）。"""
    assert _HOT_MOD is not None, "endpoints_hot 模块未被加载"
    return _HOT_MOD._load_main_hot_analysis()


def _fake_main_task(status: str, phase: str, events=None):
    """构造一个可直接插入主项目 _tasks 的假任务（带 progress_queue）。"""
    import asyncio as _aio
    q = _aio.Queue()
    for ev in (events or []):
        q.put_nowait(ev)
    return {
        "status": status, "phase": phase,
        "total_stocks": 0, "completed_stocks": 0, "current_stock": "",
        "current_round": 0, "passed_count": 0, "error": None,
        "started_at": "2026-08-05T00:00:00", "elapsed_seconds": 0.0,
        "progress_queue": q, "result": None,
    }


def test_cancel_marks_task_cancelled():
    main = _main_hot()
    main._tasks["test_task_1"] = _fake_main_task("running", "fetching")
    try:
        client = _client()
        r = client.post("/api/v1/hot-analysis/run/test_task_1/cancel")
        assert r.status_code == 200, r.text
        assert main._tasks["test_task_1"]["status"] == "cancelled"
        print("OK /run/{id}/cancel marks task cancelled (delegated to main project)")
    finally:
        main._tasks.pop("test_task_1", None)


def test_cancel_unknown_task_404():
    client = _client()
    r = client.post("/api/v1/hot-analysis/run/does_not_exist_xyz/cancel")
    assert r.status_code == 404
    print("OK /run/{id}/cancel unknown -> 404")


def test_sse_emits_partial_on_cancelled():
    db = _v2_db()
    items_before, _ = db.get_analysis_history(page=1, page_size=1)
    before_id = items_before[0]["id"] if items_before else 0
    main = _main_hot()
    main._tasks["test_task_sse"] = _fake_main_task(
        "cancelled", "completed",
        events=[
            {"type": "partial", "data": {
                "status": "cancelled", "results": [],
                "passed_stock_count": 0, "analyzed_stock_count": 0,
                "raw_intersection_count": 0, "filtered_intersection_count": 0,
                "started_at": "2026-08-05T00:00:00", "finished_at": "2026-08-05T00:00:01",
                "duration_seconds": 1.0, "hot_lists_summary": {}, "hot_lists_status": {},
                "hot_lists_notes": {}, "filter_config": {},
            }},
            {"type": "done"},
        ],
    )
    try:
        client = _client()
        with client.stream("GET", "/api/v1/hot-analysis/run/test_task_sse/stream") as r:
            assert r.status_code == 200
            body = b"".join(r.iter_bytes()).decode("utf-8", errors="replace")
        assert "event: partial" in body, f"missing partial event:\n{body}"
        assert '"status": "cancelled"' in body, body
        print("OK SSE emits partial event from main progress_queue")
    finally:
        main._tasks.pop("test_task_sse", None)
        # 清理本测试触发 _persist_async_result 落库的 cancelled 记录
        items_after, _ = db.get_analysis_history(page=1, page_size=50)
        for item in items_after:
            if item["id"] > before_id and item["status"] == "cancelled":
                db.delete_analysis_run(item["id"])


# ============================================================================
# 5. market-data
# ============================================================================
def test_market_data_returns_items():
    client = _client()
    r = client.get("/api/v1/hot-analysis/market-data?codes=600519")
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    print(f"OK /market-data: items={len(data['items'])}")


def test_market_data_rejects_over_50():
    client = _client()
    codes = ",".join(f"{600000+i}" for i in range(51))
    r = client.get(f"/api/v1/hot-analysis/market-data?codes={codes}")
    assert r.status_code == 400
    print("OK /market-data >50 -> 400")


# ============================================================================
# 6. market-review GET by-date + GET compare（此前 405）
# ============================================================================
def test_market_review_get_by_date_route_exists():
    client = _client()
    # 不存在的日期 → 404（说明路由存在，而非 405 Method Not Allowed）
    r = client.get("/api/v1/hot-analysis/market-review/2099-01-01")
    assert r.status_code == 404, f"expected 404 (route exists), got {r.status_code}"
    assert "复盘" in r.json().get("detail", "")
    print("OK market-review GET by-date route exists (404 for missing)")


def test_market_review_compare_get_route_exists():
    client = _client()
    r = client.get(
        "/api/v1/hot-analysis/market-review/compare?date_a=2099-01-01&date_b=2099-01-02"
    )
    assert r.status_code == 404, f"expected 404 (route exists), got {r.status_code}"
    print("OK market-review GET compare route exists (404 for missing)")


ALL_TESTS = [
    test_hot_analysis_health_ok,
    test_compare_runs_returns_structured_diff,
    test_compare_requires_both_ids,
    test_pdf_report_generates_valid_pdf,
    test_cancel_marks_task_cancelled,
    test_cancel_unknown_task_404,
    test_sse_emits_partial_on_cancelled,
    test_market_data_returns_items,
    test_market_data_rejects_over_50,
    test_market_review_get_by_date_route_exists,
    test_market_review_compare_get_route_exists,
]


def run_all():
    passed = 0
    failed = 0
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n=== Round 14 fixes: {passed} PASSED, {failed} FAILED ===")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
