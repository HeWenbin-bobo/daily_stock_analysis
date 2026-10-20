# -*- coding: utf-8 -*-
"""Regression test: malformed numeric inputs on /portfolio-p0/trades must be 400.

The endpoint called ``float(payload["quantity"])`` and friends directly, and the
surrounding handler only caught ``ValueError``. A list or dict raises
``TypeError`` instead, which escaped to the global handler:

    {"quantity": [100]}   -> 500
    {"price": {"a": 1}}   -> 500
    {"fee": [5]}          -> 500

All three now answer 400 via ``_coerce_float``. A caller sending the wrong type
is a client error, not a server fault.

Run: ``python -m pytest update_version/backend/tests/test_phase72_p0_trade_input_guards.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest


def _client():
    """Import lazily — endpoints_hot reorders sys.path at import time."""
    from db.migrate import migrate
    migrate()
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app)


_BASE = {
    "account_id": 1,
    "symbol": "600519",
    "trade_date": "2026-01-05",
    "side": "buy",
    "quantity": 100,
    "price": 10.0,
}


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("quantity", [100]),
        ("quantity", {"n": 100}),
        ("price", {"a": 1}),
        ("price", ["10"]),
        ("fee", [5]),
        ("tax", True),
        ("account_id", [1]),
    ],
)
def test_container_and_bool_inputs_are_rejected_with_400(field: str, bad_value) -> None:
    body = dict(_BASE)
    body[field] = bad_value
    r = _client().post("/api/v2/portfolio-p0/trades", json=body)
    assert r.status_code == 400, f"{field}={bad_value!r} -> {r.status_code}: {r.text[:120]}"
    assert field in r.json()["detail"]


def test_invalid_date_is_still_400() -> None:
    body = dict(_BASE)
    body["trade_date"] = "not-a-date"
    r = _client().post("/api/v2/portfolio-p0/trades", json=body)
    assert r.status_code == 400


def test_valid_numeric_strings_still_work() -> None:
    """Be lenient where it is safe: numeric strings are a common client habit."""
    from api.endpoints_v2 import _coerce_float
    assert _coerce_float("12.5", "x") == pytest.approx(12.5)
    assert _coerce_float(12, "x") == pytest.approx(12.0)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
