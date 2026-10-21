# -*- coding: utf-8 -*-
"""Regression test: migrate() must add newly registered columns to old databases.

``migrate()`` only ran ``CREATE TABLE IF NOT EXISTS``, so a column added to
schema.sql appeared in fresh databases and was silently missing in existing
ones — breaking only on the upgrade path (`no such column`), which is the path
nobody runs in CI. ``_add_missing_columns`` now closes that gap.

Run: ``python -m pytest update_version/backend/tests/test_phase86_column_migration.py``
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from db import migrate as mig


_OLD_HYPEROPT_RUN = """
CREATE TABLE hyperopt_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    epochs INTEGER, loss_fn TEXT,
    train_start TEXT, train_end TEXT, test_start TEXT, test_end TEXT,
    best_params_json TEXT, best_loss REAL, best_train_json TEXT,
    oos_metrics_json TEXT, oos_loss REAL,
    status TEXT DEFAULT 'running', error_message TEXT,
    created_at REAL NOT NULL, completed_at REAL
)
"""


@pytest.fixture()
def old_db(tmp_path, monkeypatch):
    """A database shaped like the pre-migration schema."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute(_OLD_HYPEROPT_RUN)
    conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(hyperopt_run)")}
    conn.close()
    assert "prev_dsl_json" not in cols, "fixture should start without the new column"
    monkeypatch.setattr(mig, "DB_PATH", path)
    return path


def _columns(path: Path) -> set:
    conn = sqlite3.connect(str(path))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(hyperopt_run)")}
    finally:
        conn.close()


def test_migration_adds_the_registered_column(old_db) -> None:
    mig.migrate()
    assert "prev_dsl_json" in _columns(old_db)


def test_migration_is_idempotent(old_db) -> None:
    mig.migrate()
    mig.migrate()
    mig.migrate()
    cols = _columns(old_db)
    assert "prev_dsl_json" in cols
    # 重复执行不应重复加列或破坏结构
    conn = sqlite3.connect(str(old_db))
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()


def test_migration_tolerates_absent_table(tmp_path, monkeypatch) -> None:
    """A registered table that doesn't exist yet must not raise."""
    path = tmp_path / "empty.db"
    sqlite3.connect(str(path)).close()
    monkeypatch.setattr(mig, "DB_PATH", path)
    mig.migrate()  # schema.sql 会建表；这里只要求不抛异常
    assert path.exists()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
