"""FastAPI 入口。

启动: python app.py
或:   uvicorn app:app --host 0.0.0.0 --port 8100 --reload
(端口 8100 刻意避开参考项目 daily_stock_analysis_v2 的 8000)
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

# 加载 .env
ENV_PATH = Path(__file__).parent / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

# 配置日志
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("app")

from routers import stocks_router, analysis_router
from services.cache import analysis_cache, kline_cache
from security import require_admin
from middlewares.perf import (
    PerfMiddleware,
    get_endpoint_stats,
    reset_endpoint_stats,
)
from middlewares.rate_limit import rate_limiter
from middlewares.security_headers import SecurityHeadersMiddleware

BACKEND_VERSION = "1.0.0"


# iter 163: lifespan — 启动时后台预加载全量 A股列表 (akshare, ~5500 只), daemon 线程
# fire-and-forget 不阻塞启动。否则首次 /search 会触发 3-5s 网络拉取 (已用 to_thread
# 防阻塞事件循环, 但用户仍需等待; 预加载后首次搜索立即完成)。akshare 不可达时降级内置。
# 用 lifespan 替代已弃用的 @app.on_event("startup") (FastAPI 0.110+ deprecation)。
from contextlib import asynccontextmanager as _asynccontextmanager


@_asynccontextmanager
async def _lifespan(_app):
    import threading as _threading
    from services.data_provider import _get_cn_name_map
    _threading.Thread(target=_get_cn_name_map, name="cn-stock-list-preload", daemon=True).start()
    yield


app = FastAPI(
    title="Stock K-Line & Peak-Trough Analysis API",
    version=BACKEND_VERSION,
    description="多市场 K线 + 波峰波谷分析后端",
    lifespan=_lifespan,
)

# iter 70: TrustedHost middleware 阻止 Host header 攻击 (cache poisoning / 绕过 CORS)。
# iter 135: 默认仍为 ["*"] (本地开发), 但接受环境变量 ALLOWED_HOSTS 配置生产白名单。
# 多个域名逗号分隔, e.g. ALLOWED_HOSTS="api.example.com,example.com,localhost"。
# 未设置时维持 ["*"], 部署到生产前必须显式配置。
_allowed_hosts_env = os.environ.get("ALLOWED_HOSTS", "").strip()
_allowed_hosts = (
    [h.strip() for h in _allowed_hosts_env.split(",") if h.strip()]
    if _allowed_hosts_env
    else ["*"]
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=_allowed_hosts,  # 生产前必须通过 ALLOWED_HOSTS 环境变量配置白名单
)

# CORS - 允许前端 dev server (端口刻意避开参考项目的 5173)
# 注意: 不能与 allow_credentials=True 同时使用 "*", 那会被浏览器拒绝
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5179",
        "http://127.0.0.1:5179",
        "http://localhost:8100",
        "http://127.0.0.1:8100",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# 性能监控中间件(在 CORS 之后, 业务路由之前)
app.add_middleware(PerfMiddleware)

# 安全响应头中间件(最外层, 确保所有响应包括 CORS 预检都带安全头)
app.add_middleware(SecurityHeadersMiddleware)


# iter 66: 请求 ID 中间件 — 若客户端传 ``X-Request-ID`` 头则透传, 否则生成 uuid4
# 让前端报错时可附 request id, 运维日志中能按 id 串起来
import uuid as _uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as _StarletteRequest
from starlette.responses import Response


class _RequestIDMiddleware(BaseHTTPMiddleware):
    # iter 87: 显式 -> Response 返回类型 — BaseHTTPMiddleware 子类的 dispatch 约定
    # 返回 Response 对象, 加上类型让 mypy/IDE 能追溯到 starlette.middleware.base 基类。
    async def dispatch(self, request: _StarletteRequest, call_next) -> Response:
        rid = request.headers.get("x-request-id") or _uuid.uuid4().hex
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


app.add_middleware(_RequestIDMiddleware)

# 注: GZip 中间件不启用。BaseHTTPMiddleware 会缓存 streaming body, 导致 GZip
# 看不到原始响应 — 必须把 security_headers 改成纯 ASGI 才能解决 (iter 70 调研结论)。
# 当前未规划该重构, 注释掉保持代码意图清晰。

app.include_router(stocks_router)
app.include_router(analysis_router)


# iter 71: 把 Pydantic 校验错误 (422) 和异步取消 (CancelledError) 包成通用响应
# 默认 FastAPI 422 会暴露所有字段级错误信息 (字段名/类型), 生产环境不友好
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse as _JSONResponse


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request, exc: RequestValidationError):
    # 不向响应泄露内部字段路径 / 类型细节; 仅记日志供调试
    logger.warning(
        "request validation failed path=%s errors=%d",
        request.url.path, len(exc.errors()),
    )
    return _JSONResponse(
        status_code=422,
        content={"detail": "请求参数无效"},
    )


# 注: asyncio.CancelledError 在 Python 3.8+ 是 BaseException, 不是 Exception,
# FastAPI/Starlette 拒绝把它注册为 Exception handler (会触发 AssertionError)。
# 取消场景在中间件层 (SecurityHeadersMiddleware) 已自然返回空响应, 无需自定义。


@app.get("/")
async def root() -> dict:
    return {
        "name": "stock-analysis-backend",
        "version": BACKEND_VERSION,
        "endpoints": [
            "/health (full)",
            "/healthz (liveness)",
            "/ready (readiness)",
            "/api/v1/stocks/search?q=",
            "/api/v1/stocks/{code}/quote",
            "/api/v1/stocks/{code}/history?period=daily|weekly|monthly&days=N",
            "/api/v1/analysis/peak-trough",
            "/api/v1/analysis/llm-insight",
            "/api/v1/analysis/llm-status",
        ],
        # iter 102: force_mock 移到 /health (运维面板) — / 是未鉴权的对外端点,
        # 暴露 FORCE_MOCK 让任何人能探测当前是否走 mock。force_mock 仍可通过
        # /health (同样未鉴权, 但用户需要主动请求) 或 .env 文件查看。
        "llm_status_endpoint": "/api/v1/analysis/llm-status",
    }


@app.get("/health")
async def health() -> dict:
    """完整健康信息 — 供运维/dashboard 使用。

    ``/healthz`` (liveness) 与 ``/ready`` (readiness) 是更轻量的 K8s 探针专用端点 (iter 66)。
    """
    return {
        "status": "ok",
        "version": BACKEND_VERSION,
        # iter 103: force_mock 从 /health 移除 — 同样理由
        # iter 106: log_level 也移除 — 暴露 LOG_LEVEL 让外部能推断调试能力
        # (DEBUG 模式会打印 traceback 等敏感信息)。运维通过读进程环境变量 / 日志配置即可。
        "cache": {
            "kline": kline_cache.stats(),
            "analysis": analysis_cache.stats(),
        },
        "rate_limiter": rate_limiter.stats(),
    }


@app.get("/healthz")
async def healthz() -> dict:
    """Liveness probe: 进程是否存活。永远 200, 除非进程死了。"""
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict:
    """Readiness probe: 服务是否能承接流量。

    iter 74: 实际检查 cache 没爆 + 路由栈能解析 — 不查外部数据源 (避免探针触发网络请求)。
    """
    # 缓存满 → 已无去重能力, 命中率会从 99% 跌到 ~0%, 不应再接流量
    kline_full = kline_cache.stats()["size"] >= kline_cache.stats()["max_size"]
    analysis_full = analysis_cache.stats()["size"] >= analysis_cache.stats()["max_size"]
    ready_ok = not (kline_full and analysis_full)
    return {
        "status": "ready" if ready_ok else "degraded",
        "version": BACKEND_VERSION,
        "kline_cache_full": kline_full,
        "analysis_cache_full": analysis_full,
    }


@app.get("/admin/stats", dependencies=[Depends(require_admin)])
async def admin_stats() -> dict:
    """端点性能统计(供调试用, 生产可加鉴权)。

    默认本地开发放行; 配置 ADMIN_API_KEY 或部署到非 loopback 时强制鉴权。
    """
    return {
        "endpoints": get_endpoint_stats(),
        "cache": {
            "kline": kline_cache.stats(),
            "analysis": analysis_cache.stats(),
        },
        "rate_limiter": rate_limiter.stats(),
    }


@app.post("/admin/stats/reset", dependencies=[Depends(require_admin)])
async def admin_stats_reset() -> dict:
    """清零统计(同上, 受 admin 鉴权保护)。"""
    reset_endpoint_stats()
    kline_cache.clear()
    analysis_cache.clear()
    return {"status": "ok"}


# 启动时打印关键配置(便于排查)
# iter 105: FORCE_MOCK 改 DEBUG — 之前 INFO 暴露 mock 模式给所有能读 log 的人。
# 启动横幅 (=== stock-analysis-backend 启动 ===) 保持 INFO; LOG_LEVEL 也是 INFO 即可。
# FORCE_MOCK 仅运维排查时通过 --log-level=DEBUG 看。
logger.info("=== stock-analysis-backend 启动 ===")
logger.info("LOG_LEVEL=%s", LOG_LEVEL)
logger.debug("FORCE_MOCK=%s", os.environ.get("FORCE_MOCK", "1"))
# iter 91: 启动日志避免泄漏开发者用户名 — 只在显式设置了 CC_SWITCH_DIR env 时记录,
# 不再 fallback 到 C:\Users\jww\.cc-switch 这种"开发机用户名+路径"的组合。
if "CC_SWITCH_DIR" in os.environ:
    logger.info("CC_SWITCH_DIR=%s (from env)", os.environ["CC_SWITCH_DIR"])


# iter 66: 优雅关闭 — SIGTERM/SIGINT 时 uvicorn 自然停 listen + 等 in-flight 完成
import signal


def _install_signal_handlers() -> None:
    """注册 SIGTERM/SIGINT 处理器, 仅记日志; uvicorn 自己会真正关闭。"""
    def _handle(signum: int, _frame) -> None:  # type: ignore[no-redef]
        sig_name = signal.Signals(signum).name
        logger.warning("收到信号 %s, 开始优雅关闭 (in-flight 请求会完成)", sig_name)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError) as exc:
            # Windows 子线程 / 非主线程调用 signal.signal 会抛 ValueError;
            # 在某些沙箱里 (WASM / restricted) 会抛 OSError。忽略即可。
            # iter 90: DEBUG → WARNING — signal 注册失败意味着进程不会优雅关闭,
            # 运维需要知道, 不应藏在 DEBUG 级别。
            logger.warning("signal.signal 注册失败 (%s): %s", sig, exc)


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("BACKEND_HOST", "127.0.0.1")
    # 端口刻意避开参考项目 daily_stock_analysis_v2 的 WEBUI_PORT=8000 与默认 Vite 5173
    # iter 89: env var 解析容错 — BACKEND_PORT=abc 会让 int() 抛 ValueError 阻断启动,
    # 退到默认 8100 + WARNING 日志让运维能看出"为什么不是预期端口"。
    try:
        port = int(os.environ.get("BACKEND_PORT", "8100"))
    except ValueError:
        raw = os.environ.get("BACKEND_PORT", "")
        logger.warning("BACKEND_PORT=%r 不是合法整数, 退到默认 8100", raw)
        port = 8100
    _install_signal_handlers()
    # uvicorn 收到 SIGTERM 后会先停 listen, 再等当前请求完成, 再退出
    uvicorn.run("app:app", host=host, port=port, reload=False)