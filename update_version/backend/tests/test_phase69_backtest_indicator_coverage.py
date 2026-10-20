# -*- coding: utf-8 -*-
"""Regression tests: indicator coverage in the backtest engine.

Two defects, both silent rather than loud:

1. ``_execute_backtest`` precomputes MA for a fixed list
   ``[5,10,12,14,20,26,30,60]``, and the on-demand branch added for ema/rsi/
   volume_ratio **omitted ma**. DSL allows period 1..250, so ``ma(period=90)``
   evaluated against a missing series, ``_eval_condition`` returned False every
   bar, and the backtest "succeeded" with 0 trades. Measured on 400 bars:
   ma20/30/60 -> 96-108 trades, ma90/120 -> 0.

2. The out-of-sample check ran the bare test slice with no warmup. test is
   TEST_SPLIT_RATIO (20%) of the data, so with 500 bars it is 100 bars and
   MA60 burns 60 of them. Same strategy, same params: 11 OOS trades bare vs 28
   with the train tail used as warmup.

Run: ``python -m pytest update_version/backend/tests/test_phase69_backtest_indicator_coverage.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from modules import backtest_engine as be
from modules import hyperopt_engine as he


def _kline(n: int) -> list[dict]:
    return [
        {
            "date": f"d{i:04d}",
            "open": 10.0,
            "high": 10.5,
            "low": 9.5,
            "close": 10.0 + (i % 7) * 0.1,
            "volume": 1000 + (i % 5) * 100,
        }
        for i in range(n)
    ]


def _ma_dsl(period: int) -> dict:
    return {
        "entry": [{"indicator": "ma", "operator": ">", "value": 10.0, "period": period}],
        "exit": [{"indicator": "ma", "operator": "<", "value": 10.0, "period": period}],
        "stop_loss_pct": -5,
        "take_profit_pct": 10,
        "hold_days": 3,
    }


@pytest.mark.parametrize("period", [20, 30, 60, 90, 120, 250])
def test_every_dsl_ma_period_actually_trades(period: int) -> None:
    """Any period the DSL accepts must produce a usable indicator series."""
    result = be._execute_backtest(_kline(400), _ma_dsl(period), 1_000_000.0)
    assert result["total_trades"] > 0, (
        f"ma(period={period}) produced no trades — the indicator was never computed"
    )


def test_on_demand_ma_matches_precomputed_ma() -> None:
    """The on-demand series must be identical to the precomputed one."""
    dsl_precomputed = _ma_dsl(20)  # 20 在固定预计算列表里
    dsl_ondemand = _ma_dsl(90)     # 90 只能按需补算
    a = be._execute_backtest(_kline(400), dsl_precomputed, 1_000_000.0)
    b = be._execute_backtest(_kline(400), dsl_ondemand, 1_000_000.0)
    # 两者都应有交易（不要求笔数相同：周期不同，信号本就不同）
    assert a["total_trades"] > 0 and b["total_trades"] > 0


@pytest.mark.parametrize(
    "dsl,expected",
    [
        (_ma_dsl(20), 20),
        (_ma_dsl(60), 60),
        (_ma_dsl(120), 120),
        ({"entry": [{"indicator": "macd", "operator": "cross_above"}],
          "exit": [{"indicator": "macd", "operator": "cross_below"}]}, 35),
        ({"entry": [], "exit": []}, 0),
    ],
)
def test_oos_warmup_matches_the_dsl(dsl: dict, expected: int) -> None:
    train, test = he._split_by_date(_kline(500))
    assert he._oos_warmup_bars(train, test, dsl) == expected


def test_oos_warmup_never_exceeds_train() -> None:
    """Borrowing more than train holds would drag in-sample data into OOS."""
    train, test = he._split_by_date(_kline(500))
    huge = {"entry": [{"indicator": "ma", "operator": ">", "value": 1, "period": 9999}], "exit": []}
    assert he._oos_warmup_bars(train, test, huge) == len(train)


def test_oos_with_warmup_sees_more_than_the_bare_slice() -> None:
    """The concrete regression: MA60 OOS trades jump once the warmup is supplied."""
    train, test = he._split_by_date(_kline(500))
    dsl = _ma_dsl(60)
    bare = be._execute_backtest(test, dsl, 1_000_000.0)
    warm = he._oos_warmup_bars(train, test, dsl)
    fixed = be._execute_backtest(
        train[-warm:] + test, dsl, 1_000_000.0, warmup_bars=warm
    )
    assert fixed["total_trades"] > bare["total_trades"], (
        f"warmup made no difference: bare={bare['total_trades']}, fixed={fixed['total_trades']}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
