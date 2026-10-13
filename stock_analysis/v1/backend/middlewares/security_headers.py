"""安全响应头中间件。

为每个响应添加基础安全头, 同时与 CORS 中间件共存。
"""
from __future__ import annotations

import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_CSP = "default-src 'self'; frame-ancestors 'self';"
_X_FRAME = "DENY"
_X_CONTENT = "nosniff"
# iter 71: 修正拼写 (原本是 _REF_ERRER)
_REFERRER = "strict-origin-when-cross-origin"


def _apply_headers(response) -> None:
    """统一为响应注入安全头(供 dispatch 与异常分支共用)."""
    response.headers["Content-Security-Policy"] = _CSP
    response.headers["X-Frame-Options"] = _X_FRAME
    response.headers["X-Content-Type-Options"] = _X_CONTENT
    response.headers["Referrer-Policy"] = _REFERRER


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """设置 Content-Security-Policy、X-Frame-Options、
    X-Content-Type-Options、Referrer-Policy。

    即使 ``call_next`` 抛出未捕获异常, 也会在异常分支构造 500 响应并
    补齐安全头, 确保所有出站响应都带同一组安全头。
    """

    async def dispatch(self, request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception as exc:
            # 走兜底: 异常被向外抛后, ServerErrorMiddleware 会再包一层;
            # 此处先构造 500 + 安全头, 避免安全头随异常路径丢失.
            # iter 90: logger.exception 保留完整 stack trace (之前的 exception
            # 异常被吞掉只构造 500 响应, 调试时看不到根因)。
            # iter 98: 加上 rid= 与 _RequestIDMiddleware 串联, 跨中间件关联日志。
            rid = getattr(request.state, "request_id", "-")
            logger.exception(
                "unhandled exception rid=%s (path=%s): %s",
                rid, request.url.path, type(exc).__name__,
            )
            response = JSONResponse(
                status_code=500,
                content={"detail": "服务器内部错误"},
            )
        _apply_headers(response)
        return response
