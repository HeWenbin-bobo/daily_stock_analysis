# -*- coding: utf-8 -*-
"""Phase 10 smoke test: 组合管理（持仓 + 盈亏 + 风险预警 + 风险提示）"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrate import get_conn
from modules import portfolio as pf
from modules import paper_trading as pt
from core import data_source as ds


def _reset_tables():
    conn = get_conn()
    conn.execute("DELETE FROM paper_account")
    conn.execute("DELETE FROM paper_trade")
    conn.execute("DELETE FROM paper_snapshot")


def test_encryption_roundtrip():
    pf.test_encryption_roundtrip()
    print("OK encryption: cost roundtrip")


def test_add_holding_creates_account():
    _reset_tables()
    trade_id = pf.add_holding("u1", "600519", cost=1500.0, quantity=100, note="长期持有")
    assert trade_id > 0
    holdings = pf.list_holdings("u1")
    assert len(holdings) == 1
    assert holdings[0]["symbol"] == "600519"
    assert holdings[0]["quantity"] == 100
    assert holdings[0]["avg_cost"] == 1500.0
    assert holdings[0]["note"] == "长期持有"
    print(f"OK add holding: {holdings[0]['symbol']} qty={holdings[0]['quantity']}")


def test_add_multiple_holdings():
    _reset_tables()
    pf.add_holding("u1", "600519", cost=1500.0, quantity=100)
    pf.add_holding("u1", "000858", cost=100.0, quantity=500)
    pf.add_holding("u1", "002156", cost=50.0, quantity=1000)
    holdings = pf.list_holdings("u1")
    assert len(holdings) == 3
    symbols = {h["symbol"] for h in holdings}
    assert symbols == {"600519", "000858", "002156"}
    print(f"OK multiple holdings: {len(holdings)} stocks")


def test_avg_cost_after_multiple_buys():
    """多次买入同一只股票应算加权平均成本"""
    _reset_tables()
    pf.add_holding("u1", "600519", cost=1500.0, quantity=100)  # 150,000
    pf.add_holding("u1", "600519", cost=1600.0, quantity=100)  # 160,000
    holdings = pf.list_holdings("u1")
    assert len(holdings) == 1
    assert holdings[0]["quantity"] == 200
    # avg = (1500*100 + 1600*100) / 200 = 310000/200 = 1550
    assert abs(holdings[0]["avg_cost"] - 1550.0) < 0.01
    print(f"OK avg cost: {holdings[0]['avg_cost']}")


def test_holding_pnl_calculation():
    """当前价 > 成本 → 盈利"""
    _reset_tables()
    # ★ Round 4：list_holdings 走 fetch_quotes_bulk（位于 core.data_source），
    #   patch 路径同步切换到 data_source
    from core import data_source as ds
    orig = ds.fetch_quote_with_fallback
    ds.fetch_quote_with_fallback = lambda code: {"name": "测试", "price": 1700.0, "industry": "白酒"}
    try:
        pf.add_holding("u1", "600519", cost=1500.0, quantity=100)
        holdings = pf.list_holdings("u1")
        h = holdings[0]
        # market_value = 100 * 1700 = 170,000
        assert h["market_value"] == 170_000
        # pnl = 170,000 - 150,000 = 20,000
        assert h["pnl"] == 20_000
        # pnl_pct = 20000 / 150000 * 100 = 13.33
        assert abs(h["pnl_pct"] - 13.33) < 0.1
        print(f"OK pnl: cost=¥150k, mkt=¥170k, pnl=¥{h['pnl']:,.0f} ({h['pnl_pct']:+.2f}%)")
    finally:
        from core import data_source as ds
        ds.fetch_quote_with_fallback = orig


def test_overview_industry_breakdown():
    _reset_tables()
    from core import data_source as ds
    orig = ds.fetch_quote_with_fallback
    quote_table = {
        "600519": {"name": "茅台", "price": 1700.0, "industry": "白酒"},
        "000858": {"name": "五粮液", "price": 100.0, "industry": "白酒"},
        "002156": {"name": "通富微电", "price": 50.0, "industry": "半导体"},
    }
    ds.fetch_quote_with_fallback = lambda code: quote_table.get(code)
    try:
        pf.add_holding("u1", "600519", cost=1500.0, quantity=100)  # 170,000
        pf.add_holding("u1", "000858", cost=100.0, quantity=500)   # 50,000
        pf.add_holding("u1", "002156", cost=50.0, quantity=1000)   # 50,000
        overview = pf.get_overview("u1")
        # total_market_value = 170,000 + 50,000 + 50,000 = 270,000
        assert overview["total_market_value"] == 270_000
        # 白酒 = 220,000 / 270,000 = 81.48%  → 触发预警
        assert "白酒" in overview["industry_pct"]
        assert overview["industry_pct"]["白酒"] > 80
        # 半导体 = 50,000 / 270,000 = 18.52%
        assert "半导体" in overview["industry_pct"]
        print(f"OK industry breakdown: {overview['industry_pct']}")
    finally:
        ds.fetch_quote_with_fallback = orig


def test_warning_single_stock_concentration():
    """单股 > 30% → 触发预警"""
    _reset_tables()
    orig = ds.fetch_quote_with_fallback
    ds.fetch_quote_with_fallback = lambda code: {"name": "A", "price": 100.0, "industry": "科技"}
    try:
        # 80% 集中在 A
        pf.add_holding("u1", "A", cost=100.0, quantity=100)
        pf.add_holding("u1", "B", cost=100.0, quantity=25)
        overview = pf.get_overview("u1")
        conc_warnings = [w for w in overview["warnings"] if w["type"] == "concentration"]
        assert len(conc_warnings) >= 1
        assert "A" in conc_warnings[0]["message"]
        print(f"OK concentration warning: {conc_warnings[0]['message']}")
    finally:
        ds.fetch_quote_with_fallback = orig


def test_warning_industry_concentration():
    """单一行业 > 60% → 触发预警"""
    _reset_tables()
    # ★ Round 4：list_holdings 走 fetch_quotes_bulk（位于 core.data_source），
    #   patch 路径同步切换
    from core import data_source as ds
    orig = ds.fetch_quote_with_fallback
    quote_table = {
        "A": {"name": "A", "price": 100.0, "industry": "白酒"},
        "B": {"name": "B", "price": 100.0, "industry": "白酒"},
        "C": {"name": "C", "price": 100.0, "industry": "半导体"},
    }
    ds.fetch_quote_with_fallback = lambda code: quote_table.get(code)
    try:
        pf.add_holding("u1", "A", cost=100.0, quantity=100)  # 10,000
        pf.add_holding("u1", "B", cost=100.0, quantity=100)  # 10,000
        pf.add_holding("u1", "C", cost=100.0, quantity=20)   # 2,000
        overview = pf.get_overview("u1")
        ind_warnings = [w for w in overview["warnings"] if w["type"] == "industry_concentration"]
        assert len(ind_warnings) >= 1
        assert "白酒" in ind_warnings[0]["message"]
        print(f"OK industry warning: {ind_warnings[0]['message']}")
    finally:
        ds.fetch_quote_with_fallback = orig


def test_warning_total_loss():
    """总亏损 > 20% → critical 预警"""
    _reset_tables()
    orig = ds.fetch_quote_with_fallback
    ds.fetch_quote_with_fallback = lambda code: {"name": "A", "price": 50.0, "industry": "科技"}
    try:
        pf.add_holding("u1", "A", cost=100.0, quantity=100)  # 成本 10,000，当前 5,000 → -50%
        overview = pf.get_overview("u1")
        loss_warnings = [w for w in overview["warnings"] if w["type"] == "total_loss"]
        assert len(loss_warnings) == 1
        assert loss_warnings[0]["level"] == "critical"
        print(f"OK total loss warning: {loss_warnings[0]['message']}")
    finally:
        ds.fetch_quote_with_fallback = orig


def test_no_warning_for_balanced_portfolio():
    """分散良好的组合不应有预警（5 只 × 5 行业，每只 20%）"""
    _reset_tables()
    orig = ds.fetch_quote_with_fallback
    quote_table = {
        "A": {"name": "A", "price": 100.0, "industry": "科技"},
        "B": {"name": "B", "price": 100.0, "industry": "金融"},
        "C": {"name": "C", "price": 100.0, "industry": "医药"},
        "D": {"name": "D", "price": 100.0, "industry": "消费"},
        "E": {"name": "E", "price": 100.0, "industry": "能源"},
    }
    ds.fetch_quote_with_fallback = lambda code: quote_table.get(code)
    try:
        for code in ["A", "B", "C", "D", "E"]:
            pf.add_holding("u1", code, cost=100.0, quantity=10)  # 每只 1,000
        overview = pf.get_overview("u1")
        assert len(overview["warnings"]) == 0, f"Should have no warnings, got {overview['warnings']}"
        print(f"OK balanced portfolio: 0 warnings (5 stocks × 5 industries, each 20%)")
    finally:
        ds.fetch_quote_with_fallback = orig


def test_generate_report_includes_disclaimer():
    """生成的报告必须含风险提示"""
    _reset_tables()
    orig = ds.fetch_quote_with_fallback
    ds.fetch_quote_with_fallback = lambda code: {"name": "A", "price": 100.0, "industry": "科技"}
    try:
        pf.add_holding("u1", "A", cost=100.0, quantity=10)
        report = pf.generate_report("u1")
        assert "组合分析报告" in report["content"]
        assert "不构成" in report["content"] and "投资建议" in report["content"]
        assert "投资有风险" in report["content"]
        print("OK report includes disclaimer")
    finally:
        ds.fetch_quote_with_fallback = orig


def test_remove_holding():
    _reset_tables()
    pf.add_holding("u1", "A", cost=100.0, quantity=10)
    pf.add_holding("u1", "B", cost=100.0, quantity=10)
    assert pf.remove_holding("u1", "A")
    holdings = pf.list_holdings("u1")
    assert len(holdings) == 1
    assert holdings[0]["symbol"] == "B"
    print(f"OK remove holding: remaining={holdings[0]['symbol']}")


def test_remove_nonexistent_holding():
    _reset_tables()
    assert pf.remove_holding("u1", "NONEXISTENT") is False
    print("OK remove nonexistent returns False")


def test_overview_with_no_holdings():
    _reset_tables()
    overview = pf.get_overview("u1")
    assert overview["total_market_value"] == 0
    assert overview["holdings"] == []
    assert overview["warnings"] == []
    print("OK empty overview: 0 holdings, 0 warnings")


if __name__ == "__main__":
    test_encryption_roundtrip()
    test_add_holding_creates_account()
    test_add_multiple_holdings()
    test_avg_cost_after_multiple_buys()
    test_holding_pnl_calculation()
    test_overview_industry_breakdown()
    test_warning_single_stock_concentration()
    test_warning_industry_concentration()
    test_warning_total_loss()
    test_no_warning_for_balanced_portfolio()
    test_generate_report_includes_disclaimer()
    test_remove_holding()
    test_remove_nonexistent_holding()
    test_overview_with_no_holdings()
    print("\n=== Phase 10 smoke test: ALL 15 PASSED ===")