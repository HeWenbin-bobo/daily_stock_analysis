# -*- coding: utf-8 -*-
"""Regression test: the portfolio ledger must not take part in paper transfers.

``place_order`` and ``settle_daily`` both refuse portfolio-ledger accounts via
``is_portfolio_account``, but ``transfer_between_accounts`` did not. The
portfolio account's cash is only a nominal starting balance — its real assets
live in ``portfolio_holding`` — so allowing transfers let a user move that
nominal cash into a paper account and "realise" it.

Reproduced before the fix: transferring ¥10,000 out of 「我的持仓」 succeeded and
dropped its cash from 1,000,000 to 990,000.

Run: ``python -m pytest update_version/backend/tests/test_phase66_portfolio_transfer_isolation.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from modules import paper_trading as pt
from modules import portfolio as pf


@pytest.fixture()
def accounts():
    """A portfolio ledger account plus two normal paper accounts."""
    pf.add_holding("u_xfer", "600519", quantity=100, cost=1000.0)
    owned = pt.list_accounts("u_xfer")
    portfolio_id = next(a["id"] for a in owned if a["name"] == "我的持仓")
    assert pt.is_portfolio_account(portfolio_id)
    a = pt.create_account("u_xfer", "paper_a", 50000.0)
    b = pt.create_account("u_xfer", "paper_b", 10000.0)
    return portfolio_id, a, b


def test_cannot_transfer_out_of_portfolio_ledger(accounts) -> None:
    portfolio_id, paper_a, _ = accounts
    before = pt.get_account(portfolio_id)["cash"]

    with pytest.raises(ValueError, match="组合持仓"):
        pt.transfer_between_accounts(portfolio_id, paper_a, 10000.0, "probe")

    assert pt.get_account(portfolio_id)["cash"] == before, "portfolio cash changed"


def test_cannot_transfer_into_portfolio_ledger(accounts) -> None:
    portfolio_id, paper_a, _ = accounts
    before = pt.get_account(portfolio_id)["cash"]

    with pytest.raises(ValueError, match="组合持仓"):
        pt.transfer_between_accounts(paper_a, portfolio_id, 1000.0, "probe")

    assert pt.get_account(portfolio_id)["cash"] == before


def test_normal_accounts_still_transfer(accounts) -> None:
    _, paper_a, paper_b = accounts
    result = pt.transfer_between_accounts(paper_a, paper_b, 1000.0, "ok")
    assert result["from_new_cash"] == pytest.approx(49000.0)
    assert result["to_new_cash"] == pytest.approx(11000.0)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
