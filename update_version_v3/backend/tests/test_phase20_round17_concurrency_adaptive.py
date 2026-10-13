# -*- coding: utf-8 -*-
"""Round 17 验证：自适应并发降级机制
1. AdaptiveThrottle：失败降级到串行 + 成功慢恢复 + 并发限制
2. DynamicAsyncLimiter：LLM 失败过多 → 分析并发降级
3. DataFetcherManager：数据源并发节流装饰器生效
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 主项目根（data_provider / api.v1.endpoints.hot_analysis）：测试文件在
#   update_version_v3/backend/tests/ → 向上 4 级即 F:/daily_stock_analysis_v2
_MAIN = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _MAIN not in sys.path:
    sys.path.insert(0, _MAIN)


# ============================================================================
# 1. AdaptiveThrottle
# ============================================================================
def test_throttle_degrades_to_serial_on_failures():
    from data_provider.adaptive_throttle import AdaptiveThrottle
    t = AdaptiveThrottle(initial_limit=3, failure_threshold=3, recovery_threshold=100)
    for _ in range(3):
        t.record_failure()
    assert t.current_limit() == 2
    for _ in range(3):
        t.record_failure()
    assert t.current_limit() == 1
    for _ in range(3):
        t.record_failure()
    assert t.current_limit() == 1, "已到最低并发，不应再降"
    print(f"OK throttle 降级到串行: limit={t.current_limit()}")


def test_throttle_recovers_slowly():
    from data_provider.adaptive_throttle import AdaptiveThrottle
    t = AdaptiveThrottle(initial_limit=3, failure_threshold=1, recovery_threshold=5)
    t.record_failure()  # -> 2
    assert t.current_limit() == 2
    for _ in range(5):
        t.record_success()
    assert t.current_limit() == 3
    print("OK throttle 成功慢恢复: 2->3")


def test_throttle_limits_concurrency_and_timeout_fallback():
    from data_provider.adaptive_throttle import AdaptiveThrottle
    t = AdaptiveThrottle(initial_limit=2, acquire_timeout=0.3)
    t.acquire()
    t.acquire()
    blocked = threading.Event()
    def worker():
        t.acquire()
        blocked.set()
        t.release()
    th = threading.Thread(target=worker)
    th.start()
    time.sleep(0.1)
    assert not blocked.is_set(), "第3个 acquire 应阻塞"
    t.release()
    th.join(timeout=2)
    assert blocked.is_set(), "释放后 worker 应获得许可"
    # 超时兜底
    t2 = AdaptiveThrottle(initial_limit=1, acquire_timeout=0.2)
    t2.acquire()
    t2.acquire()  # 应超时放行（不阻塞业务）
    t2.release()
    t2.release()
    print("OK throttle 并发限制 + 超时兜底")


# ============================================================================
# 2. DynamicAsyncLimiter（LLM 并发降级）
# ============================================================================
def test_dynamic_limiter_degrades_to_serial():
    import asyncio
    from api.v1.endpoints.hot_analysis import DynamicAsyncLimiter
    async def go():
        lim = DynamicAsyncLimiter(3)
        assert await lim.degrade() and lim.limit == 2
        assert await lim.degrade() and lim.limit == 1
        assert not await lim.degrade() and lim.limit == 1
        # async with 支持
        lim2 = DynamicAsyncLimiter(1)
        entered = 0
        async with lim2:
            entered += 1
        assert entered == 1
    asyncio.run(go())
    print("OK DynamicAsyncLimiter 降级到串行 + async with")


def test_dynamic_limiter_limits_concurrency():
    import asyncio
    from api.v1.endpoints.hot_analysis import DynamicAsyncLimiter
    async def go():
        lim = DynamicAsyncLimiter(2)
        await lim.acquire()
        await lim.acquire()
        got = []
        async def worker():
            await lim.acquire()
            got.append(True)
            await lim.release()
        t = asyncio.ensure_future(worker())
        await asyncio.sleep(0.05)
        assert not got
        await lim.release()
        await asyncio.sleep(0.05)
        assert got
    asyncio.run(go())
    print("OK DynamicAsyncLimiter 并发限制")


# ============================================================================
# 3. DataFetcherManager 数据源并发节流
# ============================================================================
def test_fetcher_manager_throttle_integration():
    from data_provider.base import DataFetcherManager
    m = DataFetcherManager([])
    m.set_data_concurrency(4)
    assert m.get_data_concurrency() == 4
    assert getattr(m.get_daily_data, "__wrapped__", None) is not None, "get_daily_data 未包节流器"
    assert getattr(m.get_chip_distribution, "__wrapped__", None) is not None, "get_chip_distribution 未包节流器"
    # 设置后重置失败计数
    m.set_data_concurrency(2)
    assert m.get_data_concurrency() == 2
    print("OK DataFetcherManager 节流器集成")


ALL_TESTS = [
    test_throttle_degrades_to_serial_on_failures,
    test_throttle_recovers_slowly,
    test_throttle_limits_concurrency_and_timeout_fallback,
    test_dynamic_limiter_degrades_to_serial,
    test_dynamic_limiter_limits_concurrency,
    test_fetcher_manager_throttle_integration,
]


def run_all():
    passed = 0
    failed = 0
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n=== Round 17 adaptive concurrency: {passed} PASSED, {failed} FAILED ===")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
