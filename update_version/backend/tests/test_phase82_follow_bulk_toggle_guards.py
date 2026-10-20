# -*- coding: utf-8 -*-
"""Regression test: community follow and alert bulk-toggle rejected containers.

Both put caller values straight into SQLite bind parameters, so a list/dict
raised ``ProgrammingError: Error binding parameter N: type 'list' is not
supported`` while the endpoints only catch ValueError. Verified live:

    POST /community/follow  {"followee_id": [1]}      -> 500
    POST /community/unfollow {"followee_id": {"x": 1}} -> 500
    POST /alerts/rules/bulk-toggle {"rule_ids": [[1]]} -> 500

Run: ``python -m pytest update_version/backend/tests/test_phase82_follow_bulk_toggle_guards.py``
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


_H = {"X-User-Id": "u_guard"}


# JSON 只能表达 list / dict（tuple、set 无法序列化），只测这两种。
# 注意不发送 X-User-Id：带上 header 时 _check_resource_owner 会先按
# 「payload 身份与 header 不一致」返回 403 —— 那是正确的行为，但会把
# 本用例想验证的类型校验挡在后面。
@pytest.mark.parametrize("bad", [[1], {"a": 1}])
@pytest.mark.parametrize("path", ["/api/v2/community/follow", "/api/v2/community/unfollow"])
@pytest.mark.parametrize("field", ["follower_id", "followee_id"])
def test_container_ids_are_400(path: str, field: str, bad) -> None:
    body = {"follower_id": "a", "followee_id": "b"}
    body[field] = bad
    r = _client().post(path, json=body)
    assert r.status_code == 400, f"{path} {field}={bad!r} -> {r.status_code}: {r.text[:120]}"
    assert field in r.json()["detail"]


def test_mismatched_header_still_403() -> None:
    """身份不一致时 403 优先于类型校验 —— 这是既有的安全语义。"""
    r = _client().post(
        "/api/v2/community/follow",
        json={"follower_id": [1], "followee_id": "b"},
        headers=_H,
    )
    assert r.status_code == 403


def test_follow_round_trip_still_works() -> None:
    c = _client()
    assert c.post("/api/v2/community/follow",
                  json={"follower_id": "u_guard", "followee_id": "someone"}, headers=_H).status_code == 200
    assert c.post("/api/v2/community/unfollow",
                  json={"follower_id": "u_guard", "followee_id": "someone"}, headers=_H).status_code == 200


@pytest.mark.parametrize("bad_ids", [[[1]], [{"a": 1}], "1", []])
def test_bulk_toggle_rejects_bad_rule_ids(bad_ids) -> None:
    r = _client().post("/api/v2/alerts/rules/bulk-toggle",
                       json={"rule_ids": bad_ids, "enabled": True}, headers=_H)
    assert r.status_code == 400, f"rule_ids={bad_ids!r} -> {r.status_code}: {r.text[:120]}"


def test_bulk_toggle_still_works() -> None:
    c = _client()
    rid = c.post("/api/v2/alerts/rules",
                 json={"user_id": "u_guard", "name": "n", "rule_type": "limit_up", "params": {}},
                 headers=_H).json()["rule_id"]
    r = c.post("/api/v2/alerts/rules/bulk-toggle", json={"rule_ids": [rid], "enabled": False}, headers=_H)
    assert r.status_code == 200
    assert r.json()["affected"] == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
