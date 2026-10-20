# -*- coding: utf-8 -*-
"""Regression test: the scheduled-task watcher must always write a terminal state.

``_watch_scheduled_persist`` polls the main-project task store and records the
outcome on ``scheduled_tasks.last_status``. Two paths used to return without
recording anything:

* the analysis task dictionary disappeared (main app restarted, or its
  ``_cleanup_old_tasks`` pruned it)
* the 7200s deadline elapsed

In both cases ``last_status`` stayed ``running`` forever. The UI renders that as
"运行中" and the user cannot re-run the task; only a backend restart would
correct it, via ``reset_stuck_running_tasks``.

Run: ``python -m pytest update_version/backend/tests/test_phase71_scheduled_watcher_terminal_state.py``
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest


def _hot():
    """Import lazily: endpoints_hot rewrites sys.path at import time (it reorders
    the main-project root to position 0 to shadow the same-named `api` package),
    which breaks the conftest session fixture's `from db.migrate import migrate`.
    Doing it per-test keeps collection clean.
    """
    from api import endpoints_hot
    return endpoints_hot


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def __call__(self, task_id: int, status: str, result_id=None) -> None:
        self.calls.append((task_id, status))


def _run(coro):
    return asyncio.run(coro)


def test_vanished_task_marks_stale(monkeypatch) -> None:
    eh = _hot()
    rec = _Recorder()
    monkeypatch.setattr(eh.db_history, "update_task_run", rec)
    monkeypatch.setattr(eh, "_main_task", lambda _tid: None)

    _run(eh._watch_scheduled_persist(7, "gone-task", timeout=30))

    assert rec.calls == [(7, "stale")], rec.calls


def test_deadline_expiry_marks_timeout(monkeypatch) -> None:
    eh = _hot()
    rec = _Recorder()
    monkeypatch.setattr(eh.db_history, "update_task_run", rec)
    # 任务一直在跑，永远不会终态
    monkeypatch.setattr(eh, "_main_task", lambda _tid: {"status": "running", "phase": "analyzing"})

    async def _fake_sleep(_seconds):
        return None

    monkeypatch.setattr(eh.asyncio, "sleep", _fake_sleep)

    # timeout<=0 让 while 条件立即为假，直接走到超时分支
    _run(eh._watch_scheduled_persist(9, "slow-task", timeout=-1))

    assert rec.calls == [(9, "timeout")], rec.calls


def test_completed_task_records_its_status(monkeypatch) -> None:
    eh = _hot()
    rec = _Recorder()
    persisted: list[tuple[str, dict]] = []
    monkeypatch.setattr(eh.db_history, "update_task_run", rec)
    monkeypatch.setattr(eh, "_persist_async_result", lambda tid, res: persisted.append((tid, res)))
    monkeypatch.setattr(
        eh, "_main_task",
        lambda _tid: {"status": "ok", "phase": "completed", "result": {"status": "ok", "n": 1}},
    )

    _run(eh._watch_scheduled_persist(11, "done-task", timeout=30))

    assert rec.calls == [(11, "ok")]
    assert persisted and persisted[0][0] == "done-task"


def test_exception_does_not_propagate(monkeypatch) -> None:
    """The watcher runs in a daemon thread; an escaping exception would kill it."""
    eh = _hot()

    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(eh.db_history, "update_task_run", _boom)
    monkeypatch.setattr(eh, "_main_task", lambda _tid: {"status": "ok", "result": {"status": "ok"}})

    _run(eh._watch_scheduled_persist(13, "x", timeout=30))  # must not raise


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
