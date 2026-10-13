"""简单的进程内 TTL 缓存, 用于 K线/peak-trough 结果去重。

设计原则:
  - LRU + TTL 混合策略
  - 单进程使用, 不支持跨实例
  - key 格式: (stock_code, period, days)
  - 默认 TTL 5 分钟(数据源刷新频率低, 过短反而浪费缓存)
"""
from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock
from typing import Generic, Optional, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """LRU + TTL 缓存, 线程安全。"""

    def __init__(self, max_size: int = 128, default_ttl: float = 300.0):
        self._data: OrderedDict[str, tuple[float, T]] = OrderedDict()
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._lock = Lock()
        # 命中统计
        self.hits = 0
        self.misses = 0

    @property
    def default_ttl(self) -> float:
        """对外暴露的 TTL 访问器 (iter 72 — 之前用 _default_ttl 私有属性耦合)。"""
        return self._default_ttl

    def delete(self, key: str) -> bool:
        """显式删除单个 key; iter 72 — 之前直接 _data.pop() 绕过锁。"""
        with self._lock:
            return self._data.pop(key, None) is not None

    def get(self, key: str) -> Optional[T]:
        now = time.monotonic()
        with self._lock:
            if key not in self._data:
                self.misses += 1
                return None
            expires_at, value = self._data[key]
            if now > expires_at:
                # 已过期, 删除
                del self._data[key]
                self.misses += 1
                return None
            # 命中, 移到末尾(LRU)
            self._data.move_to_end(key)
            self.hits += 1
            return value

    def set(self, key: str, value: T, ttl: Optional[float] = None) -> None:
        with self._lock:
            ttl = ttl if ttl is not None else self._default_ttl
            expires_at = time.monotonic() + ttl
            # iter 108: 合并 if/else — 两分支 body 相同 (赋值 + move_to_end),
            # 只有"是否触发 LRU 驱逐"这一行为需要分支判断。原代码复制粘贴两遍,
            # 未来若 set 逻辑分叉容易漏改一边。改为: 始终先写, 再判断是否新增。
            existed = key in self._data
            self._data[key] = (expires_at, value)
            self._data.move_to_end(key)
            if not existed:
                # 新增, 检查容量; 已存在则覆盖, 不触发驱逐
                while len(self._data) > self._max_size:
                    self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> dict:
        # 全部读操作必须在锁内, 避免与并发 get/set 出现撕裂快照
        # (e.g. size 已更新但 hits/misses 未提交)
        with self._lock:
            total = self.hits + self.misses
            hit_rate = self.hits / total if total > 0 else 0.0
            return {
                "size": len(self._data),
                "max_size": self._max_size,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(hit_rate, 3),
            }


# 单例: 全局 K线缓存(避免对同一只股票/周期/天数反复拉取)
kline_cache: TTLCache = TTLCache(max_size=256, default_ttl=300.0)

# 单例: peak-trough 缓存
# TTL 与 kline_cache 保持一致: peak-trough 是 fetch_history 的纯函数派生, 数据没变结果就不变。
# iter 72: 用 default_ttl 公开属性而不是 _default_ttl 私有字段。
analysis_cache: TTLCache = TTLCache(max_size=128, default_ttl=kline_cache.default_ttl)


# iter 155: 从 routers/stocks.py 移到 services/cache.py — 这两个常量/函数专门服务
# 缓存键稳定性 (iter 110 window 量化, 这里的 days 量化), 不是路由职责。routers 之间
# 互相 import 私有函数 (routers/analysis.py:41 from routers.stocks import _quantize_days)
# 是紧耦合代码气味, 应当走共享 services 层。

# K线缓存的天数桶: 量化 days 到最近的桶, 避免用户传 365/366/364 拆成不同 cache key,
# 导致同一股票同一周期命中 3 个 cache 槽位。
_DAYS_BUCKETS: tuple[int, ...] = (60, 90, 120, 180, 250, 365, 500, 750, 1000, 1500, 2000)


def quantize_days(days: int) -> int:
    """把 days 量化到 ``_DAYS_BUCKETS`` 中最小的不小于 days 的桶。

    例: 35 -> 60, 365 -> 365, 3650 -> 2000。

    iter 155 改名为不带下划线 — 现在跨 router 共享, 不再是 router 内部 helper,
    公共 API 风格与其它 services/* 函数 (is_force_mock, fetch_history) 一致。
    """
    for bucket in _DAYS_BUCKETS:
        if days <= bucket:
            return bucket
    return _DAYS_BUCKETS[-1]
