# -*- coding: utf-8 -*-
"""Phase 5 smoke test: 模拟盘"""
import time
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrate import get_conn
from modules import paper_trading as pt
from core import data_source as ds


def _reset_tables():
    conn = get_conn()
    conn.execute("DELETE FROM paper_account")
    conn.execute("DELETE FROM paper_trade")
    conn.execute("DELETE FROM paper_snapshot")


def test_create_account():
    _reset_tables()
    acc_id = pt.create_account("u1", "测试账户")
    assert acc_id > 0
    acc = pt.get_account(acc_id)
    assert acc["cash"] == 1_000_000
    assert acc["initial_capital"] == 1_000_000
    print(f"OK create account: id={acc_id}, cash=¥{acc['cash']:,.0f}")


def test_create_account_unique_constraint_update():
    _reset_tables()
    id1 = pt.create_account("u1", "MyAccount", initial_capital=500_000)
    id2 = pt.create_account("u1", "MyAccount", initial_capital=2_000_000)
    assert id1 == id2
    acc = pt.get_account(id1)
    # 应该返回已存在的（cash 不变）
    assert acc["cash"] == 500_000, f"Should keep original 500000, got {acc['cash']}"
    print(f"OK unique update: id={id1}")


def test_list_accounts():
    _reset_tables()
    pt.create_account("u1", "A")
    pt.create_account("u1", "B")
    pt.create_account("u2", "C")
    assert len(pt.list_accounts(user_id="u1")) == 2
    assert len(pt.list_accounts(user_id="u2")) == 1
    print("OK list accounts")


def test_place_buy_order():
    _reset_tables()
    # mock 行情
    orig = ds.fetch_quote_with_fallback
    ds.fetch_quote_with_fallback = lambda code: {"code": code, "name": "测试", "price": 100.0, "change_pct": 0.5}
    try:
        acc_id = pt.create_account("u1", "test")
        result = pt.place_order(acc_id, "600519", "buy", quantity=1000, price=100.0)
        assert result["amount"] == 100_000
        assert abs(result["fee"] - 30.0) < 0.01, f"Fee should be ~30, got {result['fee']}"
        # 现金应该是 1,000,000 - 100,000 - 30 = 899,970
        assert abs(result["new_cash"] - 899_970) < 1, f"new_cash should be ~899970, got {result['new_cash']}"
        # 持仓应该有 1000 股
        positions = pt.get_positions(acc_id)
        assert len(positions) == 1
        assert positions[0]["symbol"] == "600519"
        assert positions[0]["quantity"] == 1000
        print(f"OK buy: amount=¥{result['amount']}, new_cash=¥{result['new_cash']}")
    finally:
        ds.fetch_quote_with_fallback = orig


def test_place_buy_invalid_quantity():
    _reset_tables()
    acc_id = pt.create_account("u1", "test")
    try:
        pt.place_order(acc_id, "600519", "buy", quantity=0)
        assert False, "Should raise"
    except ValueError as e:
        assert "quantity" in str(e)
        print(f"OK invalid qty: {e}")


def test_place_buy_insufficient_cash():
    _reset_tables()
    orig = ds.fetch_quote_with_fallback
    ds.fetch_quote_with_fallback = lambda code: {"price": 1500.0}
    try:
        acc_id = pt.create_account("u1", "test", initial_capital=100_000)
        try:
            pt.place_order(acc_id, "600519", "buy", quantity=100, price=1500.0)
            assert False, "Should raise"
        except ValueError as e:
            assert "现金不足" in str(e)
            print(f"OK insufficient cash blocked: {e}")
    finally:
        ds.fetch_quote_with_fallback = orig


def test_place_sell_order():
    _reset_tables()
    orig = ds.fetch_quote_with_fallback
    ds.fetch_quote_with_fallback = lambda code: {"price": 110.0}
    try:
        acc_id = pt.create_account("u1", "test")
        pt.place_order(acc_id, "600519", "buy", quantity=1000, price=100.0)
        result = pt.place_order(acc_id, "600519", "sell", quantity=500, price=110.0)
        assert result["amount"] == 55_000
        # 现金 = 899,970 + 55,000 - 16.5 ≈ 954,953.5
        assert abs(result["new_cash"] - 954_953.5) < 2, f"new_cash should be ~954953.5, got {result['new_cash']}"
        # 持仓应该还剩 500
        positions = pt.get_positions(acc_id)
        assert positions[0]["quantity"] == 500
        # 卖出后 avg_cost 应该按比例减少
        assert positions[0]["avg_cost"] == 100.0  # 全仓买的，成本不变
        print(f"OK sell: new_cash=¥{result['new_cash']}, remaining_qty={positions[0]['quantity']}")
    finally:
        ds.fetch_quote_with_fallback = orig


def test_place_sell_insufficient_position():
    _reset_tables()
    orig = ds.fetch_quote_with_fallback
    ds.fetch_quote_with_fallback = lambda code: {"price": 100.0}
    try:
        acc_id = pt.create_account("u1", "test")
        pt.place_order(acc_id, "600519", "buy", quantity=100, price=100.0)
        try:
            pt.place_order(acc_id, "600519", "sell", quantity=500, price=100.0)
            assert False
        except ValueError as e:
            assert "持仓不足" in str(e)
            print(f"OK sell insufficient blocked: {e}")
    finally:
        ds.fetch_quote_with_fallback = orig


def test_place_sell_invalid_side():
    _reset_tables()
    acc_id = pt.create_account("u1", "test")
    try:
        pt.place_order(acc_id, "600519", "invalid", quantity=100, price=100.0)
        assert False
    except ValueError as e:
        assert "side" in str(e)
        print(f"OK invalid side: {e}")


def test_list_trades():
    _reset_tables()
    orig = ds.fetch_quote_with_fallback
    ds.fetch_quote_with_fallback = lambda code: {"price": 100.0}
    try:
        acc_id = pt.create_account("u1", "test")
        pt.place_order(acc_id, "600519", "buy", quantity=100, price=100.0)
        pt.place_order(acc_id, "000858", "buy", quantity=100, price=50.0)
        trades = pt.list_trades(acc_id)
        assert len(trades) == 2
        # 按 created_at DESC
        assert trades[0]["symbol"] == "000858"
        print(f"OK list trades: {len(trades)} entries")
    finally:
        ds.fetch_quote_with_fallback = orig


def test_take_snapshot():
    _reset_tables()
    # ★ 第三轮：改用 patch fetch_quotes_bulk（新接口）— 兼容并行/串行调用
    quote_table = {
        "600519": {"code": "600519", "price": 110.0},
        "000300": {"code": "000300", "price": 4000.0, "change_pct": 0.5},
    }
    import unittest.mock
    with unittest.mock.patch("modules.paper_trading.fetch_quotes_bulk") as mock_bulk, \
         unittest.mock.patch("modules.paper_trading.fetch_quote_with_fallback") as mock_quote:
        # fetch_quotes_bulk 返回行情表（_sync=False 不真正并发）
        mock_bulk.side_effect = lambda codes, parallel=True: {c: quote_table.get(c) for c in codes}
        # 兼容旧直接调用（benchmark 用）
        mock_quote.side_effect = lambda code: quote_table.get(code)
        acc_id = pt.create_account("u1", "test")
        pt.place_order(acc_id, "600519", "buy", quantity=1000, price=100.0)
        snap = pt.take_snapshot(acc_id, "2026-07-30")
        assert snap["snapshot_date"] == "2026-07-30"
        # cash = 1,000,000 - 100,000 - 30 = 899,970
        assert abs(snap["cash"] - 899_970) < 1
        # positions_value = 1000 * 110 = 110,000
        assert abs(snap["positions_value"] - 110_000) < 1
        # total_equity = 899,970 + 110,000 = 1,009,970
        assert abs(snap["total_equity"] - 1_009_970) < 1
        # benchmark = 1,000,000 * (1 + 0.005) = 1,005,000
        assert abs(snap["benchmark_equity"] - 1_005_000) < 1
        print(f"OK snapshot: equity=¥{snap['total_equity']:,.0f}, pnl=¥{snap['total_equity']-1_000_000:,.0f}")


def test_settle_daily_all_accounts():
    _reset_tables()
    import unittest.mock
    with unittest.mock.patch("modules.paper_trading.fetch_quotes_bulk") as mock_bulk:
        mock_bulk.side_effect = lambda codes, parallel=True: {c: {"price": 100.0} for c in codes}
        # 真正进入 settle 流程
        from modules import paper_trading as pt2
        # 创建账户后调用 settle_daily
        acc_id = pt2.create_account("u1", "test")
        # 这里仅验证 settle_daily 不抛异常
        result = pt2.settle_daily()
        assert "settled" in result
    print(f"OK settle_daily: {result}")


def test_settle_daily_three_accounts():
    """Phase 15 配套测试：验证 settle_daily 批量结算 3 个账户（用 fetch_quotes_bulk mock）"""
    _reset_tables()
    import unittest.mock
    with unittest.mock.patch("modules.paper_trading.fetch_quotes_bulk") as mock_bulk:
        mock_bulk.side_effect = lambda codes, parallel=True: {c: {"price": 100.0} for c in codes}
        from modules import paper_trading as pt2
        acc_a = pt2.create_account("u1", "A")
        acc_b = pt2.create_account("u2", "B")
        acc_c = pt2.create_account("u3", "C")
        result = pt2.settle_daily()
        assert result["settled"] == 3
        assert result["errors"] == 0
        for acc_id in (acc_a, acc_b, acc_c):
            snapshots = pt2.list_snapshots(acc_id, limit=10)
            assert len(snapshots) >= 1, f"Account {acc_id} has no snapshots"
        print(f"OK settle (3 accounts): {result}")


def test_leaderboard_orders_by_equity():
    _reset_tables()
    quote_table = {
        "A": {"price": 110.0},   # 涨 10%
        "B": {"price": 95.0},    # 跌 5%
        "C": {"price": 100.0},   # 平
    }
    import unittest.mock
    with unittest.mock.patch("modules.paper_trading.fetch_quotes_bulk") as mock_bulk, \
         unittest.mock.patch("modules.paper_trading.fetch_quote_with_fallback") as mock_quote:
        mock_bulk.side_effect = lambda codes, parallel=True: {c: quote_table.get(c, {"price": 100.0}) for c in codes}
        mock_quote.side_effect = lambda code: quote_table.get(code, {"price": 100.0})
        acc_a = pt.create_account("u1", "A")
        acc_b = pt.create_account("u2", "B")
        acc_c = pt.create_account("u3", "C")
        # 买入 1000 股
        pt.place_order(acc_a, "A", "buy", quantity=1000, price=100.0)
        pt.place_order(acc_b, "B", "buy", quantity=1000, price=100.0)
        pt.place_order(acc_c, "C", "buy", quantity=1000, price=100.0)
        # 结算
        pt.take_snapshot(acc_a, "2026-07-30")
        pt.take_snapshot(acc_b, "2026-07-30")
        pt.take_snapshot(acc_c, "2026-07-30")

        board = pt.leaderboard()
        assert len(board) == 3
        # A 应该排第一（涨 10%）
        assert board[0]["account_name"] == "A"
        assert board[0]["total_return_pct"] > 0
        # B 应该排最后（跌 5%）
        assert board[-1]["account_name"] == "B"
        assert board[-1]["total_return_pct"] < 0
        print(f"OK leaderboard: top={board[0]['account_name']} ({board[0]['total_return_pct']}%), bottom={board[-1]['account_name']} ({board[-1]['total_return_pct']}%)")


def test_get_account_overview():
    _reset_tables()
    orig = pt.fetch_quote_with_fallback
    pt.fetch_quote_with_fallback = lambda code: {"price": 100.0}
    try:
        acc_id = pt.create_account("u1", "test")
        overview = pt.get_account_overview(acc_id)
        assert "account" in overview
        assert "current_equity" in overview
        assert "current_pnl" in overview
        assert "current_pnl_pct" in overview
        assert overview["current_pnl"] == 0  # 还没交易
        print(f"OK overview: equity=¥{overview['current_equity']:,.0f}, pnl={overview['current_pnl_pct']}%")
    finally:
        pt.fetch_quote_with_fallback = orig


def test_place_order_with_alert_id():
    """模拟盘支持来自异动信号的下单"""
    _reset_tables()
    orig = ds.fetch_quote_with_fallback
    ds.fetch_quote_with_fallback = lambda code: {"price": 100.0}
    try:
        acc_id = pt.create_account("u1", "test")
        result = pt.place_order(
            acc_id, "600519", "buy", quantity=100, price=100.0,
            reason="alert_triggered", related_alert_id=42,
        )
        trades = pt.list_trades(acc_id)
        assert trades[0]["reason"] == "alert_triggered"
        assert trades[0]["related_alert_id"] == 42
        print(f"OK alert order: reason={trades[0]['reason']}, alert_id={trades[0]['related_alert_id']}")
    finally:
        ds.fetch_quote_with_fallback = orig


def test_delete_account():
    _reset_tables()
    acc_id = pt.create_account("u1", "test")
    assert pt.delete_account(acc_id)
    assert pt.get_account(acc_id) is None
    print("OK delete account")


if __name__ == "__main__":
    test_create_account()
    test_create_account_unique_constraint_update()
    test_list_accounts()
    test_place_buy_order()
    test_place_buy_invalid_quantity()
    test_place_buy_insufficient_cash()
    test_place_sell_order()
    test_place_sell_insufficient_position()
    test_place_sell_invalid_side()
    test_list_trades()
    test_take_snapshot()
    test_settle_daily_all_accounts()
    test_leaderboard_orders_by_equity()
    test_get_account_overview()
    test_place_order_with_alert_id()
    test_delete_account()
    print("\n=== Phase 5 smoke test: ALL 16 PASSED ===")