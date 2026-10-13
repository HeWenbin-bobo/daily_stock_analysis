"""缓存层单元测试。"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from services.cache import TTLCache


class TestTTLCache:
    """TTLCache 基本行为。"""

    def test_set_and_get(self):
        c: TTLCache[str] = TTLCache(default_ttl=10)
        c.set("key1", "value1")
        assert c.get("key1") == "value1"

    def test_miss_returns_none(self):
        c: TTLCache[str] = TTLCache(default_ttl=10)
        assert c.get("nonexistent") is None

    def test_ttl_expired(self):
        c: TTLCache[str] = TTLCache(default_ttl=0.05)  # 50ms
        c.set("k", "v")
        assert c.get("k") == "v"
        time.sleep(0.1)
        assert c.get("k") is None

    def test_lru_eviction(self):
        c: TTLCache[int] = TTLCache(max_size=3, default_ttl=60)
        c.set("a", 1)
        c.set("b", 2)
        c.set("c", 3)
        # 此时已满, 插入第 4 个应驱逐最早插入的 a
        c.set("d", 4)
        assert c.get("a") is None
        assert c.get("b") == 2
        assert c.get("c") == 3
        assert c.get("d") == 4

    def test_lru_access_moves_to_end(self):
        c: TTLCache[int] = TTLCache(max_size=3, default_ttl=60)
        c.set("a", 1)
        c.set("b", 2)
        c.set("c", 3)
        # 访问 a → a 移到末尾
        assert c.get("a") == 1
        # 插入 d → 驱逐 b (现在最久未用)
        c.set("d", 4)
        assert c.get("b") is None
        assert c.get("a") == 1
        assert c.get("c") == 3
        assert c.get("d") == 4

    def test_overwrite_existing_key(self):
        c: TTLCache[str] = TTLCache(default_ttl=60)
        c.set("k", "v1")
        c.set("k", "v2")
        assert c.get("k") == "v2"

    def test_clear_resets_stats(self):
        c: TTLCache[str] = TTLCache(default_ttl=60)
        c.set("k", "v")
        c.get("k")
        c.get("nonexistent")
        assert c.stats()["hits"] >= 1
        assert c.stats()["misses"] >= 1
        c.clear()
        assert c.stats()["hits"] == 0
        assert c.stats()["misses"] == 0

    def test_stats_hit_rate(self):
        c: TTLCache[str] = TTLCache(default_ttl=60)
        c.set("k", "v")
        c.get("k")  # hit
        c.get("k")  # hit
        c.get("none")  # miss
        s = c.stats()
        assert s["hits"] == 2
        assert s["misses"] == 1
        assert abs(s["hit_rate"] - 2/3) < 0.01

    def test_ttl_partial_expiry(self):
        """多个 key 设置不同 TTL, 部分过期后 stats 仍正确。"""
        c: TTLCache[str] = TTLCache(default_ttl=10)
        c.set("short", "x", ttl=0.05)
        c.set("long", "y", ttl=10)
        time.sleep(0.1)
        assert c.get("short") is None  # 过期
        assert c.get("long") == "y"    # 未过期

    def test_zero_ttl_immediate_expiry(self):
        c: TTLCache[str] = TTLCache()
        c.set("k", "v", ttl=0)
        time.sleep(0.01)
        assert c.get("k") is None

    def test_large_cache_lru(self):
        """超出 max_size 时应弹出最早插入的。"""
        c: TTLCache[int] = TTLCache(max_size=5, default_ttl=60)
        for i in range(10):
            c.set(f"k{i}", i)
        stats = c.stats()
        assert stats["size"] == 5
        # k0-k4 应被驱逐(最早)
        assert c.get("k0") is None
        assert c.get("k4") is None
        # k5-k9 应保留
        for i in range(5, 10):
            assert c.get(f"k{i}") == i

    def test_update_doesnt_increase_size(self):
        """重复 set 同一 key 不应增加 size。"""
        c: TTLCache[str] = TTLCache(default_ttl=60)
        c.set("k", "v1")
        c.set("k", "v2")
        c.set("k", "v3")
        assert c.stats()["size"] == 1
        assert c.get("k") == "v3"


class TestTTLCacheStatsConsistency:
    """stats() 必须在锁内返回一致快照 — 防御并发读对 _data / hits 的撕裂访问。"""

    def test_stats_consistent_under_sequential_ops(self):
        c: TTLCache[str] = TTLCache(max_size=10, default_ttl=60)
        c.set("a", "1")
        c.set("b", "2")
        c.get("a")  # hit
        c.get("missing")  # miss
        s = c.stats()
        assert s["size"] == 2
        assert s["hits"] == 1
        assert s["misses"] == 1
        # hit_rate 应基于 hits+misses 计算, 不能用过期 hits
        assert abs(s["hit_rate"] - 0.5) < 0.01

    def test_stats_consistent_under_concurrent_writes(self):
        """并发 set / get 完成后, stats 字段必须彼此相容。

        具体不变量:
          - hits + misses == get 调用总数 (除了 clear 等显式重置)
          - hit_rate 应在 [0, 1] 之间
          - size 应 <= max_size
        """
        c: TTLCache[int] = TTLCache(max_size=64, default_ttl=60)
        n_threads = 6
        per_thread = 200
        barrier = threading.Barrier(n_threads)

        def worker(tid: int) -> None:
            barrier.wait()
            for i in range(per_thread):
                key = f"k{tid}-{i % 40}"  # 40 个唯一 key 产生重复 set/get
                c.set(key, i)
                c.get(key)
                # 部分 miss: 读不存在的 key
                if i % 17 == 0:
                    c.get(f"missing-{tid}-{i}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        s = c.stats()
        # 不变量断言
        assert s["size"] <= 64
        assert s["hits"] >= 0
        assert s["misses"] >= 0
        assert 0.0 <= s["hit_rate"] <= 1.0
        # size 必须是真实的字典大小快照 — 与 hits/misses 同时取出, 不应撕裂
        actual_size = len(c._data)
        assert s["size"] == actual_size

    def test_stats_does_not_raise_under_concurrent_get_set(self):
        """stats() 在并发 get/set 进行中时, 不能抛异常, 也不能死锁。"""
        c: TTLCache[str] = TTLCache(max_size=16, default_ttl=60)

        stop = threading.Event()

        def writer() -> None:
            i = 0
            while not stop.is_set():
                c.set(f"k{i % 8}", f"v{i}")
                i += 1

        def reader() -> None:
            i = 0
            while not stop.is_set():
                c.get(f"k{i % 8}")
                i += 1

        def stats_reader() -> None:
            while not stop.is_set():
                s = c.stats()
                # 各字段必须是合法值 (在锁内一致读出)
                assert s["size"] <= 16
                assert s["max_size"] == 16
                assert s["hits"] >= 0
                assert s["misses"] >= 0

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
            threading.Thread(target=stats_reader),
        ]
        for t in threads:
            t.start()
        time.sleep(0.3)
        stop.set()
        for t in threads:
            t.join()


class TestCacheIntegration:
    """缓存与 fetch_history 集成测试。"""

    def test_fetch_history_caches(self):
        """同一只股票/周期 二次调用应命中缓存, 不会重复产生日志。"""
        from services.data_provider import fetch_history

        # 第一次调用 - 写入缓存
        data1, source1, market1, name1 = fetch_history("600519", "daily", 60)
        assert len(data1) > 0

        # 第二次调用 - 应命中缓存
        data2, source2, market2, name2 = fetch_history("600519", "daily", 60)
        assert data2 is data1  # 同一个 list 对象(强一致)
        assert name1 == name2

    def test_different_periods_different_keys(self):
        from services.data_provider import fetch_history

        d1, _, _, _ = fetch_history("AAPL", "daily", 30)
        d2, _, _, _ = fetch_history("AAPL", "weekly", 30)
        # daily 和 weekly 是不同 cache key, 数据不同
        assert len(d1) != len(d2)


class TestQuantizeDays:
    """quantize_days() — iter 155 从 routers/stocks.py 迁到 services/cache.py。

    验证桶边界正确, 且跨 router 共享 utility 走 services 层的契约。
    """

    def test_quantize_to_bucket_below_first(self):
        """days=30 (PeakTroughRequest 最小值) 应量化到 60 (第一个桶)。"""
        from services.cache import quantize_days
        assert quantize_days(30) == 60

    def test_quantize_exact_bucket(self):
        """days=365 恰好等于桶值, 返回 365。"""
        from services.cache import quantize_days
        assert quantize_days(365) == 365

    def test_quantize_between_buckets(self):
        """days=400 在 365 和 500 之间, 应量化到 500。"""
        from services.cache import quantize_days
        assert quantize_days(400) == 500

    def test_quantize_above_max_bucket(self):
        """days=5000 超过最大桶 2000, 仍返回 2000 (上限)。"""
        from services.cache import quantize_days
        assert quantize_days(5000) == 2000

    def test_quantize_zero_or_negative_clamps_to_first_bucket(self):
        """days <= 0 应量化到第一个桶 (30 -> 60)。实际请求 days>=30 有 Pydantic 约束,
        但函数本身对非法输入不抛异常, 而是走最小桶。"""
        from services.cache import quantize_days
        assert quantize_days(0) == 60
        assert quantize_days(-100) == 60
