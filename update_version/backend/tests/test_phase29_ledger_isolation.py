# -*- coding: utf-8 -*-
"""Regression tests for ledger separation (2026-10-19 exploration).

`modules/portfolio.py` and `modules/paper_trading.py` share the same
`paper_account` / `paper_trade` tables. `portfolio.add_holding()` creates a
"我的持仓" account and writes a `buy` row with `reason='portfolio_holding|...'`.

Before the fix, `paper_trading.get_positions()` / `place_order()` / `settle_daily()`
/ `leaderboard()` did not distinguish the two ledgers, so:

* `POST /paper/accounts/{id}/orders` could **sell a real portfolio holding at a
  caller-chosen price**, crediting the account cash (reproduced: 10 shares @1000
  → cash 1,000,000 → 1,009,995), and
* that account then appeared on `/paper/leaderboard` — a free scoreboard cheat.

Run: ``python -m pytest update_version/backend/tests/test_phase29_ledger_isolation.py``
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

# 必须放到仓库之外：backend/data 是容器的 bind mount 目录，宿主侧打开任何
# .db 都会锁住容器内的 SQLite（见 memory: dsa-container-sqlite-host-access-trap）。
_PROBE_DIR = Path(tempfile.mkdtemp(prefix="dsa_probe_ledger_"))
os.environ.setdefault("V4_DB_PATH", str(_PROBE_DIR / "v2_test_ledger.db"))

import pytest

from db.migrate import migrate
from modules import paper_trading as pt
from modules import portfolio as pf
import core.data_source as ds


def _fake_bulk(symbols, parallel=True):
    return {s: {"price": 100.0, "name": s} for s in symbols}


@pytest.fixture()
def ledger():
    """每个测试用独立库 + 固定行情。"""
    with mock.patch.object(ds, "fetch_quotes_bulk", side_effect=_fake_bulk):
        migrate()
        conn = pt.get_conn()
        for table in ("paper_snapshot", "paper_trade", "paper_account"):
            conn.execute(f"DELETE FROM {table}")
        yield conn
        conn.close()


def _portfolio_account(user_id: str) -> dict:
    pf.add_holding(user_id, "600519", cost=1000, quantity=10, purchase_date="2025-01-05")
    return next(a for a in pt.list_accounts(user_id) if a["name"] == "我的持仓")


def test_portfolio_account_is_detected(ledger) -> None:
    acc = _portfolio_account("alice")
    assert pt.is_portfolio_account(acc["id"]) is True
    normal = pt.create_account("alice", "模拟盘", 1_000_000)
    assert pt.is_portfolio_account(normal) is False


def test_place_order_rejects_portfolio_ledger(ledger) -> None:
    """核心断言：真实持仓不能通过模拟盘卖出变现。"""
    acc = _portfolio_account("alice")
    with pytest.raises(ValueError, match="组合持仓账本"):
        pt.place_order(acc["id"], "600519", "sell", 10, price=1000)
    assert pt.get_account(acc["id"])["cash"] == 1_000_000.0, "现金不得变化"


def test_portfolio_holding_not_counted_as_paper_position(ledger) -> None:
    """模拟盘视角看不到组合账本的持仓（反之亦然）。"""
    _portfolio_account("alice")
    normal = pt.create_account("alice", "模拟盘", 1_000_000)
    assert pt.get_positions(normal) == []

    # 组合侧仍能看到自己的持仓
    holdings = pf.list_holdings("alice")
    assert [(h["symbol"], h["quantity"]) for h in holdings] == [("600519", 10)]


def test_leaderboard_excludes_portfolio_ledger(ledger) -> None:
    _portfolio_account("alice")
    normal = pt.create_account("bob", "模拟盘", 1_000_000)
    pt.place_order(normal, "600519", "buy", 10, price=100)

    summary = pt.settle_daily()
    assert summary["skipped_portfolio"] == 1

    names = [row["account_name"] for row in pt.leaderboard()]
    assert "我的持仓" not in names, f"组合账本出现在排行榜: {names}"
    assert "模拟盘" in names, f"真实模拟盘缺失: {names}"


def test_paper_order_still_works_normally(ledger) -> None:
    """隔离不能误伤正常模拟盘下单。"""
    acc_id = pt.create_account("carol", "模拟盘", 1_000_000)
    res = pt.place_order(acc_id, "600519", "buy", 10, price=100)
    assert res["trade_id"] > 0
    assert pt.get_account(acc_id)["cash"] == pytest.approx(1_000_000 - 1000 - 5)
    assert [(p["symbol"], p["quantity"]) for p in pt.get_positions(acc_id)] == [("600519", 10)]
