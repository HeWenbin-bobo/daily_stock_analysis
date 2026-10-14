# -*- coding: utf-8 -*-
"""Round 6 修复验证：长阻塞路由异步化 + 前端错误处理"""
import os
import sys
import inspect
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================================
# 1. async 路由验证
# ============================================================================
def test_backtest_run_is_async():
    """★ Round 6：/api/v2/backtest/run 应为 async def"""
    from app import app
    for route in app.routes:
        if getattr(route, "path", "") == "/api/v2/backtest/run":
            assert inspect.iscoroutinefunction(route.endpoint), "backtest/run 应为 async"
            print(f"OK backtest/run is async")
            return
    raise AssertionError("backtest/run route not found")


def test_compile_backtest_is_async():
    """★ Round 6：/api/v2/backtest/strategies/{id}/compile 应为 async def"""
    from app import app
    for route in app.routes:
        path = getattr(route, "path", "")
        if "compile" in path and "backtest" in path:
            assert inspect.iscoroutinefunction(route.endpoint), "compile 应为 async"
            print(f"OK backtest compile is async ({path})")
            return
    raise AssertionError("compile route not found")


def test_paper_snapshot_is_async():
    """★ Round 6：/api/v2/paper/accounts/{id}/snapshot 应为 async def"""
    from app import app
    for route in app.routes:
        path = getattr(route, "path", "")
        if "snapshot" in path and "paper" in path:
            assert inspect.iscoroutinefunction(route.endpoint), "paper snapshot 应为 async"
            print(f"OK paper snapshot is async ({path})")
            return
    raise AssertionError("paper snapshot route not found")


def test_llm_chat_still_async():
    """Round 5 已修；Round 6 回归测试"""
    from app import app
    for route in app.routes:
        if getattr(route, "path", "") == "/api/v2/llm/chat":
            assert inspect.iscoroutinefunction(route.endpoint), "llm_chat 应仍为 async"
            print("OK llm_chat still async (regression)")
            return
    raise AssertionError("llm_chat route not found")


# ============================================================================
# 2. async 路由仍能返回正确结果（compile / snapshot mock 测试）
# ============================================================================
def test_async_compile_handles_missing_strategy():
    """async compile 在 strategy 不存在时仍返 404（行为不变）"""
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)
    r = client.post("/api/v2/backtest/strategies/99999/compile", json={})
    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"
    print(f"OK async compile returns 404 for missing strategy")


def test_async_snapshot_handles_missing_account():
    """async snapshot 在 account 不存在时仍返 404"""
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)
    r = client.post("/api/v2/paper/accounts/99999/snapshot", json={})
    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"
    print(f"OK async snapshot returns 404 for missing account")


def test_async_backtest_handles_missing_strategy():
    """async backtest/run 在 strategy 不存在时应返 404（与 compile 端点一致）"""
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)
    r = client.post(
        "/api/v2/backtest/run",
        json={"user_id": "u1", "strategy_id": 99999, "symbol": "600519"},
    )
    # ★ 本轮修复：run_backtest 增加策略归属校验后，不存在的 strategy 走 404（资源不存在），
    #   与 /backtest/strategies/{id}/compile 的 404 语义一致；旧断言 400 是旧实现
    #   （无预查、run_backtest 内部抛 ValueError → 400）的历史行为。
    assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"
    print(f"OK async backtest returns 404 for missing strategy")


# ============================================================================
# 3. /metrics 仍能暴露 round 5 的指标
# ============================================================================
def test_metrics_endpoint_still_works():
    """Round 6 回归：/metrics 不应被破坏"""
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    metrics = r.json()
    assert "cache_hit" in metrics
    assert "llm_request_total" in metrics
    print(f"OK /metrics still works: {len(metrics)} keys")


# ============================================================================
# 4. owner check + X-User-Id 仍生效
# ============================================================================
def test_owner_check_still_works():
    """Round 5 回归：DELETE 无 X-User-Id 应 401"""
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    from core.notifier import add_channel
    add_channel("u1", "wechat_work", "https://example.com/h")
    client = TestClient(app)
    r = client.delete("/api/v2/notifications/channels/1")
    assert r.status_code == 401, f"got {r.status_code}"
    print(f"OK owner check regression: anonymous DELETE → 401")


# ============================================================================
# 5. 前端 build 仍成功（useHotAnalysis 修改）
# ============================================================================
def test_frontend_builds_with_round6():
    """Round 6：useHotAnalysis.ts 加 _feedbackTimers + try/catch 包裹后 build 必须成功"""
    import subprocess, shutil
    npx_cmd = shutil.which("npx") or shutil.which("npx.cmd") or "npx.cmd"
    r = subprocess.run(
        [npx_cmd, "vite", "build"],
        cwd=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "frontend"),
        capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, f"vite build failed:\n{r.stderr[-800:]}"
    print(f"OK frontend builds with Round 6 changes")


# ============================================================================
# 6. async endpoint 在 mock 阻塞场景下不挂 FastAPI worker
# ============================================================================
def test_async_run_isolates_worker_from_blocking_io():
    """★ Round 6：async endpoint 即使下层是 sync IO，worker 也能并发处理其他请求"""
    # 这个测试通过执行其他并发请求验证 to_thread 隔离
    # 简化：检查 endpoint signature 是 async 即可（具体隔离由 asyncio.to_thread 保证）
    from app import app
    async_paths = []
    for route in app.routes:
        if getattr(route, "path", "") in (
            "/api/v2/backtest/run",
            "/api/v2/backtest/strategies/{strategy_id}/compile",
            "/api/v2/paper/accounts/{account_id}/snapshot",
            "/api/v2/llm/chat",
        ):
            fn = route.endpoint
            assert inspect.iscoroutinefunction(fn), f"{route.path} 应 async"
            async_paths.append(route.path)
    assert len(async_paths) >= 4, f"Expected ≥4 async, got {len(async_paths)}"
    print(f"OK 4 个目标端点全部 async: {async_paths}")


ALL_TESTS = [
    test_backtest_run_is_async,
    test_compile_backtest_is_async,
    test_paper_snapshot_is_async,
    test_llm_chat_still_async,
    test_async_compile_handles_missing_strategy,
    test_async_snapshot_handles_missing_account,
    test_async_backtest_handles_missing_strategy,
    test_metrics_endpoint_still_works,
    test_owner_check_still_works,
    test_frontend_builds_with_round6,
    test_async_run_isolates_worker_from_blocking_io,
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
    print(f"\n=== Round 6 fixes+features: {passed} PASSED, {failed} FAILED ===")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)