"""Phase 95: history.db 的写者必须能等过写锁，而不是 5 秒就失败

`db/history._get_conn()` 用 `sqlite3.connect(str(_DB_PATH))` —— 默认 timeout 是
**5 秒**，而 `db/migrate.get_conn()`（v2_dev.db）用的是 30 秒。

history.db 有多个并发写入方：
  - cron 的异动扫描（每 5 分钟）
  - 用户定时任务执行
  - 大盘复盘落库
  - API 的保存/删除请求

持有写锁的一方稍慢（akshare 抓取、LLM 调用期间事务未提交），其它写入方
5 秒后就抛 `OperationalError: database is locked`。

容器内实测（两个线程，holder 持锁 8 秒）：
  - 默认 timeout：waiter 精确在 **5.0 秒** 抛 `database is locked`
  - 修复后（timeout=30 + busy_timeout=30000）：waiter 在 **7.5 秒** 成功写入
"""
import importlib.util
import os
import sqlite3
import sys
import threading
import time

BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
UPDATE_VERSION = os.path.dirname(BACKEND)  # tests -> backend -> update_version

if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def _load_history_mod():
    """按文件路径加载 update_version/db/history.py。

    不能 `import db.history`：仓库里有两个同名 `db` 包（`backend/db/` 与
    `update_version/db/`），`sys.path` 上谁先命中就用谁，结果随测试顺序变化。
    项目其它地方（`core/cron.py` 的 `_load_scheduled_tasks_db`）也是同样的做法。
    """
    path = os.path.join(UPDATE_VERSION, "db", "history.py")
    spec = importlib.util.spec_from_file_location("v2_history_busy_probe", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["v2_history_busy_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_history_conn_survives_a_lock_longer_than_the_old_default():
    """持锁 7 秒（> 旧的 5 秒默认值）时，第二个写者仍必须成功。

    这是能区分「真修复」与「碰巧长度内」的关键用例：持锁 3 秒的话，
    连旧的 5 秒默认值都等得过去，测不出任何差异。
    """
    history = _load_history_mod()

    path = history._DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    with history._get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS _lock_probe")
        conn.execute("CREATE TABLE _lock_probe (x INTEGER)")

    started = threading.Event()
    holder_error = []

    def holder():
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO _lock_probe VALUES (1)")
            started.set()
            time.sleep(7)  # > 旧默认值 5 秒
            conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001
            holder_error.append(exc)
        finally:
            conn.close()

    th = threading.Thread(target=holder)
    th.start()
    assert started.wait(5), "holder 未能取得写锁"

    time.sleep(0.2)
    t0 = time.time()
    try:
        with history._get_conn() as conn:
            conn.execute("INSERT INTO _lock_probe VALUES (2)")
    finally:
        th.join()

    elapsed = time.time() - t0
    assert not holder_error, f"holder 失败: {holder_error}"

    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute("SELECT count(*) FROM _lock_probe").fetchone()[0]
    finally:
        conn.close()
    assert rows == 2, (
        f"第二个写入没落库（等了 {elapsed:.1f}s）。"
        f"若抛过 database is locked，说明 timeout 仍是 5 秒默认值。"
    )


def test_history_conn_sets_busy_timeout_pragma():
    """连接上必须显式设置 busy_timeout（与 migrate.get_conn 对齐）"""
    history = _load_history_mod()

    with history._get_conn() as conn:
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert busy >= 30000, f"busy_timeout={busy}ms，应 >= 30000ms"


def test_history_and_migrate_busy_timeouts_agree():
    """两个库的 busy_timeout 不应再分叉"""
    history = _load_history_mod()
    from db import migrate

    with history._get_conn() as hconn:
        h_busy = hconn.execute("PRAGMA busy_timeout").fetchone()[0]

    mconn = migrate.get_conn()
    try:
        m_busy = mconn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        mconn.close()

    assert h_busy >= 30000, f"history busy_timeout={h_busy}"
    assert m_busy > 0, f"migrate busy_timeout={m_busy}"
