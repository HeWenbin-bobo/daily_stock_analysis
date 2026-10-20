# -*- coding: utf-8 -*-
"""Regression test: /settings/llm must answer 400 for uncoercible values.

``llm_settings.update`` already rejects containers and bools with a ValueError
("字段 X 的值类型不支持（需要字符串）"), but the endpoint did not catch it, so
``{"fields": {"openai_model": ["x"]}}`` surfaced as a 500 internal error.

Run: ``python -m pytest update_version/backend/tests/test_phase75_llm_settings_input_guards.py``
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


@pytest.mark.parametrize("bad_value", [["x"], {"a": 1}, True])
def test_uncoercible_field_value_is_400(bad_value) -> None:
    r = _client().post("/api/v2/settings/llm", json={"fields": {"openai_model": bad_value}})
    assert r.status_code == 400, f"{bad_value!r} -> {r.status_code}: {r.text[:120]}"
    assert "openai_model" in r.json()["detail"]


def test_non_object_fields_is_400() -> None:
    r = _client().post("/api/v2/settings/llm", json={"fields": ["x"]})
    assert r.status_code == 400


def test_unknown_field_is_400() -> None:
    r = _client().post("/api/v2/settings/llm", json={"fields": {"not_a_real_field": "x"}})
    assert r.status_code == 400
    assert "未知字段" in r.json()["detail"]


def test_valid_string_value_saves() -> None:
    r = _client().post("/api/v2/settings/llm", json={"fields": {"openai_model": "gpt-4o-mini"}})
    assert r.status_code == 200
    assert "openai_model" in r.json()["saved"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
