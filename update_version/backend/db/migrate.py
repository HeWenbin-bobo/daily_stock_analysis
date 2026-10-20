# -*- coding: utf-8 -*-
"""数据库迁移：执行 schema.sql 建表"""
import os
import sqlite3
import logging
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# ★ 迭代2 修复（测试隔离）：DB_PATH 支持 V4_DB_PATH 环境变量覆盖。
#   破坏性测试（对表执行全表 DELETE）必须设 V4_DB_PATH 指向独立测试库，
#   防止测试清空生产库 v2_dev.db 的数据（曾导致 backtest_strategy 全表丢失）。
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "v2_dev.db"


def _running_as_test() -> bool:
    """当前进程是不是测试进程。

    ★ 探索期修复(测试数据漏进生产库)：`tests/` 下 30 个文件里有 25 个带
      `if __name__ == "__main__"` 直跑入口。用 `python tests/test_phase13_*.py`
      直接跑时 **conftest.py 不会被加载**，因此它 `setdefault` 的 V4_DB_PATH
      不生效，DB_PATH 回落到生产库 `backend/data/v2_dev.db` ——
      实测生产库里因此长期留着 `only_fail` / `only_succ` / `mixed` 三条测试夹具
      的 data_source_health 记录，并通过 `/api/v2/data/health` 和 `/ready`
      直接展示给用户。
    """
    argv0 = Path(sys.argv[0] or "").name
    if argv0.startswith("test_") or argv0.endswith("_test.py"):
        return True
    return "pytest" in sys.modules


def _resolve_db_path() -> Path:
    """解析数据库路径；测试进程一律不碰生产库。"""
    override = os.environ.get("V4_DB_PATH")
    if override:
        return Path(override)
    if _running_as_test():
        fallback = Path(tempfile.gettempdir()) / "dsa_v2_direct_run.db"
        logger.warning(
            "test process without V4_DB_PATH; using %s instead of the live DB %s",
            fallback, _DEFAULT_DB,
        )
        return fallback
    return _DEFAULT_DB


DB_PATH = _resolve_db_path()
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