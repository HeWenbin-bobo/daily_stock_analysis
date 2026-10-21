# -*- coding: utf-8 -*-
"""Regression test: /notifications/test must answer 400 for container-typed input.

channel_id went straight into a SQLite query, so ``{"channel_id": [1]}`` raised
ProgrammingError and surfaced as 500. title/content had the same problem via the
notification_log insert.

Run: ``python -m pytest update_version/backend/tests/test_phase77_notification_test_guards.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest


# 注：限流桶的重置由 tests/conftest.py 的 `_reset_ratelimit_buckets` 统一处理
# （`/notifications/test` 只有 5 次/分钟，本模块自己就发 5 个请求）


def _client():
    from db.migrate import migrate
    migrate()
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app)


@pytest.mark.parametrize("bad_id", [[1], {"a": 1}, "abc"])
def test_container_channel_id_is_400(bad_id) -> None:
    r = _client().post(
        "/api/v2/notifications/test", json={"channel_id": bad_id}, headers={"X-User-Id": "u"}
    )
    assert r.status_code == 400, f"{bad_id!r} -> {r.status_code}: {r.text[:120]}"
    assert "channel_id" in r.json()["detail"]


def test_missing_channel_is_404() -> None:
    r = _client().post(
        "/api/v2/notifications/test", json={"channel_id": 9999}, headers={"X-User-Id": "u"}
    )
    assert r.status_code == 404


def test_container_title_is_400() -> None:
    from core import notifier
    ch = notifier.add_channel("u", "serverchan", "SCT123abc")
    r = _client().post(
        "/api/v2/notifications/test",
        json={"channel_id": ch, "title": {"a": 1}},
        headers={"X-User-Id": "u"},
    )
    assert r.status_code == 400
    assert "title" in r.json()["detail"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
