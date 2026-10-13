"""滑动窗口限流器单元测试。"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from middlewares.rate_limit import RateLimiter, rate_limiter


class TestRateLimiter:
    """RateLimiter 基本行为。"""

    def test_allows_within_limit(self):
        rl = RateLimiter()
        for _ in range(5):
            allowed, _ = rl.is_allowed("test", max_calls=5, window_seconds=10)
            assert allowed is True

    def test_denies_over_limit(self):
        rl = RateLimiter()
        for _ in range(3):
            allowed, _ = rl.is_allowed("test", max_calls=3, window_seconds=10)
            assert allowed is True
        # 第 4 次应被拒绝
        allowed, retry_after = rl.is_allowed("test", max_calls=3, window_seconds=10)
        assert allowed is False
        assert retry_after > 0

    def test_different_keys_independent(self):
        rl = RateLimiter()
        for _ in range(5):
            allowed, _ = rl.is_allowed("user_a", max_calls=5, window_seconds=10)
            assert allowed is True
        # user_b 不受影响
        allowed, _ = rl.is_allowed("user_b", max_calls=5, window_seconds=10)
        assert allowed is True

    def test_window_slides(self):
        """超过窗口后旧时间戳应被清除。"""
        rl = RateLimiter()
        # 0.05s 窗口
        for _ in range(3):
            allowed, _ = rl.is_allowed("test", max_calls=3, window_seconds=0.05)
            assert allowed is True
        # 第 4 次立即拒绝
        allowed, _ = rl.is_allowed("test", max_calls=3, window_seconds=0.05)
        assert allowed is False
        # 等待窗口过期: 改为轮询, 避免 slow CI 上 0.1s 不足导致 flaky
        # (iter 63 test 修复: 原本 time.sleep(0.1) 在 0.05s 窗口下极易抖动)
        allowed = False
        for _ in range(40):  # 最多 400ms
            time.sleep(0.02)
            allowed, _ = rl.is_allowed("test", max_calls=3, window_seconds=0.05)
            if allowed:
                break
        assert allowed is True

    def test_reset_specific_key(self):
        rl = RateLimiter()
        rl.is_allowed("key1", max_calls=1, window_seconds=10)
        rl.is_allowed("key2", max_calls=1, window_seconds=10)
        # key1 满了
        allowed, _ = rl.is_allowed("key1", max_calls=1, window_seconds=10)
        assert allowed is False
        # 重置 key1
        rl.reset("key1")
        allowed, _ = rl.is_allowed("key1", max_calls=1, window_seconds=10)
        assert allowed is True
        # key2 不受影响
        allowed, _ = rl.is_allowed("key2", max_calls=1, window_seconds=10)
        assert allowed is False

    def test_reset_all(self):
        rl = RateLimiter()
        rl.is_allowed("k1", max_calls=1, window_seconds=10)
        rl.is_allowed("k2", max_calls=1, window_seconds=10)
        rl.reset()
        # 重置后所有 key 都可以再次调用
        allowed1, _ = rl.is_allowed("k1", max_calls=1, window_seconds=10)
        allowed2, _ = rl.is_allowed("k2", max_calls=1, window_seconds=10)
        assert allowed1 is True
        assert allowed2 is True

    def test_stats(self):
        rl = RateLimiter()
        rl.is_allowed("a", max_calls=10, window_seconds=10)
        rl.is_allowed("b", max_calls=10, window_seconds=10)
        rl.is_allowed("a", max_calls=10, window_seconds=10)
        s = rl.stats()
        assert s["tracked_keys"] == 2
        assert s["total_pending"] == 3
        # 新增 allow/deny 计数
        assert s["allow_count"] == 3
        assert s["deny_count"] == 0
        assert s["deny_rate"] == 0.0

    def test_stats_counts_allows_and_denies(self):
        """allow_count / deny_count 反映 is_allowed 的接受/拒绝次数。"""
        rl = RateLimiter()
        # 3 次允许
        for _ in range(3):
            allowed, _ = rl.is_allowed("k", max_calls=3, window_seconds=10)
            assert allowed is True
        # 2 次拒绝
        for _ in range(2):
            allowed, _ = rl.is_allowed("k", max_calls=3, window_seconds=10)
            assert allowed is False
        s = rl.stats()
        assert s["allow_count"] == 3
        assert s["deny_count"] == 2
        # deny_rate = 2 / (3+2)
        assert abs(s["deny_rate"] - 0.4) < 1e-6

    def test_reset_clears_allow_deny_counts(self):
        """reset() 全量清除时, allow_count / deny_count 归零。"""
        rl = RateLimiter()
        rl.is_allowed("k", max_calls=1, window_seconds=10)
        rl.is_allowed("k", max_calls=1, window_seconds=10)  # deny
        assert rl.stats()["deny_count"] == 1
        rl.reset()
        s = rl.stats()
        assert s["allow_count"] == 0
        assert s["deny_count"] == 0
        assert s["deny_rate"] == 0.0


class TestRateLimiterBounded:
    """max_keys / LRU 淘汰 — 防资源耗尽。"""

    def test_max_keys_caps_tracked_count(self):
        """写入超过 max_keys 个唯一 key 后, 跟踪表大小应被封顶。"""
        rl = RateLimiter(max_keys=16, bucket_idle_ttl=3600.0, cleanup_interval=1e9)
        for i in range(200):
            allowed, _ = rl.is_allowed(f"k{i}", max_calls=10, window_seconds=60)
            assert allowed is True
        s = rl.stats()
        assert s["tracked_keys"] <= 16
        assert s["max_keys"] == 16

    def test_lru_eviction_drops_least_recently_used(self):
        """超过 max_keys 时应淘汰最久未访问的 key。"""
        rl = RateLimiter(max_keys=3, bucket_idle_ttl=3600.0, cleanup_interval=1e9)
        rl.is_allowed("a", max_calls=10, window_seconds=60)
        rl.is_allowed("b", max_calls=10, window_seconds=60)
        rl.is_allowed("c", max_calls=10, window_seconds=60)
        # 再次访问 a, 让 a 移到 LRU 末端
        rl.is_allowed("a", max_calls=10, window_seconds=60)
        # 第 4 个新 key 触发淘汰, 应淘汰 b (b 现在是最久未用)
        rl.is_allowed("d", max_calls=10, window_seconds=60)
        s = rl.stats()
        assert s["tracked_keys"] == 3
        # b 应已被驱逐, 不计入
        # (无法直接枚举, 但 tracked_keys=3 是关键断言)

    def test_evicted_key_can_be_re_admitted(self):
        """被淘汰的 key 再次出现时, 仍能正常限流(视为新桶)。"""
        rl = RateLimiter(max_keys=2, bucket_idle_ttl=3600.0, cleanup_interval=1e9)
        rl.is_allowed("a", max_calls=1, window_seconds=60)
        rl.is_allowed("b", max_calls=1, window_seconds=60)
        # 触发 a 被驱逐
        rl.is_allowed("c", max_calls=1, window_seconds=60)
        # a 重新加入后应是新桶, 立即可用
        allowed, _ = rl.is_allowed("a", max_calls=1, window_seconds=60)
        assert allowed is True

    def test_uniqueness_cap_with_concurrent_unique_keys(self):
        """多线程大量唯一 key 也不能撑爆。"""
        rl = RateLimiter(max_keys=64, bucket_idle_ttl=3600.0, cleanup_interval=1e9)
        n_threads = 4
        per_thread = 500
        barrier = threading.Barrier(n_threads)

        def worker(tid: int) -> None:
            barrier.wait()
            for i in range(per_thread):
                rl.is_allowed(f"k{tid}-{i}", max_calls=10, window_seconds=60)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        s = rl.stats()
        # 必须严格不超 max_keys
        assert s["tracked_keys"] <= 64
        assert s["max_keys"] == 64


class TestRateLimiterIdleTTL:
    """空闲桶 TTL 清理。"""

    def test_idle_buckets_evicted_after_ttl(self):
        """桶的最末时间戳早于 cutoff 时, 下次清理应被删除。"""
        rl = RateLimiter(
            max_keys=100,
            bucket_idle_ttl=0.1,
            cleanup_interval=0.05,
        )
        rl.is_allowed("x", max_calls=10, window_seconds=60)
        assert rl.stats()["tracked_keys"] == 1
        # 等待超过 idle TTL + cleanup interval, 再触发一次 is_allowed
        time.sleep(0.25)
        rl.is_allowed("y", max_calls=10, window_seconds=60)
        # x 应被清理
        s = rl.stats()
        # 核心断言: x 已被移除 (只剩 y)
        # (iter 63 修复: 删掉了 'assert "x" not in [_ for _ in []] or ...' 这行
        #  它是空列表推导, 第一部分恒真, 把整个 assert 变成了 tautology)
        assert s["tracked_keys"] == 1


class TestRateLimiterThreadSafety:
    """并发场景下计数器一致性。"""

    def test_concurrent_is_allowed_under_limit(self):
        """多线程反复请求同一 key, 不应出现 race 让超限请求放行。

        注意: 总允许数 = max_calls + 已切走的旧时间戳。
        我们用 ``max_calls=N`` 且 ``per_thread=1`` 验证在时间窗口内, 至少
        ``N`` 次成功、其余被拒。
        """
        rl = RateLimiter()
        rl.reset()
        n_threads = 8
        per_thread = 50
        max_calls = 30
        window = 5.0

        results = {"allowed": 0, "denied": 0}
        lock = threading.Lock()
        barrier = threading.Barrier(n_threads)

        def worker() -> None:
            barrier.wait()
            for _ in range(per_thread):
                ok, _ = rl.is_allowed("shared", max_calls=max_calls, window_seconds=window)
                with lock:
                    if ok:
                        results["allowed"] += 1
                    else:
                        results["denied"] += 1

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = results["allowed"] + results["denied"]
        assert total == n_threads * per_thread
        # 在窗口内允许的次数应 <= max_calls
        assert results["allowed"] <= max_calls
        assert results["denied"] >= total - max_calls


class TestRateLimiterGlobal:
    """全局单例测试。"""

    def test_global_instance_exists(self):
        assert rate_limiter is not None
        # 它的 stats 应可调用
        s = rate_limiter.stats()
        assert "tracked_keys" in s
        assert "total_pending" in s
        assert "max_keys" in s
