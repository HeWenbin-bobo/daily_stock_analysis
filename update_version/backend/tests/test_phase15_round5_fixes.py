# -*- coding: utf-8 -*-
"""Round 5 修复验证：owner check / metrics / 异步化 / 前端响应式"""
import os
import sys
import asyncio
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrate import get_conn
from core import data_source as ds
from core import llm_router as llm_mod
from core import ratelimit


def _reset():
    conn = get_conn()
    for t in (
        "audit_log",
        "notification_channel",
        "notification_log",
        "paper_trade",
        "paper_account",
        "alert_rule",
        "alert_trigger",
        "paper_snapshot",
        "backtest_strategy",
        "backtest_result",
        "shared_report",
    ):
        try:
            conn.execute(f"DELETE FROM {t}")
        except Exception:
            pass


# ============================================================================
# 1. /metrics 端点 + cache hit/miss 计数
# ============================================================================
def test_metrics_endpoint_exposes_cache_and_llm():
    """★ Round 5：/metrics 端点应暴露 cache_* + llm_*"""
    _reset()
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)
    # 先用一下 cache 让计数变化
    ds._cache_set("k1", "v1")
    assert ds._cache_get("k1") == "v1"
    assert ds._cache_get("missing") is None
    # ★ 修复：显式强制走 mock 路径（不依赖环境）。
    #   此前断言 llm_mock_total >= 1 假设"无 API key → 必 mock"，
    #   但本机 CC switch 代理可用时真实调用成功，mock 计数为 0 → 断言被环境打崩。
    _orig = llm_mod._get_config
    llm_mod._get_config = lambda provider: None
    try:
        llm_mod.chat("test", task_complexity="simple")
    finally:
        llm_mod._get_config = _orig
    r = client.get("/metrics")
    assert r.status_code == 200
    metrics = r.json()
    assert "cache_hit" in metrics and metrics["cache_hit"] >= 1
    assert "cache_miss" in metrics and metrics["cache_miss"] >= 1
    assert "cache_hit_ratio" in metrics
    assert "llm_request_total" in metrics and metrics["llm_request_total"] >= 1
    assert "llm_mock_total" in metrics and metrics["llm_mock_total"] >= 1
    print(f"OK /metrics: cache_hit_ratio={metrics['cache_hit_ratio']}, "
          f"llm={metrics['llm_request_total']} req / {metrics['llm_mock_total']} mock")


def test_cache_evict_counter_increments():
    """★ Round 5：超过 maxsize 时应递增 _cache_evict"""
    import core.data_source as ds_mod
    ds_mod._CACHE_MAXSIZE = 5
    initial_evict = ds_mod._cache_evict
    try:
        for i in range(10):
            ds._cache_set(f"evict_test_{i}", i)
        assert ds_mod._cache_evict > initial_evict, "evict should increment"
        print(f"OK cache_evict counter incremented: +{ds_mod._cache_evict - initial_evict}")
    finally:
        ds_mod._CACHE_MAXSIZE = 2048
        ds.clear_cache()


# ============================================================================
# 2. UserIDMiddleware + owner check helper
# ============================================================================
def test_user_id_middleware_reads_header():
    """★ Round 5：UserIDMiddleware 应从 X-User-Id header 提取 user_id"""
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)
    # 通过调用一个会回显 user_id 的端点验证（/health 是基础）
    # 这里用 TestClient 的 request.state 不可见，但我们可以验证 middleware 不抛错
    r = client.get("/health", headers={"X-User-Id": "alice"})
    assert r.status_code == 200
    print("OK UserIDMiddleware accepts X-User-Id header")


def test_owner_check_anonymous_blocked():
    """★ Round 5：DELETE 不带 X-User-Id header 应被拒（401）"""
    _reset()
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)
    # 先插入一个 channel
    from core.notifier import add_channel
    add_channel("alice", "wechat_work", "https://example.com/hook")
    # 匿名 DELETE（无 X-User-Id）应该 401
    r = client.delete("/api/v2/notifications/channels/1")
    assert r.status_code == 401, f"Expected 401 without X-User-Id, got {r.status_code}: {r.text}"
    print(f"OK anonymous DELETE blocked (401)")


def test_owner_check_wrong_user_blocked():
    """★ Round 5：DELETE 带别人的 user_id 应 403（伪造）"""
    _reset()
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)
    from core.notifier import add_channel
    add_channel("alice", "wechat_work", "https://example.com/hook")
    # 带 X-User-Id=bob，但 payload user_id=alice（伪造）
    r = client.delete(
        "/api/v2/notifications/channels/1?user_id=alice",
        headers={"X-User-Id": "bob"},
    )
    assert r.status_code == 403, f"Expected 403 mismatched user, got {r.status_code}: {r.text}"
    print(f"OK mismatched user_id rejected (403)")


def test_owner_check_matching_user_succeeds():
    """★ Round 5：DELETE 带匹配的 user_id 应 200"""
    _reset()
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)
    from core.notifier import add_channel
    ch_id = add_channel("alice", "wechat_work", "https://example.com/hook")
    # 带 X-User-Id=alice，payload user_id=alice
    r = client.delete(
        f"/api/v2/notifications/channels/{ch_id}?user_id=alice",
        headers={"X-User-Id": "alice"},
    )
    assert r.status_code == 200, f"Expected 200 with matching user, got {r.status_code}: {r.text}"
    print(f"OK matching user DELETE succeeded (200)")


def test_owner_check_other_users_resource_403():
    """★ Round 5：DELETE 用对的 header 但删别人的资源 → 403"""
    _reset()
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)
    from core.notifier import add_channel
    ch_id = add_channel("alice", "wechat_work", "https://example.com/hook")
    # bob 想用 X-User-Id=bob + payload user_id=bob 删 alice 的 channel
    # → 走 owner 二次校验：channel.user_id != bob → 403
    r = client.delete(
        f"/api/v2/notifications/channels/{ch_id}?user_id=bob",
        headers={"X-User-Id": "bob"},
    )
    assert r.status_code == 403, f"Expected 403 cross-user, got {r.status_code}: {r.text}"
    print(f"OK cross-user delete blocked (403)")


# ============================================================================
# 3. async llm_chat endpoint
# ============================================================================
def test_llm_chat_endpoint_is_async():
    """★ Round 5：llm_chat 应是 async def（路由 inspect）"""
    import inspect
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    for route in app.routes:
        if getattr(route, "path", "") == "/api/v2/llm/chat":
            assert inspect.iscoroutinefunction(route.endpoint), "llm_chat 应为 async def"
            print(f"OK llm_chat is async: {route.endpoint.__name__}")
            return
    raise AssertionError("llm_chat endpoint not found")


def test_llm_chat_async_returns_mock():
    """async llm_chat 应仍能返回 mock 结果（强制无 API key 时）"""
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    # ★ 修复：显式强制走 mock 路径（不依赖环境）。
    #   此前断言 data["mocked"] is True 假设"无 API key → 必 mock"，
    #   但本机 CC switch 代理可用时真实调用成功（mocked=False）→ 断言被环境打崩。
    _orig = llm_mod._get_config
    llm_mod._get_config = lambda provider: None
    try:
        client = TestClient(app)
        r = client.post(
            "/api/v2/llm/chat",
            json={"prompt": "hello", "task_complexity": "simple"},
        )
        assert r.status_code == 200, f"got {r.status_code}: {r.text}"
        data = r.json()
        assert data["mocked"] is True
        print(f"OK async llm_chat returned mock: provider={data['provider']}")
    finally:
        llm_mod._get_config = _orig


# ============================================================================
# 4. LLM timeout 调到 20s
# ============================================================================
def test_llm_real_timeout_is_20s():
    """★ Round 5：_call_real urlopen timeout 应为 20s（不再 60s）"""
    import inspect
    src = inspect.getsource(llm_mod._call_real)
    assert "timeout=20" in src, f"_call_real 应使用 timeout=20, got: {src[-300:]}"
    print(f"OK LLM urlopen timeout=20s (was 60s)")


# ============================================================================
# 5. singleTasks 响应式（前端在 useHotAnalysis.ts）— 通过 build 验证
# ============================================================================
def test_frontend_builds_with_round5_fixes():
    """前端 build 必须成功（验证 Round 5 修改无语法错误）"""
    import subprocess
    import shutil
    npx_cmd = shutil.which("npx") or shutil.which("npx.cmd") or "npx.cmd"
    r = subprocess.run(
        [npx_cmd, "vite", "build"],
        cwd=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "frontend"),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert r.returncode == 0, f"vite build failed:\n{r.stderr[-1000:]}"
    print(f"OK frontend builds successfully (Round 5)")


# ============================================================================
# 6. _require_user_match helper 单元测试
# ============================================================================
def test_require_user_match_helper():
    """_require_user_match 单元行为"""
    from api.endpoints_v2 import _require_user_match
    from fastapi import Request

    class _Req:
        class _State:
            user_id = "alice"
        state = _State()
    req = _Req()
    # 匹配：不应抛
    _require_user_match(req, "alice")
    # 不匹配：应 403
    try:
        _require_user_match(req, "bob")
        raise AssertionError("expected 403")
    except Exception as e:
        assert "不一致" in str(e.detail) or "403" in str(getattr(e, "status_code", "")), f"got {e}"

    # 匿名（server_uid=None）→ 401
    class _AnonReq:
        class _State:
            user_id = None
        state = _State()
    try:
        _require_user_match(_AnonReq(), "alice")
        raise AssertionError("expected 401")
    except Exception as e:
        assert "401" in str(getattr(e, "status_code", "")), f"got {e}"
    print(f"OK _require_user_match: matching OK, mismatch 403, anonymous 401")


# ============================================================================
# runner
# ============================================================================
ALL_TESTS = [
    test_metrics_endpoint_exposes_cache_and_llm,
    test_cache_evict_counter_increments,
    test_user_id_middleware_reads_header,
    test_owner_check_anonymous_blocked,
    test_owner_check_wrong_user_blocked,
    test_owner_check_matching_user_succeeds,
    test_owner_check_other_users_resource_403,
    test_llm_chat_endpoint_is_async,
    test_llm_chat_async_returns_mock,
    test_llm_real_timeout_is_20s,
    test_frontend_builds_with_round5_fixes,
    test_require_user_match_helper,
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
    print(f"\n=== Round 5 fixes+features: {passed} PASSED, {failed} FAILED ===")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)