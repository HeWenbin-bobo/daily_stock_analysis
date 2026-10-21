# -*- coding: utf-8 -*-
"""Regression test: /audit/stats must not leak a database connection per call.

``audit_stats`` did ``conn = get_conn()``, ran three queries and returned without
closing. Measured against the running container: 30 requests took the process
from 17 to 47 open fds, and the count did not fall back after 15s idle.

This is a *polled* endpoint — a dashboard hitting it once a minute accumulates
~1440 fds a day, and fd exhaustion takes the whole service down. Contrast with
the same pattern in a short-lived test process, where ``gc.collect()`` reclaims
everything: the leak only shows up in a long-running server whose generational
GC may not run for a long time.

Run: ``python -m pytest update_version/backend/tests/test_phase87_audit_stats_no_fd_leak.py``
"""

from __future__ import annotations

import gc
import sqlite3
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


def _live_connections() -> int:
    gc.collect()
    return sum(1 for o in gc.get_objects() if isinstance(o, sqlite3.Connection))


def test_repeated_audit_stats_does_not_accumulate_connections() -> None:
    c = _client()
    for _ in range(3):          # 预热：首次请求会触发 migrate / 依赖初始化
        c.get("/api/v2/audit/stats", headers={"X-User-Id": "default"})

    baseline = _live_connections()
    for _ in range(50):
        r = c.get("/api/v2/audit/stats", headers={"X-User-Id": "default"})
        assert r.status_code == 200

    leaked = _live_connections() - baseline
    assert leaked == 0, f"50 次调用泄漏了 {leaked} 个数据库连接（应显式 close）"


def test_audit_stats_still_returns_the_expected_shape() -> None:
    """补连接关闭不能改变响应结构。"""
    r = _client().get("/api/v2/audit/stats?days=7", headers={"X-User-Id": "default"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"days", "total", "errors", "by_action"}
    assert body["days"] == 7
    assert isinstance(body["by_action"], list)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
