# -*- coding: utf-8 -*-
"""Regression tests for order/DSL input validation (2026-10 exploration).

Two defects found by probing the live container:

1. ``paper_trading.place_order`` validated ``quantity > 0`` and the *upper*
   bound of ``price`` but never the lower bound. ``price=0`` opened a position
   for free; ``price=-100`` wrote ``amount=-1000`` into the ledger and thereby
   corrupted position cost / leaderboard equity.
2. ``backtest_engine`` executed the DSL straight from ``compiled_dsl``, which
   is **LLM output stored verbatim**. A string threshold raised ``TypeError``
   (uncaught by the endpoint → 500) and ``take_profit_pct=-5`` silently
   inverted the take-profit logic into always-firing.

Run: ``python -m pytest update_version/backend/tests/test_phase26_input_guards.py``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

# 隔离 DB：这些用例只做参数校验，不碰真实账本。
os.environ.setdefault("V4_DB_PATH", str(BACKEND / "data" / "v2_test_phase26.db"))

import pytest

from modules import paper_trading as pt
from modules.backtest_engine import normalize_dsl


# ---------------------------------------------------------------------------
# place_order price guard
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_price", [0, 0.0, -1, -100.0])
def test_place_order_rejects_non_positive_price(bad_price: float) -> None:
    with pytest.raises(ValueError, match="price 必须 > 0"):
        pt.place_order(1, "600519", "buy", 10, price=bad_price)


def test_place_order_still_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValueError, match="quantity 必须 > 0"):
        pt.place_order(1, "600519", "buy", 0, price=100.0)


# ---------------------------------------------------------------------------
# DSL normalization
# ---------------------------------------------------------------------------
def test_normalize_dsl_accepts_numeric_strings() -> None:
    """LLM 常把阈值写成字符串；能转成数字就该接受。"""
    dsl = normalize_dsl({
        "entry": [{"indicator": "volume_ratio", "operator": ">", "value": "1.5"}],
        "exit": [],
    })
    assert dsl["entry"][0]["value"] == 1.5


def test_normalize_dsl_rejects_non_numeric_threshold() -> None:
    """无法转换的阈值必须变成 ValueError（端点层 400），而不是执行时 TypeError（500）。"""
    with pytest.raises(ValueError, match="value 必须是数字"):
        normalize_dsl({
            "entry": [{"indicator": "close", "operator": ">", "value": "abc"}],
            "exit": [],
        })


def test_normalize_dsl_rejects_bad_period() -> None:
    with pytest.raises(ValueError, match="period 必须是整数"):
        normalize_dsl({
            "entry": [{"indicator": "ma", "operator": ">", "value": 100, "period": "abc"}],
            "exit": [],
        })


def test_normalize_dsl_clamps_period_range() -> None:
    with pytest.raises(ValueError, match="period 必须在"):
        normalize_dsl({
            "entry": [{"indicator": "ma", "operator": ">", "value": 100, "period": 9999}],
            "exit": [],
        })


def test_normalize_dsl_fixes_inverted_stop_loss_and_take_profit() -> None:
    """LLM 常把止盈写成负数、止损写成正数；方向纠正比报错更有用。"""
    dsl = normalize_dsl({"entry": [], "exit": [], "stop_loss_pct": 5, "take_profit_pct": -5})
    assert dsl["stop_loss_pct"] == -5
    assert dsl["take_profit_pct"] == 5


def test_normalize_dsl_clamps_negative_hold_days() -> None:
    assert normalize_dsl({"entry": [], "exit": [], "hold_days": -3})["hold_days"] == 0


@pytest.mark.parametrize("bad", ["boll"])
def test_normalize_dsl_rejects_unknown_indicator(bad: str) -> None:
    with pytest.raises(ValueError, match="不支持的 indicator"):
        normalize_dsl({"entry": [{"indicator": bad, "operator": ">", "value": 1}], "exit": []})


def test_normalize_dsl_rejects_unknown_operator() -> None:
    with pytest.raises(ValueError, match="不支持的 operator"):
        normalize_dsl({"entry": [{"indicator": "close", "operator": "~~", "value": 1}], "exit": []})


def test_normalize_dsl_rejects_non_object() -> None:
    with pytest.raises(ValueError, match="必须是 JSON 对象"):
        normalize_dsl("not a dsl")


def test_normalize_dsl_passes_error_through() -> None:
    """compile_strategy 用 {"error": ...} 表达失败，不能把它当合法策略规范化掉。"""
    assert normalize_dsl({"error": "LLM 返回的不是有效 JSON"}) == {"error": "LLM 返回的不是有效 JSON"}
