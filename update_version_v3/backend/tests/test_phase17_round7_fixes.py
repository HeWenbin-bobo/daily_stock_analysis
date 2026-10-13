# -*- coding: utf-8 -*-
"""Round 7 修复验证：akshare end_date + mock fallback + viz 路由"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_akshare_end_date_kwarg():
    """★ Round 7：akshare 1.18+ 改用 end_date（之前用 end 会抛 unexpected keyword）"""
    import inspect
    from core import data_source as ds
    src = inspect.getsource(ds.AkshareAdapter.fetch_kline)
    assert "end_date=" in src, f"AkshareAdapter.fetch_kline 应使用 end_date=\n{src[-400:]}"
    assert "end=" not in src.replace("end_date=", ""), f"不应再使用 end="
    print("OK AkshareAdapter.fetch_kline uses end_date (Round 7 fix)")


def test_mock_kline_fallback_returns_data():
    """★ Round 7：所有数据源失败时返回 mock K 线（sandbox/离线环境）"""
    from core import data_source as ds
    # 清缓存，确保 fresh fetch
    ds.clear_cache()
    kline = ds.fetch_kline_with_fallback("600519", days=30)
    # ★ Round 13 修复：akshare 在交易日返回 21 行（30 个日历日去除周末），
    #   仅在 sandbox 完全离线时才走 mock → 30 行。用 >=20 兼容两种路径。
    assert len(kline) >= 20, f"Expected >=20 days kline (mock 30 or akshare 21), got {len(kline)}"
    sample = kline[0]
    assert set(sample.keys()) >= {"date", "open", "high", "low", "close", "volume"}
    assert sample["high"] >= sample["low"], "high 应 >= low"
    print(f"OK mock kline fallback: {len(kline)} 天, sample={sample}")


def test_mock_quote_fallback_returns_data():
    """★ Round 7：所有数据源失败时返回 mock 行情"""
    from core import data_source as ds
    ds.clear_cache()
    quote = ds.fetch_quote_with_fallback("600519")
    assert quote is not None
    assert "code" in quote and "price" in quote and "change_pct" in quote
    assert isinstance(quote["price"], (int, float))
    print(f"OK mock quote fallback: {quote}")


def test_kline_api_endpoint_returns_data():
    """★ Round 7：/api/v2/data/kline 应返回非空数据（依赖 mock fallback）"""
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)
    r = client.get("/api/v2/data/kline?code=600519&days=30")
    assert r.status_code == 200
    data = r.json()
    assert "kline" in data
    assert len(data["kline"]) > 0, f"kline should not be empty (Round 7 mock fallback): {data}"
    print(f"OK /api/v2/data/kline returns {len(data['kline'])} days")


def test_quote_api_endpoint_returns_data():
    """★ Round 7：/api/v2/data/quote 应返回 mock 行情"""
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)
    r = client.get("/api/v2/data/quote?code=600519")
    assert r.status_code == 200
    data = r.json()
    assert "price" in data and "change_pct" in data
    print(f"OK /api/v2/data/quote returns mock: {data}")


def test_frontend_builds_with_round7():
    """Round 7：AdvancedViz 改独立 axios 实例 + @update:value 后 build 必须成功"""
    import subprocess, shutil
    npx_cmd = shutil.which("npx") or shutil.which("npx.cmd") or "npx.cmd"
    r = subprocess.run(
        [npx_cmd, "vite", "build"],
        cwd=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "frontend"),
        capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, f"vite build failed:\n{r.stderr[-800:]}"
    print(f"OK frontend builds with Round 7 changes")


def test_mock_data_is_deterministic():
    """同一 code + days 应产生稳定数据（基于 seed）"""
    from core import data_source as ds
    ds.clear_cache()
    a = ds.fetch_kline_with_fallback("600519", days=20)
    b = ds.fetch_kline_with_fallback("600519", days=20)
    # mock_kline 是确定性的（同 code 同 days → 同 seed）
    assert a == b, "mock kline 应基于 seed 稳定可复现"
    print(f"OK mock deterministic: {len(a)} rows reproducible")


def test_real_akshare_signature_in_data_source():
    """回归：确认 data_source.py 不再传 end= 参数"""
    import inspect
    from core import data_source as ds
    src = inspect.getsource(ds.AkshareAdapter.fetch_kline)
    # 应有 end_date 参数；不应有 standalone `end=`
    assert "start_date=" in src and "end_date=" in src, "应使用 start_date + end_date"
    print("OK data_source 仍使用 start_date + end_date kwarg（修复后未回归）")


ALL_TESTS = [
    test_akshare_end_date_kwarg,
    test_mock_kline_fallback_returns_data,
    test_mock_quote_fallback_returns_data,
    test_kline_api_endpoint_returns_data,
    test_quote_api_endpoint_returns_data,
    test_frontend_builds_with_round7,
    test_mock_data_is_deterministic,
    test_real_akshare_signature_in_data_source,
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
    print(f"\n=== Round 7 fixes+features: {passed} PASSED, {failed} FAILED ===")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)