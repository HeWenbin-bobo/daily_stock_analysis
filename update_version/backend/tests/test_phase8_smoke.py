# -*- coding: utf-8 -*-
"""Phase 8 smoke test: 多数据源 + 健康度 + fallback + 缓存"""
import time
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrate import migrate, get_conn
from core import data_source as ds


def _reset_health_table():
    """测试前清空 data_source_health 表"""
    conn = get_conn()
    conn.execute("DELETE FROM data_source_health")


def test_migrate_includes_health_table():
    n = migrate()
    assert n >= 16
    print(f"OK migrate: {n} tables (includes data_source_health)")


def test_health_record_and_query():
    _reset_health_table()
    ds.update_health("test_source", success=True)
    h = ds.get_health("test_source")
    assert h == 1.0, f"Healthy source should be 1.0, got {h}"
    print(f"OK health: healthy source -> {h}")


def test_health_after_failure():
    _reset_health_table()
    ds.update_health("test_source", success=False, error="network timeout")
    h = ds.get_health("test_source")
    assert h == 0.3, f"After failure should drop to 0.3, got {h}"
    print(f"OK health: after failure -> {h}")


def test_health_recovers():
    _reset_health_table()
    ds.update_health("test_source", success=False, error="x")
    ds.update_health("test_source", success=True)
    h = ds.get_health("test_source")
    assert h == 0.7, f"After recovery should be 0.7, got {h}"
    print(f"OK health: after recovery -> {h}")


def test_fallback_chain_order():
    assert ds._FALLBACK_ORDER[0] == "akshare"
    assert "baostock" in ds._FALLBACK_ORDER
    print(f"OK fallback: {ds._FALLBACK_ORDER}")


def test_cache_set_and_get():
    ds.clear_cache()
    ds._cache_set("test_key", {"data": 123}, ttl=60)
    val = ds._cache_get("test_key")
    assert val == {"data": 123}, f"Cache should return value, got {val}"
    print("OK cache: set + get within TTL")


def test_cache_expires():
    ds.clear_cache()
    # 写入后手工把 ts 改成 70 秒前 → 模拟过期
    # ★ 本轮修复(测试)：_cache_set/_cache_get 的存储格式是 3 元组 (ts, value, ttl)，
    #   旧测试直接塞 2 元组 (ts, value)，_cache_get 解包崩在这里。
    import time as _t
    ds._cache["test_key_exp"] = (_t.time() - 70, {"data": 999}, 60)
    val = ds._cache_get("test_key_exp")
    assert val is None, f"Expired cache should be None, got {val}"
    print("OK cache: expired entry returns None")


def test_health_report():
    _reset_health_table()
    ds.update_health("akshare", success=True)
    ds.update_health("baostock", success=False, error="test")
    report = ds.get_health_report()
    assert "sources" in report and len(report["sources"]) >= 2
    assert report["primary"] == "akshare"
    print(f"OK health report: {len(report['sources'])} sources, primary={report['primary']}")


def test_fetch_quote_fallback_returns_mock_when_all_fail():
    # ★ Round 7 升级：所有源失败时不再返回 None，而是返回 mock 行情
    #   （保证 sandbox / 离线环境 viz、单股 tab 仍能展示）
    orig_order = ds._FALLBACK_ORDER[:]
    try:
        ds._FALLBACK_ORDER = ["nonexistent_source_1", "nonexistent_source_2"]
        ds._ADAPTERS.clear()
        result = ds.fetch_quote_with_fallback("600519")
        assert result is not None, "Round 7: 应返回 mock 行情（不再 None）"
        assert result.get("source") == "mock", f"应是 mock source, got {result}"
        assert result.get("code") == "600519"
        assert isinstance(result.get("price"), (int, float))
        print(f"OK fetch fallback: all failed -> mock quote ({result['name']} ¥{result['price']})")
    finally:
        ds._FALLBACK_ORDER = orig_order
        ds._ADAPTERS.clear()


if __name__ == "__main__":
    test_migrate_includes_health_table()
    test_health_record_and_query()
    test_health_after_failure()
    test_health_recovers()
    test_fallback_chain_order()
    test_cache_set_and_get()
    test_cache_expires()
    test_health_report()
    test_fetch_quote_fallback_returns_mock_when_all_fail()
    print("\n=== Phase 8 smoke test: ALL 9 PASSED ===")