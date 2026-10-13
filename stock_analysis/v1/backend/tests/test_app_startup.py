"""App 启动烟雾测试 — 验证 FastAPI 应用能正确加载且所有路由注册成功。

不发起实际 HTTP 请求,避免反复启动后端。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

# Force mock 避免依赖外部数据
import os
os.environ["FORCE_MOCK"] = "1"
os.environ.setdefault("BACKEND_PORT", "8100")


class TestAppStartup:
    """应用加载和路由注册。"""

    def test_app_loads(self):
        """App 模块能成功 import。"""
        import app
        assert app.app is not None

    def test_app_title(self):
        import app
        assert "Stock" in app.app.title

    def test_routers_registered(self):
        """所有 router 应正确挂载到 app。"""
        import app
        schema = app.app.openapi()
        paths = set(schema["paths"].keys())
        # 关键业务路径
        for segment in [
            "/api/v1/stocks/search",
            "/api/v1/stocks/{stock_code}/quote",
            "/api/v1/stocks/{stock_code}/history",
            "/api/v1/stocks/{stock_code}/indicators/{indicator}",
            "/api/v1/analysis/peak-trough",
            "/api/v1/analysis/llm-insight",
            "/api/v1/analysis/llm-status",
            "/health",
            "/admin/stats",
            "/admin/stats/reset",
            "/",
        ]:
            assert segment in paths, f"路径 {segment} 未注册 (现有: {sorted(paths)})"

    def test_cors_middleware_configured(self):
        """CORS 中间件应配置。"""
        import app
        middleware_classes = [m.cls.__name__ for m in app.app.user_middleware]
        assert "CORSMiddleware" in middleware_classes

    def test_perf_middleware_configured(self):
        """PerfMiddleware 应配置。"""
        import app
        middleware_classes = [m.cls.__name__ for m in app.app.user_middleware]
        assert "PerfMiddleware" in middleware_classes

    def test_openapi_schema_generated(self):
        """OpenAPI schema 应可生成。"""
        import app
        schema = app.app.openapi()
        assert schema["info"]["title"]
        assert "paths" in schema
        # 至少 10 个路径
        assert len(schema["paths"]) >= 10

    def test_health_in_openapi(self):
        """健康检查应在 OpenAPI 中。"""
        import app
        schema = app.app.openapi()
        assert "/health" in schema["paths"]

    def test_cors_origins_explicit(self):
        """CORS origin 不应包含 *, 与 credentials=True 冲突。"""
        import app
        cors_found = False
        for m in app.app.user_middleware:
            if m.cls.__name__ == "CORSMiddleware":
                cors_found = True
                opts = m.kwargs
                if "allow_origins" in opts:
                    origins = opts["allow_origins"]
                    if isinstance(origins, (list, tuple, set)):
                        assert "*" not in origins, "CORS 与 credentials 同用时不能用 *"
        assert cors_found, "CORSMiddleware 未配置"

    def test_no_hardcoded_api_keys_in_source(self):
        """代码中不应有硬编码的 LLM API key 模式。"""
        from pathlib import Path
        # 只检查核心 llm 模块
        from llm import cc_switch
        source = Path(cc_switch.__file__).read_text(encoding="utf-8")
        # 这些是真实写死的密钥才会触发的模式
        forbidden_patterns = [
            "sk-ant-api03-",  # Anthropic 真实 key 前缀
            "sk-prod-",
            "sk-live-",
        ]
        for pattern in forbidden_patterns:
            assert pattern not in source, f"硬编码疑似泄漏: {pattern}"

    def test_routers_have_tags(self):
        """每个业务 router 应有 OpenAPI tags。"""
        import app
        schema = app.app.openapi()
        # 检查每个 path 至少有 1 个方法, 每个方法有 tags
        # 排除 '/' / '/health' / '/admin/*' (这些端点没有 tags)
        excluded = ("/", "/health", "/admin")
        for path, methods in schema["paths"].items():
            if any(path.startswith(e) for e in excluded):
                continue
            for method, info in methods.items():
                if method in ("get", "post", "put", "delete"):
                    assert "tags" in info, f"{method.upper()} {path} 缺少 tags"
                    assert len(info["tags"]) >= 1

    def test_cors_origin_count(self):
        """CORS 应配置至少 1 个 origin(避免 wildcard 'origin: *')。"""
        import app
        for m in app.app.user_middleware:
            if m.cls.__name__ == "CORSMiddleware":
                origins = m.kwargs.get("allow_origins", [])
                if isinstance(origins, (list, tuple, set)):
                    assert len(origins) >= 1, "CORS 应配置具体 origin 白名单"
