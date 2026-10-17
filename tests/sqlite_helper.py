# -*- coding: utf-8 -*-
"""SQLite connection helper for tests.

``with sqlite3.connect(...) as conn`` only commits the transaction — it does
**not** close the connection (unlike file objects). The connection then stays
alive in the test's local scope, and on Windows an open handle makes
``TemporaryDirectory.cleanup()`` fail with ``PermissionError: [WinError 32]``.
POSIX unlinks open files happily, so the leak stayed hidden on CI.

Use ``sqlite_conn(path)`` where ``sqlite3.connect(path)`` was used: it keeps
the commit-on-success semantics and closes the connection on exit.
"""

from __future__ import annotations

import sqlite3
import tempfile
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def sqlite_conn(db_path: str) -> Iterator[sqlite3.Connection]:
    """Open ``db_path``, commit on clean exit, and always close the handle."""
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def temp_db_dir() -> Iterator[str]:
    """``TemporaryDirectory`` that disposes ``DatabaseManager`` before removal.

    ``unittest`` runs ``tearDown`` *after* the ``with`` block already deleted
    the directory, so an engine opened inside it still holds the SQLite file
    and Windows refuses the unlink (``WinError 32``).
    """
    from src.storage import DatabaseManager

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            yield tmpdir
        finally:
            DatabaseManager.reset_instance()
