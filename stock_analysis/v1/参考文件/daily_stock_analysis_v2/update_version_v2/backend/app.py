# -*- coding: utf-8 -*-
"""
update_version_v2 FastAPI app
- 启动入口
- ★ Round 4 升级：request_id 中间件、/ready 健康检查、APP_CORS_ORIGINS 强校验
"""
import logging
import os
import sys
import uuid
import time
from pathlib import Path
from typing import Any, Dict
from threading import Lock
from contextlib import asynccontextmanager

# 让 backend/ 作为 Python root
BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from core.security_headers import SecurityHeadersMiddleware
from core import cron as cron_mod
from core import data_source as ds
from api.endpoints_v2 import router as v2_router
from api.endpoints_hot import router as hot_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ★ Round 5：metrics — 进程内轻量计数器（无需引入 prometheus_client）
_metrics: Dict[str, int] = {}
_metrics_lock = Lock()


def _metrics_inc(key: str, n: int = 1) -> None:
    with _metrics_lock:
        _metrics[key] = _metrics.get(key, 0) + n


# ============================================================================
# ★ Round 5 新增：UserIDMiddleware — 统一从 X-User-Id header 读取服务端 user_id
# 取代（叠加）payload["user_id"] 信任。
# 工作模式：
#   - 有 X-User-Id header → 写入 request.state.user_id（信任）
#   - 无 header 时 → request.state.user_id = None（端点应拒绝伪造 user_id 的写请求）
#   - DELETE/PUT 端点可调用 _require_user_match() 强制 server-side user_id 与 payload 一致
# ============================================================================
class UserIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        header_uid = request.headers.get("X-User-Id")
        request.state.user_id = header_uid if header_uid else None
        response = await call_next(request)
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：替代 deprecated on_event"""
    cron_mod.init(db_path="data/v2_dev.db")
    logger.info("update_version_v2 started")
    yield
    cron_mod.shutdown()
    logger.info("update_version_v2 stopped")


# ============================================================================
# ★ Round 4 新增：request_id 中间件（便于跨模块日志关联）
# ============================================================================
class RequestIDMiddleware(BaseHTTPMiddleware):
    """注入 X-Request-ID（取自 header 或生成 uuid），写到 request.state 与响应头。

    不引入外部依赖；日志侧可后续用 logging.Filter 把 contextvars 注入。
    """

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception as e:
            logger.exception("[rid=%s] unhandled %s %s: %s", rid, request.method, request.url.path, e)
            return JSONResponse(
                status_code=500,
                content={"detail": "internal error", "request_id": rid},
            )
        response.headers["X-Request-ID"] = rid
        elapsed_ms = int((time.monotonic() - start) * 1000)
        # 仅记录慢请求或非 2xx，避免噪音
        if elapsed_ms > 1000 or response.status_code >= 400:
            logger.info(
                "[rid=%s] %s %s -> %s in %dms",
                rid, request.method, request.url.path, response.status_code, elapsed_ms,
            )
        return response


def _resolve_cors_origins() -> list:
    """解析 CORS 来源。

    - 优先级：APP_CORS_ORIGINS 环境变量
    - 默认（开发模式）：['*']
    - APP_ENV=production 时强制 APP_CORS_ORIGINS 已设置；缺省则告警并降级为 ['*']
      （不让启动失败，避免影响本地/CI 但留下明确日志）。
    """
    raw = os.environ.get("APP_CORS_ORIGINS", "*").strip()
    app_env = os.environ.get("APP_ENV", "development").lower()
    if not raw or raw == "*":
        if app_env == "production":
            logger.warning(
                "APP_CORS_ORIGINS unset or '*' in production; this is insecure. "
                "Set APP_CORS_ORIGINS=https://your.domain.com",
            )
        return ["*"]
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if not origins:
        return ["*"]
    return origins


def create_app() -> FastAPI:
    app = FastAPI(
        title="update_version_v2 API",
        version="2.0.0",
        description="可上线的产品级股票分析平台",
        lifespan=lifespan,
    )

    # 中间件顺序：最后添加的最先执行。
    # 我们希望 CORS 在最外层处理跨域，安全头在所有响应上一致生效，
    # request_id 最早拿到方法/路径/开始时间用于打日志。
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(UserIDMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    cors_origins = _resolve_cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        # 显式白名单而非 '*'，避免放开不必要的方法/头
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Request-ID", "X-Admin-Token", "X-Forwarded-For"],
    )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        rid = getattr(request.state, "request_id", "")
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "status_code": exc.status_code, "request_id": rid},
        )

    @app.get("/health")
    def health():
        """轻量探活（liveness）"""
        return {"status": "ok", "service": "update_version_v2", "ts": time.time()}

    @app.get("/ready")
    def ready():
        """就绪检查（readiness）：DB / cron / 数据源健康度"""
        checks: Dict[str, Any] = {}
        ok = True
        # 1) DB ping
        try:
            from db.migrate import get_conn
            conn = get_conn()
            conn.execute("SELECT 1").fetchone()
            checks["db"] = "ok"
        except Exception as e:
            checks["db"] = f"error: {str(e)[:80]}"
            ok = False
        # 2) cron scheduler
        try:
            checks["cron"] = "ok" if cron_mod._scheduler else "not_initialized"
            if not cron_mod._scheduler:
                ok = False
        except Exception:
            checks["cron"] = "error"
            ok = False
        # 3) data sources
        try:
            checks["data_sources"] = ds.get_health_report()
        except Exception as e:
            checks["data_sources"] = {"error": str(e)[:80]}
        # 4) cache stats
        try:
            checks["cache"] = ds.cache_stats()
        except Exception:
            pass
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ok" if ok else "degraded", "checks": checks},
        )

    @app.get("/metrics")
    def metrics():
        """★ Round 5：进程内 metrics（替代 prometheus_client 避免新增依赖）
        暴露内容：
        - cache_*：data_source 缓存命中/未命中/淘汰/命中率
        - llm_request / llm_fallback / llm_mock：LLM 调用次数
        """
        # 合并多来源 metrics
        from core import llm_router as llm_mod
        merged = dict(_metrics)
        merged.update(ds.get_metrics())
        merged.update(llm_mod.get_llm_metrics())
        return JSONResponse(content=merged)

    app.include_router(v2_router, prefix="/api/v2", tags=["v2"])
    app.include_router(hot_router, prefix="/api/v2", tags=["hot"])
    # 同时挂 /api/v1 别名（前端 /api/v1/hot-analysis/* 兼容路径）
    app.include_router(hot_router, prefix="/api/v1", tags=["v1-compat"])
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8001, reload=False)