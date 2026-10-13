"""性能监控中间件: 记录每个端点的响应时间 + 状态码 + 路径。

资源安全:
  - 当请求匹配到 FastAPI 路由时, 使用 ``request.scope['route'].path``
    (如 ``/api/v1/stocks/{stock_code}/history``) 作为统计 key,
    避免 ``/api/v1/stocks/600519/history`` 与 ``/api/v1/stocks/AAPL/history``
    各占一条目导致统计表无界增长。
  - 未匹配路由 (404 等) 使用原始 ``path``, 同时受 ``_MAX_PATHS`` 上限约束:
    超限后丢弃最早条目 (LRU)。
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from threading import Lock

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("perf")

# 默认 ``max_paths`` 软上限 — 仅作用于未匹配路由的原始 path,
# 路由模板本身是有限集合, 不受此约束。
_MAX_PATHS = 1024

# 慢请求阈值 (ms): 高于此值会触发 WARN 日志, 便于在线观察性能回归。
SLOW_REQUEST_THRESHOLD_MS = 1000.0

# 端点统计: path -> (count, total_ms, max_ms, error_count)
# 用 ``OrderedDict`` 实现 LRU 淘汰, 保证 ``_MAX_PATHS`` 硬上限
# iter 93: 用现代 PEP 585 语法 (tuple[int, ...]) 代替 typing.Tuple, 移除引号 forward reference —
# `from __future__ import annotations` 已启用, 但本行的引号是历史遗留, 与 `Tuple` 导入缺失不匹配。
_endpoint_stats: "OrderedDict[str, tuple[int, float, float, int]]" = OrderedDict()
_stats_lock = Lock()


def _record_stat(key: str, elapsed_ms: float, status_code: int) -> None:
    """线程安全地记录一次访问。"""
    with _stats_lock:
        existing = _endpoint_stats.get(key)
        if existing is None:
            # 容量控制: 超限丢弃 LRU 头部
            while len(_endpoint_stats) >= _MAX_PATHS:
                _endpoint_stats.popitem(last=False)
            _endpoint_stats[key] = (1, elapsed_ms, elapsed_ms, 1 if status_code >= 500 else 0)
            return
        count, total_ms, max_ms, err_count = existing
        _endpoint_stats[key] = (
            count + 1,
            total_ms + elapsed_ms,
            max(max_ms, elapsed_ms),
            err_count + (1 if status_code >= 500 else 0),
        )
        # 命中即提到 LRU 尾部
        _endpoint_stats.move_to_end(key)


def get_endpoint_stats() -> dict:
    """获取所有端点的统计快照(用于 /admin/stats)。

    返回数据已按 path 排序, 便于前端展示。
    """
    with _stats_lock:
        items = list(_endpoint_stats.items())
    out = {}
    for path, (count, total_ms, max_ms, err_count) in sorted(items):
        if count > 0:
            out[path] = {
                "count": count,
                "avg_ms": round(total_ms / count, 2),
                "max_ms": round(max_ms, 2),
                "total_ms": round(total_ms, 2),
                "error_count": err_count,
                "error_rate": round(err_count / count, 4),
            }
    return out


def reset_endpoint_stats() -> None:
    with _stats_lock:
        _endpoint_stats.clear()


def _resolve_stats_key(request: Request, raw_path: str) -> str:
    """优先使用路由模板 (``/users/{user_id}``), 回退到原始 path。

    Starlette 在 dispatch 过程中会写入 ``request.scope['route']``。
    但 ``BaseHTTPMiddleware`` 触发的 request 对象与路由解析在内部栈中,
    直接读取可能不可靠; 因此两者都尝试, 任意一个可用即可。
    """
    try:
        # request.scope 可能在内部转发时被替换, 取最后一个非空
        scope = request.scope
        # scope 永远是 starlette dict — 不需要 hasattr 兜底
        route = scope.get("route")
        if route is not None:
            template = getattr(route, "path", None)
            if isinstance(template, str) and template:
                return template
    except Exception as exc:
        # iter 79: 之前是裸 pass — 路由解析失败会被静默吞掉, 表现为
        # "明明有路由但 stats_key 仍是原始 path"。降级为 DEBUG 日志便于排查,
        # 同时仍回退到 raw_path (受 _MAX_PATHS LRU 上限约束, 不会无限增长)。
        logger.debug("perf route resolution failed for %s: %s", raw_path, exc)
    return raw_path


class PerfMiddleware(BaseHTTPMiddleware):
    """每个请求记录: 方法, 路径, 状态码, 耗时(ms)。"""

    async def dispatch(self, request: Request, call_next):
        # 跳过静态资源/文档/health/管理面板的高频请求以减少日志噪音
        path = request.url.path
        if path in (
            "/health",
            "/docs",
            "/openapi.json",
            "/redoc",
            "/admin/stats",
            "/admin/stats/reset",
        ):
            return await call_next(request)

        start = time.monotonic()
        try:
            response: Response = await call_next(request)
            status_code = response.status_code
        except Exception as exc:
            status_code = 500
            # iter 98: 异常日志带上 rid= 与下方 DEBUG/WARN 日志对齐, 便于跨中间件关联
            rid = getattr(request.state, "request_id", "-")
            logger.exception("unhandled exception rid=%s %s %s", rid, request.method, path)
            raise
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            # 优先用路由模板, 避免每个 user_id/token 各占一行造成无界增长
            # iter 64: 包裹在 try/except, 防止 stats 内部异常覆盖真实响应 (之前 finally 抛出会把 200 变成 500)
            try:
                stats_key = _resolve_stats_key(request, path)
                _record_stat(stats_key, elapsed_ms, status_code)
            except Exception as stats_exc:  # noqa: BLE001
                logger.warning("perf stats update failed for %s: %s", path, stats_exc)
            # 慢请求 WARN (阈值见 SLOW_REQUEST_THRESHOLD_MS)
            # iter 76: 把 request.state.request_id 带进日志, 与 _RequestIDMiddleware 串联
            rid = getattr(request.state, "request_id", "-")
            if elapsed_ms > SLOW_REQUEST_THRESHOLD_MS:
                logger.warning(
                    "SLOW rid=%s %s %s -> %d in %.1fms",
                    rid, request.method, stats_key, status_code, elapsed_ms,
                )
            else:
                # iter 77: 改 DEBUG — 之前 INFO 让每个成功请求都打一行, 高负载下刷屏
                # 慢请求 WARN 已捕获性能问题, stats 独立记录, 不需要每次 INFO 噪声
                logger.debug(
                    "rid=%s %s %s -> %d in %.1fms",
                    rid, request.method, stats_key, status_code, elapsed_ms,
                )

        # 添加响应头, 客户端可观察
        response.headers["X-Response-Time-ms"] = str(round(elapsed_ms, 2))
        return response
