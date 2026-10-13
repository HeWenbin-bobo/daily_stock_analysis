"""进程内 滑动窗口 限流器, 保护昂贵的 LLM 端点。

每个 (key, window) 维护一个请求时间戳 deque:
  - 当新请求到来时, 删除早于 window 之前的时间戳
  - 如果剩余请求数 >= max_calls, 拒绝
  - 否则加入新时间戳并放行

线程安全 (Lock)。

资源保护 (避免被恶意/异常流量撑爆内存):
  - ``max_keys`` 上限: 跟踪的 key 总数受 LRU 淘汰约束
  - ``bucket_idle_ttl`` + ``cleanup_interval``: 周期性清理空闲桶

向后兼容: ``RateLimiter()`` 默认参数即可, 既有调用 (``is_allowed`` /
``reset`` / ``stats``) 的语义保持不变。``stats()`` 增加 ``max_keys`` 字段,
既有断言只校验 ``tracked_keys`` / ``total_pending``, 不会被破坏。
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict, deque
from threading import Lock
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class RateLimiter:
    """滑动窗口限流器, 支持总键数上限和空闲 TTL 清理。"""

    def __init__(
        self,
        max_keys: int = 10_000,
        bucket_idle_ttl: float = 3600.0,
        cleanup_interval: float = 60.0,
    ):
        self._buckets: "OrderedDict[str, Deque[float]]" = OrderedDict()
        self._lock = Lock()
        self._max_keys = max(1, int(max_keys))
        self._bucket_idle_ttl = max(0.0, float(bucket_idle_ttl))
        self._cleanup_interval = max(0.0, float(cleanup_interval))
        self._last_cleanup = time.monotonic()
        # 拒绝/放行计数(用于 /health 与 /admin/stats 观测)
        self._allow_count = 0
        self._deny_count = 0

    def _maybe_cleanup_locked(self, now: float) -> None:
        """在持有锁的前提下, 按 ``cleanup_interval`` 周期清扫空闲桶。

        桶的最后活动时间取自 deque 末尾(若为空则视为已空闲, 直接删除)。
        """
        if self._cleanup_interval <= 0:
            return
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        cutoff_active = now - self._bucket_idle_ttl
        for key in list(self._buckets.keys()):
            bucket = self._buckets[key]
            if not bucket or bucket[-1] < cutoff_active:
                self._buckets.pop(key, None)

    def is_allowed(
        self,
        key: str,
        max_calls: int,
        window_seconds: float,
    ) -> Tuple[bool, float]:
        """检查是否允许请求。

        Returns:
            (allowed, retry_after_seconds)
            - allowed: True 表示放行, False 表示被限流
            - retry_after_seconds: 当 allowed=False 时, 多久后可重试
        """
        now = time.monotonic()
        cutoff = now - window_seconds
        # 整个 body 在同一把锁内, check-then-act 原子:
        # 两个并发请求同时调用 is_allowed 时, 第二个必须等第一个释放锁,
        # 因此不可能同时通过 len(bucket) >= max_calls 检查又分别 append。
        with self._lock:
            self._maybe_cleanup_locked(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                # 新桶: 若达到上限则先驱逐最旧的 (OrderedDict 头部, 最近最少访问)
                if len(self._buckets) >= self._max_keys:
                    evicted_key, _ = self._buckets.popitem(last=False)
                    # iter 102: 降级到 DEBUG — warning 在持续高流量场景下会刷屏,
                    # 这是预期的 LRU 行为, 不是错误。运维若需排查可通过 log_level=DEBUG 看到。
                    logger.debug(
                        "rate_limit evict oldest key=%s (max_keys=%d reached)",
                        evicted_key, self._max_keys,
                    )
                bucket = deque()
                self._buckets[key] = bucket
            else:
                # 命中现有桶, 移到末尾 (LRU)
                self._buckets.move_to_end(key)
            # 删除窗口外的时间戳
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= max_calls:
                # retry_after = 最早时间戳过期的时间
                # iter 114: 最小 0.001s — 浮点精度让 bucket[0] + window == now 时返回 0.0,
                # 客户端看到 "请 0 秒后重试" 会立刻再发, 触发 429 风暴。0.001s (~1ms)
                # 客户端感知不到延迟, 但能让 Retry-After header 反映真实的等待。
                # iter 119: 降级为 DEBUG — WARNING 在持续攻击场景下会让攻击者 IP 的每个被限
                # 请求都产生一条 WARNING 日志 (e.g. 60 次/分钟 = 60 WARNING/分钟/IP),
                # 淹没真正需要关注的错误。429 响应本身已表达"被限流", 日志价值低。
                retry_after = max(0.001, bucket[0] + window_seconds - now)
                logger.debug(
                    "rate_limit DENY key=%s count=%d max=%d retry_after=%.1fs",
                    key, len(bucket), max_calls, retry_after,
                )
                self._deny_count += 1
                return False, retry_after
            bucket.append(now)
            self._allow_count += 1
            return True, 0.0

    def reset(self, key: Optional[str] = None) -> None:
        """清除限流状态。key=None 时清空所有。"""
        with self._lock:
            if key is None:
                self._buckets.clear()
                self._allow_count = 0
                self._deny_count = 0
            else:
                self._buckets.pop(key, None)

    def stats(self) -> dict:
        with self._lock:
            total = self._allow_count + self._deny_count
            deny_rate = self._deny_count / total if total > 0 else 0.0
            return {
                "tracked_keys": len(self._buckets),
                "total_pending": sum(len(b) for b in self._buckets.values()),
                "max_keys": self._max_keys,
                "allow_count": self._allow_count,
                "deny_count": self._deny_count,
                "deny_rate": round(deny_rate, 4),
            }


# 全局单例
rate_limiter = RateLimiter()
