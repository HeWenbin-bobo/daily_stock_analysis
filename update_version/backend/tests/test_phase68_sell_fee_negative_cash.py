# -*- coding: utf-8 -*-
"""Regression test: a sell whose proceeds are below the commission must not run.

The commission is ``max(amount * 0.0003, 5.0)`` — a ¥5 floor. Selling one share
at ¥1 yields ¥1, so ``cash + (amount - fee)`` drove the balance negative:
measured cash 0.00 → -4.00. Negative cash then corrupts the leaderboard equity
and defeats the ``cash > 0`` guard on the buy path.

Run: ``python -m pytest update_version/backend/tests/test_phase68_sell_fee_negative_cash.py``
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from modules import paper_trading as pt


_seq = 0


def _account_with_yesterday_buy(cash_after: float = 0.0) -> int:
    """A fresh paper account holding 995 shares bought "yesterday" (T+1 ok).

    create_account reuses an existing (user_id, name) row, so each call needs its
    own name — otherwise the second test inherits the drained balance.
    """
    global _seq
    _seq += 1
    acc = pt.create_account(f"u_fee_{_seq}", f"fee_{_seq}", 1000.0)
    pt.place_order(acc, "600519", "buy", quantity=995, price=1.0)
    # 把买入挪到昨天，绕过 T+1，并把现金对齐到目标值
    conn = pt.get_conn()
    conn.execute("UPDATE paper_trade SET trade_date='2026-01-01' WHERE account_id=?", (acc,))
    conn.execute("UPDATE paper_account SET cash=? WHERE id=?", (cash_after, acc))
    return acc


def test_sell_below_commission_is_rejected() -> None:
    acc = _account_with_yesterday_buy(cash_after=0.0)

    with pytest.raises(ValueError, match="不足以支付佣金"):
        pt.place_order(acc, "600519", "sell", quantity=1, price=1.0)

    assert pt.get_account(acc)["cash"] == pytest.approx(0.0), "cash went negative"


def test_cash_never_goes_negative_from_a_rejected_sell() -> None:
    acc = _account_with_yesterday_buy(cash_after=0.0)

    for _ in range(3):
        with pytest.raises(ValueError):
            pt.place_order(acc, "600519", "sell", quantity=1, price=1.0)

    assert pt.get_account(acc)["cash"] >= 0, "repeated rejected sells drained cash"


def test_normal_sell_still_works() -> None:
    acc = _account_with_yesterday_buy(cash_after=0.0)

    pt.place_order(acc, "600519", "sell", quantity=100, price=10.0)

    # 1000 收入 - 5 佣金
    assert pt.get_account(acc)["cash"] == pytest.approx(995.0)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
