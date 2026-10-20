# -*- coding: utf-8 -*-
"""Regression test: symbol_filter must be a string before it reaches SQLite.

The value went straight into a bind parameter, so a list or dict raised
``sqlite3.ProgrammingError: Error binding parameter 4: type 'list' is not
supported``. The endpoint only catches ValueError, so
``{"symbol_filter": [1]}`` came back as 500.

The guard lives in ``create_rule`` — the shared entry point — rather than the
HTTP layer.

Run: ``python -m pytest update_version/backend/tests/test_phase79_alert_rule_symbol_filter.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from modules import alerts_engine as ae


@pytest.mark.parametrize("bad", [[1], {"a": 1}, (1,), {1}])
def test_container_symbol_filter_is_rejected(bad) -> None:
    with pytest.raises(ValueError, match="symbol_filter"):
        ae.create_rule("u", "n", "limit_up", {}, symbol_filter=bad)


def test_string_symbol_filter_still_works() -> None:
    rid = ae.create_rule("u", "with_filter", "limit_up", {}, symbol_filter="600519,000858")
    assert rid > 0
    assert ae.get_rule(rid)["symbol_filter"] == "600519,000858"


def test_none_symbol_filter_still_works() -> None:
    rid = ae.create_rule("u", "no_filter", "limit_up", {})
    assert rid > 0
    assert ae.get_rule(rid)["symbol_filter"] is None


# ---------------------------------------------------------------------------
# update_rule: the same class of defect on the PUT path
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("field,bad", [
    ("name", [1]),
    ("name", {"a": 1}),
    ("rule_type", [1]),          # 旧实现走 `v not in RULE_TYPES` → TypeError: unhashable
    ("rule_type", {"a": 1}),
    ("symbol_filter", [1]),
    ("symbol_filter", {"a": 1}),
])
def test_update_rule_rejects_container_fields(field: str, bad) -> None:
    rid = ae.create_rule("u", "upd", "limit_up", {})
    with pytest.raises(ValueError, match=field):
        ae.update_rule(rid, **{field: bad})
    # 未被改写
    assert ae.get_rule(rid)[field if field != "rule_type" else "rule_type"] == (
        "limit_up" if field == "rule_type" else (None if field == "symbol_filter" else "upd")
    )


def test_update_rule_still_accepts_valid_strings() -> None:
    rid = ae.create_rule("u", "upd2", "limit_up", {})
    assert ae.update_rule(rid, name="renamed", symbol_filter="600519")
    got = ae.get_rule(rid)
    assert got["name"] == "renamed"
    assert got["symbol_filter"] == "600519"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
