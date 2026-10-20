# -*- coding: utf-8 -*-
"""Regression test: /llm/chat must answer 400 for non-string routing fields.

``task_complexity`` is used as a dict key when resolving the provider, so a list
raised ``TypeError: unhashable type: 'list'`` and the endpoint returned 500.
Verified live: ``{"prompt": "x", "task_complexity": [1]}``.

``task_type`` had the same shape of risk and is now guarded alongside it.

Run: ``python -m pytest update_version/backend/tests/test_phase81_llm_chat_input_guards.py``
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


@pytest.mark.parametrize(
    "field,bad",
    [
        ("task_complexity", [1]),
        ("task_complexity", {"a": 1}),
        ("task_type", [1]),
        ("task_type", {"a": 1}),
    ],
)
def test_non_string_routing_fields_are_400(field: str, bad) -> None:
    r = _client().post("/api/v2/llm/chat", json={"prompt": "x", field: bad})
    assert r.status_code == 400, f"{field}={bad!r} -> {r.status_code}: {r.text[:120]}"
    assert field in r.json()["detail"]


@pytest.mark.parametrize("bad", [[1], {"a": 1}, "", "   "])
def test_bad_prompt_is_400(bad) -> None:
    r = _client().post("/api/v2/llm/chat", json={"prompt": bad})
    assert r.status_code == 400


def test_oversized_prompt_is_400() -> None:
    r = _client().post("/api/v2/llm/chat", json={"prompt": "x" * 100_001})
    assert r.status_code == 400


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
