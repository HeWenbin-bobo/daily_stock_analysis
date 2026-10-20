# -*- coding: utf-8 -*-
"""Regression test: daily settlement must fetch the benchmark once, not per account.

``settle_daily`` walks every account and calls ``take_snapshot``, which fetched
the CSI-300 benchmark each time. ``fetch_quote_with_fallback`` has **no cache**
(verified: three calls leave ``cache size: 0``), so 17 accounts meant 17 network
round-trips — measured 205s, which is already crowding the 5-minute
``max_instances=1`` slot and grows with account count.

Run: ``python -m pytest update_version/backend/tests/test_phase63_settle_benchmark_reuse.py``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from modules import paper_trading as pt


def _fake_accounts(n: int):
    return [{"id": i, "user_id": f"u{i}", "name": f"acc{i}", "initial_capital": 100000.0} for i in range(1, n + 1)]


def test_settle_daily_fetches_benchmark_once(monkeypatch) -> None:
    """One benchmark fetch per settle run, regardless of account count."""
    accounts = _fake_accounts(6)
    calls: list[str] = []

    def _quote(code):
        calls.append(code)
        return {"code": code, "price": 4000.0, "change_pct": 1.0, "source": "test"}

    monkeypatch.setattr(pt, "list_accounts", lambda: accounts)
    monkeypatch.setattr(pt, "is_portfolio_account", lambda _aid: False)
    monkeypatch.setattr(pt, "fetch_quote_with_fallback", _quote)
    monkeypatch.setattr(pt, "take_snapshot", lambda aid, date, **kw: {"account_id": aid})

    summary = pt.settle_daily()

    assert summary["settled"] == len(accounts)
    assert calls == ["000300"], (
        f"benchmark fetched {len(calls)} times for {len(accounts)} accounts: {calls}"
    )


def test_take_snapshot_still_fetches_when_called_alone(monkeypatch) -> None:
    """A manual single-account snapshot must keep fetching its own benchmark."""
    fetched: list[str] = []

    def _quote(code):
        fetched.append(code)
        return {"code": code, "price": 4000.0, "change_pct": 1.0, "source": "test"}

    monkeypatch.setattr(pt, "get_account", lambda _aid: {"id": 1, "cash": 1000.0, "initial_capital": 1000.0})
    monkeypatch.setattr(pt, "get_positions", lambda _aid: [])
    monkeypatch.setattr(pt, "fetch_quote_with_fallback", _quote)
    monkeypatch.setattr(pt, "_persist_snapshot", lambda *a, **k: None, raising=False)

    doc = pt.take_snapshot(1)

    assert fetched == ["000300"], f"expected one fetch, got {fetched}"
    assert doc["benchmark_equity"] == pytest.approx(1010.0)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
