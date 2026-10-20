# -*- coding: utf-8 -*-
"""Regression test: /paper/transfer must answer 400 for container-typed input.

Account ids went straight into SQLite lookups and amount into arithmetic, so
containers raised ProgrammingError / TypeError and the endpoint only caught
ValueError. Verified live before the fix — all four answered 500:

    {"amount": [100]}
    {"amount": {"a": 1}}
    {"from_account_id": [1]}
    {"amount": "nan"}

Run: ``python -m pytest update_version/backend/tests/test_phase80_transfer_input_guards.py``
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


@pytest.fixture()
def accounts():
    from modules import paper_trading as pt
    a = pt.create_account("u_xfer2", "a", 100_000.0)
    b = pt.create_account("u_xfer2", "b", 100_000.0)
    return a, b


@pytest.mark.parametrize("field,bad", [
    ("amount", [100]),
    ("amount", {"a": 1}),
    ("amount", "nan"),
    ("amount", "inf"),
    ("from_account_id", [1]),
    ("to_account_id", {"a": 1}),
])
def test_container_or_nonfinite_fields_are_400(accounts, field: str, bad) -> None:
    a, b = accounts
    body = {"from_account_id": a, "to_account_id": b, "amount": 100}
    body[field] = bad
    r = _client().post("/api/v2/paper/transfer", json=body, headers={"X-User-Id": "u_xfer2"})
    assert r.status_code == 400, f"{field}={bad!r} -> {r.status_code}: {r.text[:120]}"
    assert field in r.json()["detail"]


def test_valid_transfer_still_works(accounts) -> None:
    a, b = accounts
    r = _client().post(
        "/api/v2/paper/transfer",
        json={"from_account_id": a, "to_account_id": b, "amount": 100},
        headers={"X-User-Id": "u_xfer2"},
    )
    assert r.status_code == 200
    assert r.json()["amount"] == 100


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
