# -*- coding: utf-8 -*-
"""
update_version_v2 FastAPI app
- 启动入口
- ★ Round 4 升级：request_id 中间件、/ready 健康检查、APP_CORS_ORIGINS 强校验
"""
import json
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
from api.endpoints_technical import router as technical_router

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
    # ★ Round 19 修复(M7)：cron DB 用绝对路径，避免从仓库根启动时
    #   sqlite:///data/v2_dev.db 相对 CWD 解析到错误位置，导致任务存储与业务 DB 分离
    cron_mod.init(db_path=str(BACKEND_DIR / "data" / "v2_dev.db"))
    # ★ Round 20 升级：把 history.db 的 scheduled_tasks（enabled=1）注册进调度器，
    #   让用户"定时任务"真正按 cron 自动执行（此前只存不跑）
    try:
        cron_mod.register_user_scheduled_tasks()
    except Exception as e:
        logger.warning("register user scheduled tasks failed: %s", e)
    # ★ 本轮新增：应用启动补跑当日大盘复盘（若已过 10:00 且当天还没有成功记录）。
    #   异步线程执行，不阻塞启动；任何异常只 log，绝不让应用启动失败。
    try:
        cron_mod.catchup_market_review_on_startup()
    except Exception as e:
        logger.warning("catchup market review on startup failed: %s", e)
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
    - APP_ENV=production 时强制 APP_CORS_ORIGINS 已设置；缺省则抛 RuntimeError 拒绝启动
      （★ Round 13：避免生产环境静默放行 CORS=* 的高危配置）
    """
    raw = os.environ.get("APP_CORS_ORIGINS", "*").strip()
    app_env = os.environ.get("APP_ENV", "development").lower()
    if not raw or raw == "*":
        if app_env == "production":
            raise RuntimeError(
                "APP_CORS_ORIGINS must be set in production (拒绝 CORS=* 启动)"
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
        # ★ Round 19 修复(M4)：放行 X-User-Id，否则跨域部署时携带该头会被 preflight 拒绝，
        #   服务端身份机制(_require_user_match)在跨域下彻底失效
        allow_headers=["Content-Type", "Authorization", "X-Request-ID", "X-Admin-Token", "X-Forwarded-For", "X-User-Id"],
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
    # 技术分析（波峰波谷 / 指标 / AI 解读）—— 从 stock_analysis/v1 移植
    app.include_router(technical_router, prefix="/api/v2", tags=["technical"])
    # 同时挂 /api/v1 别名（前端 /api/v1/hot-analysis/* 兼容路径）
    app.include_router(hot_router, prefix="/api/v1", tags=["v1-compat"])

    # ★ Round 19 修复(H4)：create_share 返回的 share_url=/s/{token} 需根级路由，
    #   放在 /api/v2 前缀之外（v2_router 挂在 /api/v2 下，/s/ 定义在里面会变 /api/v2/s/）。
    #   公开分享预览页：自包含 HTML，社区/SEO 引流可用。
    @app.get("/s/{token}")
    def public_share_page(token: str):
        from modules import community as cm
        from fastapi.responses import HTMLResponse
        from html import escape
        s = cm.resolve_share(token)
        if not s:
            return HTMLResponse(
                """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>分享不存在或已过期</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:80px;color:#666">
<h2>该分享不存在或已过期</h2><p><a href="/">返回首页</a></p></body></html>""",
                status_code=404,
            )
        # ★ 修复(XSS)：title / resource_id / created / content 全部 html.escape()
        #   旧实现直接 f-string 拼接用户输入，create_share(title='<script>...')
        #   可在公开页执行任意脚本（存储型 XSS）。
        title = escape(str(s.get("title") or "分析报告分享"))
        content = s.get("content") or s.get("resource_type") or ""
        resource_id = escape(str(s.get("resource_id", "")))
        created = escape(str(s.get("created_at", "")))
        rows_html = ""
        if isinstance(content, dict):
            rows_html = "".join(
                f"<tr><td style='padding:6px 12px;border:1px solid #e5e7eb'>{escape(str(k))}</td>"
                f"<td style='padding:6px 12px;border:1px solid #e5e7eb'>{escape(str(v) if not isinstance(v, (dict, list)) else json.dumps(v, ensure_ascii=False)[:200])}</td></tr>"
                for k, v in list(content.items())[:20]
            )
        elif isinstance(content, str) and content:
            rows_html = f"<tr><td colspan='2' style='padding:12px;border:1px solid #e5e7eb;white-space:pre-wrap'>{escape(content[:4000])}</td></tr>"
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{title}</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font-family:'Segoe UI',system-ui,sans-serif;background:#f8fafc;margin:0}}main{{max-width:720px;margin:32px auto;padding:0 16px}}h1{{font-size:20px}}table{{border-collapse:collapse;width:100%;background:#fff}}td{{font-size:13px;color:#334155}}</style>
</head><body><main><h1>📊 {title}</h1>
<p style="color:#64748b;font-size:13px">资源: {resource_id} · 创建于: {created}</p>
<table>{rows_html}</table>
<p style="color:#94a3b8;font-size:12px;margin-top:24px">由 四榜交集分析 分享</p></main></body></html>""")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8001, reload=False)