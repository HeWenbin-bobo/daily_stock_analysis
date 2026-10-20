# -*- coding: utf-8 -*-
"""Regression test: malformed share payloads must be 400, not 500.

``create_share`` passed ``resource_id`` and ``title`` straight into the SQLite
bind parameters. sqlite3 raises ``ProgrammingError`` for list/dict values, and
the endpoint only caught ``ValueError``, so:

    {"resource_id": [1]}   -> 500
    {"title": {"a": 1}}    -> 500

Both are client errors. The guard lives in ``create_share`` because that is the
shared entry point for every caller, not just the HTTP layer.

Run: ``python -m pytest update_version/backend/tests/test_phase73_share_input_guards.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from modules import community as cm


@pytest.mark.parametrize("bad_id", [[1], {"a": 1}, (1,), {1}])
def test_container_resource_id_is_rejected(bad_id) -> None:
    with pytest.raises(ValueError, match="resource_id"):
        cm.create_share("u", "analysis_run", bad_id)


@pytest.mark.parametrize("bad_title", [{"a": 1}, ["x"], (1,), {1}])
def test_container_title_is_rejected(bad_title) -> None:
    with pytest.raises(ValueError, match="title"):
        cm.create_share("u", "analysis_run", "1", title=bad_title)


def test_none_resource_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="resource_id"):
        cm.create_share("u", "analysis_run", None)


def test_numeric_title_is_coerced() -> None:
    """A number is harmless — coerce rather than reject."""
    share = cm.create_share("u", "analysis_run", "1", title=123)
    assert share["title"] == "123"


def test_valid_share_still_works() -> None:
    share = cm.create_share("u", "analysis_run", "1", title="hello")
    assert share["title"] == "hello"
    assert share["share_token"]
    assert share["share_url"] == f"/s/{share['share_token']}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
