# -*- coding: utf-8 -*-
"""Regression test: three more endpoints answered 500 on container-typed input.

* ``POST /backtest/compare-results`` — result ids went into a SQLite bind
  parameter: ``{"result_id_a": [1]}`` -> ProgrammingError
  ("type 'list' is not supported").
* ``POST /market-review/generate`` — ``(review_date, region)`` is used as a dict
  key for in-flight de-duplication, and a list is unhashable:
  ``{"review_date": [1]}`` -> TypeError: unhashable type: 'list'.
* ``POST /market-review/batch`` — ``days`` reaches ``range()``/``timedelta``:
  ``{"days": [7]}`` -> TypeError: 'list' object cannot be interpreted as an
  integer.

Run: ``python -m pytest update_version/backend/tests/test_phase76_days_and_compare_guards.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from fastapi import HTTPException


def _client():
    from db.migrate import migrate
    migrate()
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app)


def _hot():
    from api import endpoints_hot
    return endpoints_hot


@pytest.mark.parametrize("bad_id", [[1], {"a": 1}, "abc"])
def test_compare_results_rejects_bad_ids(bad_id) -> None:
    r = _client().post(
        "/api/v2/backtest/compare-results", json={"result_id_a": bad_id, "result_id_b": 2}
    )
    assert r.status_code == 400, f"{bad_id!r} -> {r.status_code}: {r.text[:120]}"


@pytest.mark.parametrize("bad", [[1], {"a": 1}, "   ", "bad-date"])
def test_generate_rejects_bad_review_date(bad) -> None:
    eh = _hot()
    with pytest.raises(HTTPException) as exc:
        eh.market_review_generate({"review_date": bad}, request=None)
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "days,expected_fragment",
    [([7], "整数"), ({"a": 1}, "整数"), ("abc", "整数"), (0, "1~365"), (-1, "1~365"), (9999, "1~365")],
)
def test_batch_rejects_bad_days(days, expected_fragment: str) -> None:
    eh = _hot()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(eh.market_review_batch({"days": days}, request=None))
    assert exc.value.status_code == 400
    assert expected_fragment in str(exc.value.detail)


def test_compare_results_accepts_valid_ids() -> None:
    """Valid but non-existent ids are a normal 200 with an error payload."""
    r = _client().post(
        "/api/v2/backtest/compare-results", json={"result_id_a": 1, "result_id_b": 2}
    )
    assert r.status_code == 200


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
