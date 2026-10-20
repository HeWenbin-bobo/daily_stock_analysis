# -*- coding: utf-8 -*-
"""Regression test: a directly-run test file must not touch the live database.

``tests/`` has 25 files with an ``if __name__ == "__main__"`` block. Running one
of them as ``python tests/test_phase13_fixes_and_features.py`` does **not** load
``conftest.py``, so the ``V4_DB_PATH`` that conftest normally sets never
applies and ``DB_PATH`` fell back to the live ``backend/data/v2_dev.db``.

Evidence this actually happened: the live database carried three
``data_source_health`` rows named ``only_fail`` / ``only_succ`` / ``mixed`` —
pure test fixtures from ``test_phase13_fixes_and_features.py`` — and they were
rendered to users through ``/api/v2/data/health`` and ``/ready``.

Run: ``python -m pytest update_version/backend/tests/test_phase62_db_path_guard.py``
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

GUARD_PROBE = """
import sys
sys.path.insert(0, {backend!r})
from db.migrate import DB_PATH
print(DB_PATH)
"""


def _run(code: str, *, script_name: str, env_without_override: bool = True) -> str:
    """Run `code` from a script called `script_name`, return its stdout."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / script_name
        script.write_text(code.format(backend=str(BACKEND)), encoding="utf-8")
        env = dict(os.environ)
        if env_without_override:
            env.pop("V4_DB_PATH", None)
            env.pop("V4_HISTORY_PATH", None)
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert proc.returncode == 0, f"probe failed: {proc.stderr}"
        return proc.stdout.strip().splitlines()[-1].strip()


def test_test_named_script_does_not_resolve_to_live_db() -> None:
    live = BACKEND / "data" / "v2_dev.db"
    resolved = _run(GUARD_PROBE, script_name="test_direct_run_probe.py")
    assert Path(resolved) != live, (
        f"a directly-run test resolved DB_PATH to the live database {live}"
    )


def test_pytest_process_still_uses_conftest_override() -> None:
    """The guard must never hijack an explicit V4_DB_PATH.

    Checking ``DB_PATH == os.environ["V4_DB_PATH"]`` would be order-dependent:
    ``db.migrate`` resolves the path once at import, while a few test modules
    (e.g. ``test_phase10_smoke.py``) rewrite ``V4_DB_PATH`` afterwards. The
    durable invariants are that an override is set and that the live database is
    never the target — the guard's whole purpose.
    """
    from db.migrate import DB_PATH

    live = BACKEND / "data" / "v2_dev.db"
    assert os.environ.get("V4_DB_PATH"), "conftest must set V4_DB_PATH for the suite"
    assert Path(DB_PATH) != live, f"pytest is pointing at the live database {live}"


def test_application_process_still_uses_live_db() -> None:
    """A non-test entrypoint must keep resolving to the live database."""
    resolved = _run(GUARD_PROBE, script_name="app_like_entry.py")
    assert Path(resolved) == BACKEND / "data" / "v2_dev.db", (
        f"the app would write to {resolved} instead of the live DB"
    )
