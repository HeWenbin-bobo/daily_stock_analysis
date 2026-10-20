# -*- coding: utf-8 -*-
"""Regression test: a user job_id cannot be hi-jacked by another user.

``job_id`` is a caller-controlled string and also the primary key of
``cron_user_jobs``, and ``add_user_job`` wrote it with ``INSERT OR REPLACE``.
The DELETE endpoint had an owner check but the write path did not, so any
caller could take over someone else's job by reusing its id.

Reproduced against the live container before the fix:

    alice: POST {"job_id":"shared_job_probe","func_name":"run_alert_scan",...}
    bob:   POST {"job_id":"shared_job_probe","func_name":"run_paper_settle",...}
    -> the stored row became user_id=bob, func=run_paper_settle, cron='0 3 * * *'

Run: ``python -m pytest update_version/backend/tests/test_phase65_user_job_ownership.py``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from core import cron


def _jobs() -> dict[str, dict]:
    return {j["job_id"]: j for j in cron.list_user_jobs()}


def test_first_owner_can_update_own_job() -> None:
    ok = cron.add_user_job("alice", "own_job", "run_alert_scan", "*/7 * * * *")
    assert ok, "the first writer must be able to create the job"
    assert cron.add_user_job("alice", "own_job", "run_paper_settle", "0 3 * * *")
    job = _jobs()["own_job"]
    assert job["user_id"] == "alice"
    assert job["func_name"] == "run_paper_settle"
    cron.remove_user_job("own_job")


def test_other_user_cannot_overwrite_the_job() -> None:
    assert cron.add_user_job("alice", "shared_job", "run_alert_scan", "*/7 * * * *")

    hijacked = cron.add_user_job("bob", "shared_job", "run_paper_settle", "0 3 * * *")

    assert hijacked is False, "bob must not be able to take over alice's job"
    job = _jobs()["shared_job"]
    assert job["user_id"] == "alice", f"ownership changed to {job['user_id']}"
    assert job["func_name"] == "run_alert_scan", "the function was rewritten"
    assert job["cron_expr"] == "*/7 * * * *", "the schedule was rewritten"
    cron.remove_user_job("shared_job")


def test_builtin_prefix_still_rejected() -> None:
    assert cron.add_user_job("alice", "builtin_alert_scan", "run_alert_scan", "*/5 * * * *") is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
