# -*- coding: utf-8 -*-
"""回归：批量行情抓取必须有总时间预算，且只读端点不被上游拖垮。

背景（2026-10-16 实测）：社区热榜 GET /api/v2/community/hot-list 会为榜单里的
股票逐只拉实时行情。当上游数据源挂起时（akshare / baostock 各自 15s 超时），
30 只叠加会把 FastAPI 的同步端点线程池占满，导致所有业务 API 排队超时，
而 /health 这类异步端点仍秒回 —— 表现为"服务活着但全部接口不可用"。

修复后：fetch_quotes_bulk 接受 budget_seconds，到期即返回已拿到的部分，
不等待挂起的 future；社区热榜端点传入预算。
"""

import io
import sys
import time

sys.path.insert(0, ".")
# 只在直接运行时重定向 stdout；pytest 下接管会与它自己的 capture 冲突
# （ValueError: I/O operation on closed file）。
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from unittest.mock import patch

from core import data_source as ds


def test_bulk_fetch_returns_within_budget_when_upstream_hangs():
    """整批都挂起时，必须在预算内返回，而不是等到最慢的那个跑完。"""
    def hang(code):
        time.sleep(30)
        return {"code": code, "price": 1.0}

    with patch.object(ds, "fetch_quote_with_fallback", side_effect=hang):
        started = time.monotonic()
        result = ds.fetch_quotes_bulk(
            ["600519", "000001", "300750"], parallel=True, budget_seconds=2.0
        )
        elapsed = time.monotonic() - started

    assert elapsed < 4.0, f"应在预算内返回，实际 {elapsed:.2f}s"
    assert set(result) == {"600519", "000001", "300750"}, "缺失的 code 也要保留 key"
    assert all(v is None for v in result.values()), "挂起的 code 应为 None"
    print(f"OK 整批挂起时 {elapsed:.2f}s 返回（预算 2s）")


def test_bulk_fetch_keeps_fast_results_and_drops_slow_ones():
    """快慢混合时，快的结果要保留，慢的不阻塞。"""
    def mixed(code):
        time.sleep(0.3 if code.startswith("600") else 30)
        return {"code": code, "price": 9.9}

    with patch.object(ds, "fetch_quote_with_fallback", side_effect=mixed):
        started = time.monotonic()
        result = ds.fetch_quotes_bulk(
            ["600519", "600036", "000001", "300750"], parallel=True, budget_seconds=2.0
        )
        elapsed = time.monotonic() - started

    assert elapsed < 4.0, f"应在预算内返回，实际 {elapsed:.2f}s"
    assert result["600519"] is not None and result["600036"] is not None, "快的结果应保留"
    assert result["000001"] is None and result["300750"] is None, "慢的应为 None"
    print(f"OK 快慢混合 {elapsed:.2f}s 返回，保留 2 个快结果")


def test_bulk_fetch_without_budget_keeps_waiting_for_all():
    """不传预算时保持旧行为：全部完成后才返回。"""
    def quick(code):
        return {"code": code, "price": 1.0}

    with patch.object(ds, "fetch_quote_with_fallback", side_effect=quick):
        result = ds.fetch_quotes_bulk(["600519", "000001"], parallel=True)

    assert all(v is not None for v in result.values()), "不传预算时应拿到全部结果"
    print("OK 不传预算时保留旧行为（等待全部完成）")


def test_hot_list_passes_quote_budget_to_bulk_fetch():
    """社区热榜必须把预算透传给批量抓取，否则修复形同虚设。"""
    from db.migrate import get_conn
    from modules import community as cm

    conn = get_conn()
    conn.execute("DELETE FROM community_hot_list WHERE rank_date='2099-12-30'")
    try:
        for i in range(3):
            conn.execute(
                "INSERT OR REPLACE INTO community_hot_list "
                "(rank_date, symbol, rank, score, change_pct, mention_count) VALUES (?,?,?,?,?,?)",
                ("2099-12-30", f"00000{i}", i + 1, 100.0, 1.0, 10),
            )
        with patch("modules.community.fetch_quotes_bulk") as mock_bulk:
            mock_bulk.return_value = {f"00000{i}": None for i in range(3)}
            cm.get_daily_hot_list("2099-12-30", limit=3, quote_budget_seconds=5.0)
            assert mock_bulk.call_count == 1
            _, kwargs = mock_bulk.call_args
            assert kwargs.get("budget_seconds") == 5.0, (
                f"应透传 budget_seconds=5.0，实际 kwargs={kwargs}"
            )
        print("OK 热榜把 quote_budget_seconds 透传给 fetch_quotes_bulk")
    finally:
        # 测试数据必须清理，否则 get_latest_hot_list_date() 会把 2099 当最新日期
        conn.execute("DELETE FROM community_hot_list WHERE rank_date='2099-12-30'")


def test_hot_list_endpoint_defines_a_budget():
    """端点常量应存在且为有限正数（防止有人把它改回无预算）。"""
    from api import endpoints_v2 as ep

    budget = getattr(ep, "_HOT_LIST_QUOTE_BUDGET_SECONDS", None)
    assert isinstance(budget, (int, float)) and 0 < budget <= 30, (
        f"热榜行情预算应为有限正数，实际 {budget!r}"
    )
    print(f"OK 热榜行情预算 = {budget}s")


def main():
    test_bulk_fetch_returns_within_budget_when_upstream_hangs()
    test_bulk_fetch_keeps_fast_results_and_drops_slow_ones()
    test_bulk_fetch_without_budget_keeps_waiting_for_all()
    test_hot_list_passes_quote_budget_to_bulk_fetch()
    test_hot_list_endpoint_defines_a_budget()
    print("\n全部通过")


if __name__ == "__main__":
    main()
