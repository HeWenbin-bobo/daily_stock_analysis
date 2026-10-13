# -*- coding: utf-8 -*-
"""Round 8 修复验证：K 线时间顺序（today 在最右） + 周期选择器（日/周/月/季/年）"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_mock_kline_daily_oldest_first():
    """★ Round 8：mock K 线循环 oldest→newest，today 应在最右"""
    from core import data_source as ds
    ds.clear_cache()
    kline = ds._mock_kline("600519", 30, "daily")
    assert len(kline) == 30
    first_date = kline[0]["date"]
    last_date = kline[-1]["date"]
    assert first_date < last_date, f"顺序反了: first={first_date}, last={last_date}"
    print(f"OK daily mock: first={first_date} (oldest), last={last_date} (today)")


def test_mock_kline_weekly_aggregates_5_days_per_bar():
    """★ Round 8：weekly 应把 5 天聚成一根"""
    from core import data_source as ds
    ds.clear_cache()
    kline = ds._mock_kline("600519", 120, "weekly")
    # 120 天 / 5 = 24 根
    assert 20 <= len(kline) <= 26, f"weekly bar count wrong: {len(kline)}"
    first_date = kline[0]["date"]
    last_date = kline[-1]["date"]
    assert first_date < last_date
    print(f"OK weekly mock: {len(kline)} bars, first={first_date}, last={last_date}")


def test_mock_kline_monthly_quarterly_yearly_aggregates():
    """★ Round 8：monthly / quarterly / yearly 聚合规则"""
    from core import data_source as ds
    ds.clear_cache()
    k_m = ds._mock_kline("600519", 250, "monthly")
    k_q = ds._mock_kline("600519", 500, "quarterly")
    k_y = ds._mock_kline("600519", 1000, "yearly")
    # 250/21 ≈ 12, 500/63 ≈ 8, 1000/252 ≈ 4 → max(10, ...) 兜底
    assert 10 <= len(k_m) <= 13, f"monthly bar count: {len(k_m)}"
    assert 10 <= len(k_q) <= 10, f"quarterly bar count: {len(k_q)}"
    assert 10 <= len(k_y) <= 10, f"yearly bar count: {len(k_y)}"
    # 每个都应 oldest first
    assert k_m[0]["date"] < k_m[-1]["date"]
    assert k_q[0]["date"] < k_q[-1]["date"]
    assert k_y[0]["date"] < k_y[-1]["date"]
    print(f"OK monthly={len(k_m)} quarterly={len(k_q)} yearly={len(k_y)} all oldest-first")


def test_fetch_kline_with_fallback_passes_period():
    """★ Round 8：fetch_kline_with_fallback 应把 period 传到 mock"""
    from core import data_source as ds
    ds.clear_cache()
    k_daily = ds.fetch_kline_with_fallback("600519", period="daily", days=120)
    k_weekly = ds.fetch_kline_with_fallback("600519", period="weekly", days=120)
    assert len(k_daily) == 120, f"daily should be 120 bars, got {len(k_daily)}"
    assert len(k_weekly) < len(k_daily), "weekly should have fewer bars than daily"
    assert len(k_weekly) >= 20, f"weekly should be ~24 bars, got {len(k_weekly)}"
    print(f"OK fetch_kline_with_fallback: daily={len(k_daily)} weekly={len(k_weekly)}")


def test_kline_api_supports_period_param():
    """★ Round 8：/api/v2/data/kline 应接受 period 参数"""
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)

    for period in ("daily", "weekly", "monthly", "quarterly", "yearly"):
        r = client.get(f"/api/v2/data/kline?code=600519&days=120&period={period}")
        assert r.status_code == 200, f"period={period} failed: {r.text}"
        data = r.json()
        assert data["period"] == period, f"Response period mismatch: {data}"
        kline = data["kline"]
        assert len(kline) > 0
        # 验证顺序：oldest first
        assert kline[0]["date"] < kline[-1]["date"], f"period={period} ordering wrong"
        print(f"OK /api/v2/data/kline?period={period}: {len(kline)} bars, oldest first")


def test_kline_api_rejects_invalid_period():
    """★ Round 8：非法 period 应 422"""
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)
    r = client.get("/api/v2/data/kline?code=600519&days=120&period=invalid")
    assert r.status_code == 422, f"invalid period should 422, got {r.status_code}"
    print("OK invalid period rejected with 422")


def test_kline_api_default_period_is_daily():
    """Round 8：不传 period 时默认 daily"""
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    client = TestClient(app)
    r = client.get("/api/v2/data/kline?code=600519&days=30")
    assert r.status_code == 200
    data = r.json()
    assert data["period"] == "daily"
    assert len(data["kline"]) == 30
    print("OK default period=daily (backward compat)")


def test_frontend_builds_with_round8():
    """Round 8：AdvancedViz 加 period 选择器后 build 必须成功"""
    import subprocess, shutil
    npx_cmd = shutil.which("npx") or shutil.which("npx.cmd") or "npx.cmd"
    r = subprocess.run(
        [npx_cmd, "vite", "build"],
        cwd=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "frontend"),
        capture_output=True, text=True, timeout=180,
    )
    assert r.returncode == 0, f"vite build failed:\n{r.stderr[-800:]}"
    print("OK frontend builds with Round 8 period selector")


ALL_TESTS = [
    test_mock_kline_daily_oldest_first,
    test_mock_kline_weekly_aggregates_5_days_per_bar,
    test_mock_kline_monthly_quarterly_yearly_aggregates,
    test_fetch_kline_with_fallback_passes_period,
    test_kline_api_supports_period_param,
    test_kline_api_rejects_invalid_period,
    test_kline_api_default_period_is_daily,
    test_frontend_builds_with_round8,
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
    print(f"\n=== Round 8 fixes+features: {passed} PASSED, {failed} FAILED ===")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
