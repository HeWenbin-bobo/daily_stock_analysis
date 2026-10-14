# -*- coding: utf-8 -*-
"""数据库迁移：执行 schema.sql 建表"""
import os
import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ★ 迭代2 修复（测试隔离）：DB_PATH 支持 V4_DB_PATH 环境变量覆盖。
#   破坏性测试（对表执行全表 DELETE）必须设 V4_DB_PATH 指向独立测试库，
#   防止测试清空生产库 v2_dev.db 的数据（曾导致 backtest_strategy 全表丢失）。
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "v2_dev.db"
DB_PATH = Path(os.environ.get("V4_DB_PATH") or _DEFAULT_DB)
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def migrate() -> int:
    import re
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    # SQLite executescript 不识别 # 注释，剥离所有以 # 或 -- 开头的整行注释
    lines = schema_sql.splitlines()
    cleaned = "\n".join(
        line for line in lines
        if not line.strip().startswith(("#", "--"))
    )
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript(cleaned)
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    tables = [r["name"] for r in cur.fetchall()]
    logger.info("Migration complete: %d tables created", len(tables))
    conn.close()
    return len(tables)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    n = migrate()
    print(f"Created {n} tables in {DB_PATH}")