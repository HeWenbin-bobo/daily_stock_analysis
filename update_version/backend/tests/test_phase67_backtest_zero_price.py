# -*- coding: utf-8 -*-
"""Regression test: a zero close price must not crash the backtest.

``core.data_source._f()`` returns its default ``0.0`` when a value cannot be
parsed — akshare reports suspended stocks as ``"-"`` — and the kline fetcher
does not drop those bars. The buy branch then evaluated
``int(cash / close / 100)`` and raised ``ZeroDivisionError``, so one suspended
bar turned the whole backtest into a 500. Hyperopt calls the same function, so
it took down a whole parameter search too.

Run: ``python -m pytest update_version/backend/tests/test_phase67_backtest_zero_price.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from modules import backtest_engine as be


def _kline(n: int, *, zero_at: int | None = None) -> list[dict]:
    return [
        {
            "date": f"2026-01-{i + 1:02d}",
            "open": 10.0,
            "high": 10.0,
            "low": 10.0,
            "close": 0.0 if i == zero_at else 10.0,
            "volume": 1000,
        }
        for i in range(n)
    ]


_BUY_ALWAYS = {
    "entry": [{"indicator": "close", "operator": ">", "value": 1.0}],
    "exit": [{"indicator": "close", "operator": "<", "value": 1.0}],
    "stop_loss_pct": -5,
    "take_profit_pct": 10,
    "hold_days": 0,
}

_BUY_ON_ZERO = {
    "entry": [{"indicator": "close", "operator": "<", "value": 1.0}],
    "exit": [{"indicator": "close", "operator": "<", "value": 1.0}],
    "stop_loss_pct": -5,
    "take_profit_pct": 10,
    "hold_days": 0,
}


def test_zero_close_bar_does_not_crash_a_buy_signal() -> None:
    """The exact crash: the buy branch divides by close."""
    result = be._execute_backtest(_kline(60, zero_at=0), _BUY_ON_ZERO, 100_000.0)

    assert result["final_capital"] == pytest.approx(100_000.0)
    assert result["total_trades"] == 0


def test_zero_close_bar_is_skipped_mid_series() -> None:
    """A suspended bar in the middle must not poison the equity curve."""
    result = be._execute_backtest(_kline(60, zero_at=30), _BUY_ALWAYS, 100_000.0)

    assert result["final_capital"] > 0
    for point in result["equity_curve"]:
        assert point["equity"] > 0, f"zero equity recorded at {point['date']}"


def test_zero_close_never_reaches_the_curve() -> None:
    result = be._execute_backtest(_kline(60, zero_at=10), _BUY_ALWAYS, 100_000.0)
    dates = [p["date"] for p in result["equity_curve"]]
    assert "2026-01-11" not in dates, "the bar with close=0 was still recorded"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
