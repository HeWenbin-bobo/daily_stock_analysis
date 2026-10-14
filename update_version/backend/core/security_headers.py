# -*- coding: utf-8 -*-
"""安全 HTTP 头 + 风险提示尾部"""
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """添加安全相关 HTTP 响应头"""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        # HSTS（仅生产）
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        return response


# 每个 LLM 输出的分析报告底部必须强制加的提示
RISK_DISCLAIMER = (
    "\n\n---\n"
    "⚠️ **风险提示**：本报告由 AI 模型生成，仅供研究参考，不构成任何投资建议。"
    "投资有风险，入市需谨慎。过往业绩不代表未来表现。"
    "请根据自身风险承受能力和投资目标独立判断。"
)


def append_disclaimer(content: str) -> str:
    """给 LLM 输出追加风险提示（强制追加，包含去重逻辑）"""
    # 如果已有完整 disclaimer（包含"投资有风险"），不重复
    if "投资有风险" in content and "入市需谨慎" in content:
        return content
    # 如果只是部分有（如只是"不构成投资建议"），还是追加完整版
    return content + RISK_DISCLAIMER