# -*- coding: utf-8 -*-
"""v2 audit round 1 — 新功能 smoke test
- 策略对比 (compare_backtest_results)
- 异动规则批量启用/禁用 (bulk_toggle)
- 模拟盘转账 (transfer_between_accounts)
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ★ 迭代2 修复（测试隔离）：切到独立测试库 v2_test.db，防止全表 DELETE 清空生产库
# ★ 探索期修复(绕过 conftest 隔离)：无条件覆盖会把 conftest 已设好的
#   进程级临时库改回仓库工作目录，每次跑测试都在 backend/data/ 留一份
#   v2_test.db。仅在无人设置时兜底。
if not os.environ.get("V4_DB_PATH"):
    os.environ["V4_DB_PATH"] = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "v2_test.db"
    )

from db.migrate import get_conn, migrate
migrate()  # 确保测试库 schema 就绪（幂等）
from modules import backtest_engine as be
from modules import alerts_engine as ae
from modules import paper_trading as pt


def _reset():
    conn = get_conn()
    conn.execute("DELETE FROM backtest_strategy")
    conn.execute("DELETE FROM backtest_result")
    conn.execute("DELETE FROM alert_rule")
    conn.execute("DELETE FROM paper_account")
    conn.execute("DELETE FROM paper_trade")
    conn.execute("DELETE FROM paper_snapshot")


# ============================================================================
# 策略对比
# ============================================================================
def _make_result(total_return: float, sharpe: float, win_rate: float = 50.0):
    """直接写一条 backtest_result"""
    import time, json
    from db.migrate import get_conn
    sid = be.create_strategy("u1", "tmp", "tmp")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO backtest_result
           (strategy_id, user_id, symbol, start_date, end_date,
            initial_capital, final_capital, total_return_pct,
            annualized_return_pct, sharpe_ratio, max_drawdown_pct,
            win_rate, total_trades, trades_json, equity_curve_json,
            status, created_at, completed_at)
           VALUES (?, 'u1', 'T', '2024-01-01', '2024-12-31',
            1000000, ?, ?, ?, ?, ?, ?, ?, '[]', '[]',
            'success', ?, ?)""",
        (sid, 1000000 * (1 + total_return / 100), total_return,
         total_return, sharpe, 10.0, win_rate, 10,
         time.time(), time.time()),
    )
    return cur.lastrowid


def test_backtest_compare_basic():
    _reset()
    a_id = _make_result(total_return=10.0, sharpe=1.0, win_rate=50.0)
    b_id = _make_result(total_return=20.0, sharpe=1.5, win_rate=60.0)
    result = be.compare_backtest_results(a_id, b_id)
    assert "metrics_comparison" in result
    assert result["metrics_comparison"]["total_return_pct"]["A"] == 10.0
    assert result["metrics_comparison"]["total_return_pct"]["B"] == 20.0
    assert result["metrics_comparison"]["total_return_pct"]["diff"] == 10.0
    assert result["winner"] == "B", f"B should win, got {result['winner']}"
    print(f"OK backtest compare: A={a_id}, B={b_id}, winner={result['winner']}")


def test_backtest_compare_tie():
    _reset()
    a_id = _make_result(10.0, 1.0)
    b_id = _make_result(10.5, 1.1)  # 微小差异
    result = be.compare_backtest_results(a_id, b_id)
    # diff sharpe = 0.1 < 0.3 → tie
    assert result["winner"] in ("B", "tie"), f"Should be tie or B, got {result['winner']}"
    print(f"OK backtest compare tie: winner={result['winner']}")


def test_backtest_compare_a_wins():
    _reset()
    a_id = _make_result(30.0, 2.0)
    b_id = _make_result(10.0, 1.0)
    result = be.compare_backtest_results(a_id, b_id)
    assert result["winner"] == "A"
    print(f"OK backtest compare: A wins")


def test_backtest_compare_nonexistent():
    _reset()
    result = be.compare_backtest_results(99999, 88888)
    assert "error" in result
    print("OK compare nonexistent returns error")


# ============================================================================
# 异动规则批量启用/禁用
# ============================================================================
def test_alert_bulk_toggle_enable():
    _reset()
    r1 = ae.create_rule("u1", "r1", "limit_up", {})
    r2 = ae.create_rule("u1", "r2", "limit_down", {})
    r3 = ae.create_rule("u1", "r3", "volume_ratio", {})
    # 默认 enabled=1
    affected = ae.bulk_toggle([r1, r2, r3], enabled=True)
    assert affected == 3
    # 禁用 r1 和 r2
    affected = ae.bulk_toggle([r1, r2], enabled=False)
    assert affected == 2
    # 验证
    rules = {r["id"]: r for r in ae.list_rules("u1")}
    assert rules[r1]["enabled"] == 0
    assert rules[r2]["enabled"] == 0
    assert rules[r3]["enabled"] == 1
    print(f"OK bulk toggle: disabled r1={r1}, r2={r2}, kept r3={r3}")


def test_alert_bulk_toggle_empty_list():
    _reset()
    affected = ae.bulk_toggle([], enabled=True)
    assert affected == 0
    print("OK bulk toggle empty list returns 0")


def test_alert_bulk_toggle_nonexistent_ids():
    _reset()
    affected = ae.bulk_toggle([99999, 88888], enabled=True)
    assert affected == 0
    print("OK bulk toggle nonexistent ids returns 0")


# ============================================================================
# 模拟盘转账
# ============================================================================
def test_paper_transfer_basic():
    _reset()
    a = pt.create_account("u1", "AccountA", initial_capital=500_000)
    b = pt.create_account("u1", "AccountB", initial_capital=200_000)
    result = pt.transfer_between_accounts(a, b, 100_000, note="rebalance")
    assert result["amount"] == 100_000
    assert result["from_new_cash"] == 400_000
    assert result["to_new_cash"] == 300_000
    # 验证账户余额
    acc_a = pt.get_account(a)
    acc_b = pt.get_account(b)
    assert abs(acc_a["cash"] - 400_000) < 0.01
    assert abs(acc_b["cash"] - 300_000) < 0.01
    print(f"OK transfer: A {500_000}->{400_000}, B {200_000}->{300_000}")


def test_paper_transfer_self_blocked():
    _reset()
    a = pt.create_account("u1", "A")
    try:
        pt.transfer_between_accounts(a, a, 1000)
        assert False, "Should raise"
    except ValueError as e:
        assert "自己" in str(e)
        print(f"OK self-transfer blocked: {e}")


def test_paper_transfer_insufficient_cash():
    _reset()
    a = pt.create_account("u1", "A", initial_capital=100)
    b = pt.create_account("u1", "B", initial_capital=1000)
    try:
        pt.transfer_between_accounts(a, b, 500)
        assert False, "Should raise"
    except ValueError as e:
        assert "现金不足" in str(e)
        print(f"OK transfer insufficient blocked: {e}")


def test_paper_transfer_records_trades():
    _reset()
    a = pt.create_account("u1", "A")
    b = pt.create_account("u1", "B")
    pt.transfer_between_accounts(a, b, 50_000, note="test")
    trades_a = pt.list_trades(a)
    trades_b = pt.list_trades(b)
    assert any("transfer_out" in t["reason"] for t in trades_a)
    assert any("transfer_in" in t["reason"] for t in trades_b)
    print(f"OK transfer trades recorded: A={len(trades_a)}, B={len(trades_b)}")


def test_paper_transfer_invalid_amount():
    _reset()
    a = pt.create_account("u1", "A")
    b = pt.create_account("u1", "B")
    try:
        pt.transfer_between_accounts(a, b, 0)
        assert False
    except ValueError as e:
        assert "amount" in str(e)
        print(f"OK transfer zero blocked: {e}")
    try:
        pt.transfer_between_accounts(a, b, -100)
        assert False
    except ValueError:
        print("OK transfer negative blocked")


if __name__ == "__main__":
    test_backtest_compare_basic()
    test_backtest_compare_tie()
    test_backtest_compare_a_wins()
    test_backtest_compare_nonexistent()
    test_alert_bulk_toggle_enable()
    test_alert_bulk_toggle_empty_list()
    test_alert_bulk_toggle_nonexistent_ids()
    test_paper_transfer_basic()
    test_paper_transfer_self_blocked()
    test_paper_transfer_insufficient_cash()
    test_paper_transfer_records_trades()
    test_paper_transfer_invalid_amount()
    print("\n=== v2 audit round 1 new features: ALL 13 PASSED ===")