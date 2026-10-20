# -*- coding: utf-8 -*-
"""Regression test: container-typed fields must be 400 on two more write paths.

Both endpoints were probed with containers and answered 500:

* ``POST /portfolio/holdings`` — ``symbol`` went straight into a SQLite bind
  parameter (ProgrammingError for a list), and ``cost``/``quantity`` hit
  ``cost <= 0`` inside ``add_holding`` (TypeError for a list). Neither is a
  ValueError, so the endpoint's handler missed them.
* ``POST /deep-research`` — a dict/list ``code`` reached ``code.strip()`` and
  raised AttributeError, surfacing as "深度研究失败: 'dict' object has no
  attribute 'strip'".

Run: ``python -m pytest update_version/backend/tests/test_phase74_holdings_deep_research_guards.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest


def _client():
    from db.migrate import migrate
    migrate()
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app)


_HOLDING = {"user_id": "u", "symbol": "600519", "cost": 10.0, "quantity": 100}


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("symbol", ["600519"]),
        ("symbol", {"a": 1}),
        ("symbol", "   "),
        ("cost", [10]),
        ("cost", {"a": 1}),
        ("quantity", [100]),
        ("quantity", {"a": 1}),
    ],
)
def test_malformed_holding_is_400(field: str, bad_value) -> None:
    body = dict(_HOLDING)
    body[field] = bad_value
    r = _client().post("/api/v2/portfolio/holdings", json=body)
    assert r.status_code == 400, f"{field}={bad_value!r} -> {r.status_code}: {r.text[:120]}"


def test_valid_holding_still_accepted() -> None:
    r = _client().post("/api/v2/portfolio/holdings", json=_HOLDING)
    assert r.status_code == 200
    assert r.json()["trade_id"]


@pytest.mark.parametrize("bad_code", [{"a": 1}, ["600519"], "   "])
def test_deep_research_rejects_non_string_code(bad_code) -> None:
    r = _client().post("/api/v2/deep-research", json={"code": bad_code})
    assert r.status_code == 400, f"code={bad_code!r} -> {r.status_code}: {r.text[:120]}"


@pytest.mark.parametrize("bad_cost", ["nan", "NaN", "inf", "-inf", "1e400"])
def test_non_finite_cost_is_400(bad_cost: str) -> None:
    """float("nan") is a parseable call but not a valid business value.

    NaN passes every `<= 0` / `> upper` comparison (NaN compares False to
    everything), then blows up at the SQLite bind or JSON encode step:
    verified live, {"cost": "nan"} returned 500 internal error.
    """
    body = dict(_HOLDING)
    body["cost"] = bad_cost
    r = _client().post("/api/v2/portfolio/holdings", json=body)
    assert r.status_code == 400, f"cost={bad_cost!r} -> {r.status_code}: {r.text[:120]}"


def test_coerce_float_rejects_non_finite() -> None:
    from api.endpoints_v2 import _coerce_float
    from fastapi import HTTPException

    for bad in ("nan", "inf", float("nan"), float("inf")):
        with pytest.raises(HTTPException) as exc:
            _coerce_float(bad, "x")
        assert exc.value.status_code == 400
    assert _coerce_float("12.5", "x") == pytest.approx(12.5)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
