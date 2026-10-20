# -*- coding: utf-8 -*-
"""Regression test: /hot-analysis/templates must answer 400 for container input.

``name`` and ``description`` went straight into SQLite bind parameters, so a
list or dict raised ``ProgrammingError: Error binding parameter 1: type 'list'
is not supported`` and the endpoint answered 500. Verified live:
``{"name": [1], "config": {}}``.

Run: ``python -m pytest update_version/backend/tests/test_phase83_template_input_guards.py``
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


@pytest.mark.parametrize("bad", [[1], {"a": 1}, "", "   "])
def test_bad_template_name_is_400(bad) -> None:
    r = _client().post("/api/v1/hot-analysis/templates", json={"name": bad, "config": {}})
    assert r.status_code == 400, f"name={bad!r} -> {r.status_code}: {r.text[:120]}"
    assert "name" in r.json()["detail"]


@pytest.mark.parametrize("bad", [[], [1], "not-an-object"])
def test_bad_template_config_is_400(bad) -> None:
    r = _client().post("/api/v1/hot-analysis/templates", json={"name": "t", "config": bad})
    assert r.status_code == 400, f"config={bad!r} -> {r.status_code}: {r.text[:120]}"
    assert "config" in r.json()["detail"]


@pytest.mark.parametrize("bad", [[1], {"a": 1}])
def test_bad_template_description_is_400(bad) -> None:
    r = _client().post(
        "/api/v1/hot-analysis/templates", json={"name": "t", "description": bad}
    )
    assert r.status_code == 400
    assert "description" in r.json()["detail"]


def test_valid_template_is_saved() -> None:
    r = _client().post(
        "/api/v1/hot-analysis/templates",
        json={"name": "guard_test_tpl", "config": {"rounds": 3}, "description": "d"},
    )
    assert r.status_code == 200
    assert r.json()["id"] > 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
