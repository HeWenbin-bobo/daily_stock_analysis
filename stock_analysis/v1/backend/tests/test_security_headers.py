"""安全响应头集成测试 — 验证 SecurityHeadersMiddleware 为所有响应添加基础安全头。

使用 FastAPI TestClient 发起真实请求, 同时确认 CORS 头不被破坏。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os

# 在 import app 前强制 mock, 避免真实网络
os.environ["FORCE_MOCK"] = "1"

import pytest
from fastapi.testclient import TestClient

import app as app_module


@pytest.fixture
def client():
    return TestClient(app_module.app)


class TestSecurityHeaders:
    """验证安全响应头。"""

    def test_security_headers_on_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "Content-Security-Policy" in r.headers
        assert "default-src 'self'" in r.headers["Content-Security-Policy"]

    def test_security_headers_on_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_cors_still_works(self, client):
        """CORS 预检请求应成功并保留 CORS 头, 同时带安全头。"""
        r = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:5179",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )
        assert r.status_code == 200
        assert "access-control-allow-origin" in r.headers
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["X-Content-Type-Options"] == "nosniff"

    def test_security_headers_middleware_registered(self):
        """SecurityHeadersMiddleware 应已注册到 app。"""
        middleware_classes = [m.cls.__name__ for m in app_module.app.user_middleware]
        assert "SecurityHeadersMiddleware" in middleware_classes

    # iter 125: 补测 SecurityHeadersMiddleware 异常分支 (middlewares/security_headers.py:41-52)
    # — review 报告标注为 MEDIUM。正常情况下 FastAPI ServerErrorMiddleware 会拦截异常,
    # 但 SecurityHeadersMiddleware 自己仍要确保即使绕过 ServerErrorMiddleware,
    # 500 响应也带安全头 (防御性设计: 中间件顺序将来可能被改)。
    def test_unhandled_exception_returns_500_with_security_headers(self, monkeypatch):
        """call_next 抛异常时, SecurityHeadersMiddleware 应构造 500 + 安全头 + 不泄露根因。"""
        from middlewares.security_headers import SecurityHeadersMiddleware

        # 创建独立的 mini app, 用真实的 SecurityHeadersMiddleware 包裹一个会抛的路由
        from fastapi import FastAPI

        mini_app = FastAPI()

        @mini_app.get("/boom")
        def boom():
            raise RuntimeError("internal-secret-should-not-leak")

        # 关键: 把真实的 SecurityHeadersMiddleware 套在 mini_app 上
        # (不套的话 FastAPI 默认 ExceptionMiddleware 会截走 RuntimeError)
        wrapped_app = SecurityHeadersMiddleware(mini_app)
        c = TestClient(wrapped_app, raise_server_exceptions=False)

        r = c.get("/boom")
        assert r.status_code == 500
        # 4 个安全头都必须在 500 响应上
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "Content-Security-Policy" in r.headers
        # 响应内容不能包含原始异常文本 (iter 73 信息泄露防护)。
        # FastAPI ServerErrorMiddleware 可能先于 SecurityHeadersMiddleware 包装响应,
        # 所以响应体可能是 {"detail":"Internal Server Error"} 或 {"detail":"服务器内部错误"},
        # 但绝不能包含原始 "internal-secret-should-not-leak" 字符串。
        assert "internal-secret-should-not-leak" not in r.text
