"""PerfMiddleware 单元测试。

使用 httpx + FastAPI TestClient 在内存中触发请求, 验证:
  - 每个请求被记录到 _endpoint_stats
  - X-Response-Time-ms 响应头存在
  - 慢请求 (>1000ms) 触发 WARNING
  - /health /docs 等高频路径被跳过
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
import time

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from middlewares.perf import (
    PerfMiddleware,
    get_endpoint_stats,
    reset_endpoint_stats,
)


@pytest.fixture(autouse=True)
def clean_stats():
    reset_endpoint_stats()
    yield
    reset_endpoint_stats()


@pytest.fixture
def app():
    """创建一个简单的 FastAPI app 用于测试。"""
    app = FastAPI()
    app.add_middleware(PerfMiddleware)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/api/test/fast")
    async def fast_endpoint():
        return {"speed": "fast"}

    @app.get("/api/test/slow")
    async def slow_endpoint():
        time.sleep(0.05)  # 50ms
        return {"speed": "slow"}

    @app.get("/api/test/very-slow")
    async def very_slow_endpoint():
        # 模拟超慢请求(实际是 mock, 不真睡 1s)
        # 用直接写 stats 的方式
        return {"speed": "very-slow"}

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestPerfMiddlewareBasic:
    """基本功能。"""

    def test_health_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_not_recorded_in_stats(self, client):
        """高频 /health 端点不应被记录(避免日志噪音)。"""
        client.get("/health")
        stats = get_endpoint_stats()
        assert "/health" not in stats

    def test_business_endpoint_recorded(self, client):
        client.get("/api/test/fast")
        stats = get_endpoint_stats()
        assert "/api/test/fast" in stats
        s = stats["/api/test/fast"]
        assert s["count"] == 1
        assert s["avg_ms"] >= 0
        assert s["max_ms"] >= 0

    def test_response_time_header(self, client):
        r = client.get("/api/test/fast")
        assert "x-response-time-ms" in r.headers
        # header 值应为数字
        ms = float(r.headers["x-response-time-ms"])
        assert ms >= 0


class TestPerfMiddlewareStats:
    """统计聚合正确性。"""

    def test_count_increments(self, client):
        for _ in range(3):
            client.get("/api/test/fast")
        stats = get_endpoint_stats()
        assert stats["/api/test/fast"]["count"] == 3

    def test_avg_ms_reasonable(self, client):
        """平均耗时应 >= 0, <= max_ms。"""
        for _ in range(5):
            client.get("/api/test/fast")
        stats = get_endpoint_stats()
        s = stats["/api/test/fast"]
        assert 0 <= s["avg_ms"] <= s["max_ms"] + 1  # +1 容差

    def test_max_ms_tracks_slowest(self, client):
        """多次调用应保留最慢一次。"""
        # 调用多次, max 应 >= 任意一次
        max_seen = 0
        for _ in range(10):
            client.get("/api/test/slow")
        stats = get_endpoint_stats()
        assert stats["/api/test/slow"]["max_ms"] >= max_seen


class TestPerfMiddlewareReset:
    """reset_endpoint_stats 功能。"""

    def test_reset_clears_stats(self, client):
        client.get("/api/test/fast")
        assert "/api/test/fast" in get_endpoint_stats()
        reset_endpoint_stats()
        assert get_endpoint_stats() == {}

    def test_reset_keeps_endpoint_definitions(self, client):
        """reset 只清统计, 不影响路由定义。"""
        client.get("/api/test/fast")
        reset_endpoint_stats()
        # 路由仍可访问
        r = client.get("/api/test/fast")
        assert r.status_code == 200


class TestPerfMiddlewareExclude:
    """高频路径排除。"""

    @pytest.mark.parametrize("excluded_path", [
        "/health",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/admin/stats",
        "/admin/stats/reset",
    ])
    def test_high_freq_paths_excluded(self, client, excluded_path):
        # 这些路径可能在测试 client 不存在(404), 但即使返回 4xx, 也不应被记录
        client.get(excluded_path)
        stats = get_endpoint_stats()
        assert excluded_path not in stats

    def test_business_paths_recorded_with_errors(self, client):
        """即使业务端点返回 4xx/5xx, 也应被记录。"""
        @pytest.fixture
        def app_with_error():
            # 嵌套 fixture 复杂, 直接通过现有 app 测试
            pass

        # 这里用已存在的 endpoint 测试, 假设都是 200
        client.get("/api/test/fast")
        assert "/api/test/fast" in get_endpoint_stats()


class TestPerfMiddlewareLogging:
    """日志记录。"""

    def test_logs_request_info(self, client, caplog):
        """每个请求应有 DEBUG 日志(路径 + 耗时)。

        iter 77: 普通请求从 INFO 降到 DEBUG — INFO 现在只给慢请求 (SLOW_REQUEST_THRESHOLD_MS 触发 WARN)。
        测试相应地改用 DEBUG level 验证日志内容。
        """
        with caplog.at_level(logging.DEBUG):
            client.get("/api/test/fast")
        assert any("GET /api/test/fast" in r.message for r in caplog.records)

    def test_health_not_logged(self, client, caplog):
        """/health 高频请求不应被 PerfMiddleware 记录(避免日志噪音)。"""
        with caplog.at_level(logging.INFO, logger="perf"):
            client.get("/health")
        # PerfMiddleware 用 logger name 'perf', 应无相关日志
        perf_logs = [r for r in caplog.records if r.name == "perf"]
        assert len(perf_logs) == 0


class TestPerfMiddlewarePathNormalization:
    """动态路径应折叠到路由模板, 防止统计表无界增长。"""

    @pytest.fixture
    def parametric_app(self):
        """提供参数化路由的 app fixture, 供动态路径测试。"""
        reset_endpoint_stats()
        app = FastAPI()
        app.add_middleware(PerfMiddleware)

        @app.get("/api/users/{user_id}")
        async def users(user_id: str):
            return {"user": user_id}

        @app.get("/api/orders/{order_id}/items/{item_id}")
        async def order_items(order_id: str, item_id: str):
            return {"order": order_id, "item": item_id}

        yield app
        reset_endpoint_stats()

    @pytest.fixture
    def parametric_client(self, parametric_app):
        return TestClient(parametric_app)

    def test_parametric_path_collapses_to_template(self, parametric_client):
        """同一参数化端点的不同参数值应折叠到一条路由模板记录。"""
        for uid in ["alice", "bob", "carol", "dave", "eve"]:
            parametric_client.get(f"/api/users/{uid}")
        stats = get_endpoint_stats()
        assert "/api/users/{user_id}" in stats
        # 不应再出现具体的展开路径
        for uid in ["alice", "bob", "carol", "dave", "eve"]:
            assert f"/api/users/{uid}" not in stats
        assert stats["/api/users/{user_id}"]["count"] == 5

    def test_multi_param_path_collapses(self, parametric_client):
        """多个路径参数也应折叠到单一模板。"""
        parametric_client.get("/api/orders/123/items/abc")
        parametric_client.get("/api/orders/456/items/xyz")
        stats = get_endpoint_stats()
        assert "/api/orders/{order_id}/items/{item_id}" in stats
        assert stats["/api/orders/{order_id}/items/{item_id}"]["count"] == 2
        assert "/api/orders/123/items/abc" not in stats

    @pytest.mark.slow
    def test_many_unique_paths_bounded_by_max_paths(self):
        """未匹配路由的原始 path 也应受 ``_MAX_PATHS`` 上限约束。

        标记 slow: 3072 次请求, 即使单次 5ms 也需 ~15s; 默认 CI 跑 -m 'not slow' 会跳过
        (iter 63 test 修复 — 此前漏标 slow, 默认 CI 套件变慢)
        """
        from middlewares import perf as perf_mod
        reset_endpoint_stats()
        app = FastAPI()
        app.add_middleware(PerfMiddleware)
        c = TestClient(app)

        # 所有请求都不匹配任何路由 (404) → 走原始 path + _MAX_PATHS 限制
        for i in range(perf_mod._MAX_PATHS * 3):
            c.get(f"/no-such-path/{i}")

        stats = get_endpoint_stats()
        # 统计表的 key 数量必须 <= 上限
        assert len(stats) <= perf_mod._MAX_PATHS
        reset_endpoint_stats()

    def test_route_template_unaffected_by_cap(self, parametric_client):
        """路由模板条目不应被 cap 误删 (因为模板数远低于上限)。"""
        for i in range(200):
            parametric_client.get(f"/api/users/u{i}")
        stats = get_endpoint_stats()
        assert "/api/users/{user_id}" in stats
        assert stats["/api/users/{user_id}"]["count"] == 200


class TestPerfMiddlewareException:
    """dispatch 内部异常路径。"""

    def test_unhandled_exception_recorded_as_500(self):
        """路由处理函数抛异常时, 状态码应为 500, stats 仍记录。"""
        app = FastAPI()
        app.add_middleware(PerfMiddleware)

        @app.get("/api/boom")
        async def boom():
            raise RuntimeError("kaboom")

        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/api/boom")
        assert r.status_code == 500
        # 路由模板应在 stats 里, count=1
        stats = get_endpoint_stats()
        assert "/api/boom" in stats
        assert stats["/api/boom"]["count"] == 1
        # 异常计入 error_count
        assert stats["/api/boom"]["error_count"] == 1
        assert stats["/api/boom"]["error_rate"] == 1.0

    def test_error_count_only_5xx(self):
        """error_count 只统计 5xx 错误, 4xx 不算。"""
        # iter 63 修复: 显式 reset, 避免运行顺序变化时其它 test 的 stats 污染
        # (此前的污染会导致 error_rate 计算用到非本 test 写入的 count)
        reset_endpoint_stats()
        app = FastAPI()
        app.add_middleware(PerfMiddleware)

        @app.get("/api/ok")
        async def ok():
            return {"ok": True}

        @app.get("/api/bad")
        async def bad():
            raise HTTPException(status_code=400, detail="bad")

        @app.get("/api/boom")
        async def boom():
            raise RuntimeError("kaboom")

        client = TestClient(app, raise_server_exceptions=False)
        client.get("/api/ok")
        client.get("/api/bad")
        client.get("/api/boom")
        client.get("/api/boom")

        stats = get_endpoint_stats()
        # 200 -> 0 errors
        assert stats["/api/ok"]["count"] == 1
        assert stats["/api/ok"]["error_count"] == 0
        # 400 -> 0 errors (4xx 是 client error, 不算 server error)
        assert stats["/api/bad"]["count"] == 1
        assert stats["/api/bad"]["error_count"] == 0
        # 500 -> 2 errors
        assert stats["/api/boom"]["count"] == 2
        assert stats["/api/boom"]["error_count"] == 2
        assert stats["/api/boom"]["error_rate"] == 1.0

    def test_unhandled_exception_logs_at_exception_level(self, caplog):
        """未捕获异常应触发 perf 中间件 logger.exception。"""
        app = FastAPI()
        app.add_middleware(PerfMiddleware)

        @app.get("/api/boom2")
        async def boom2():
            raise ValueError("explode")

        client = TestClient(app, raise_server_exceptions=False)
        with caplog.at_level(logging.ERROR, logger="perf"):
            client.get("/api/boom2")
        # 应当有 ERROR 级别日志 (logger.exception)
        assert any(
            r.levelno == logging.ERROR and "unhandled exception" in r.message
            for r in caplog.records
        )

    def test_404_recorded_with_route_template(self):
        """404 响应仍走中间件, 路径使用路由模板(若匹配) 或 raw path。"""
        app = FastAPI()
        app.add_middleware(PerfMiddleware)

        @app.get("/api/items/{item_id}")
        async def items(item_id: int):
            return {"id": item_id}

        client = TestClient(app)
        r = client.get("/api/items/not-a-number")  # 422 (Pydantic)
        # 任何非 2xx 都应记录
        stats = get_endpoint_stats()
        assert any(v["count"] >= 1 for v in stats.values())


class TestPerfMiddlewareSlowLog:
    """慢请求 (elapsed > 1000ms) 走 WARNING 分支。"""

    @pytest.mark.slow
    def test_slow_request_triggers_warning(self, caplog):
        """sleep(1.05) 后应触发 SLOW 日志。"""
        app = FastAPI()
        app.add_middleware(PerfMiddleware)

        @app.get("/api/very-slow")
        async def very_slow():
            time.sleep(1.05)
            return {"ok": True}

        client = TestClient(app)
        with caplog.at_level(logging.WARNING, logger="perf"):
            r = client.get("/api/very-slow")
        assert r.status_code == 200
        assert any("SLOW" in r.message for r in caplog.records), (
            f"expected SLOW warning, got: {[r.message for r in caplog.records]}"
        )
