"""Phase 98: core/audit.py 的连接必须关闭（全站写路径的公共开销）

容器内实测：`GET /api/v2/audit/recent` 每次 +1 fd，连续调用后进程里堆积了
几十个 v2_dev.db 句柄。每条 SQLite 连接占 2 个 fd（db + wal）。

三处漏洞：
  - `record()`      —— **每个写操作**都会调用（几乎所有 POST/DELETE 端点）
  - `list_recent()` —— /audit/recent 列表端点
  - `cleanup_old()` —— cron 每天跑一次的保留期清理

`record()` 最关键：它是全站写路径的公共开销，泄漏速度跟写请求量成正比。
"""
import ast
import os
import sys

import pytest

BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
AUDIT = os.path.join(BACKEND, "core", "audit.py")

if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def test_no_audit_function_leaks_a_connection():
    src = open(AUDIT, encoding="utf-8").read()
    tree = ast.parse(src)

    offenders = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if "= get_conn()" not in seg:
            continue
        if "conn.close()" in seg or "_conn() as conn" in seg:
            continue
        offenders.append(node.name)

    assert not offenders, f"这些函数拿了连接却从不关闭：{offenders}"


def test_record_closes_its_connection():
    """每一次审计写入都必须归还连接"""
    from core import audit

    opened = {"n": 0, "closed": 0}

    class FakeConn:
        def cursor(self):
            class _C:
                lastrowid = 1

                def execute(self, *a, **k):
                    return self

                def fetchall(self):
                    return []

            return _C()

        def close(self):
            opened["closed"] += 1

    real_get_conn = audit.get_conn
    audit.get_conn = lambda: (opened.__setitem__("n", opened["n"] + 1) or FakeConn())
    try:
        for i in range(5):
            audit.record(action=f"probe_{i}", user_id="u")
    finally:
        audit.get_conn = real_get_conn

    assert opened["n"] == 5
    assert opened["closed"] == 5, (
        f"record() 打开 {opened['n']} 次却只关闭 {opened['closed']} 次"
    )


def test_list_recent_closes_its_connection():
    from core import audit

    opened = {"n": 0, "closed": 0}

    class FakeConn:
        def cursor(self):
            class _C:
                def execute(self, *a, **k):
                    return self

                def fetchall(self):
                    return []

            return _C()

        def close(self):
            opened["closed"] += 1

    real = audit.get_conn
    audit.get_conn = lambda: (opened.__setitem__("n", opened["n"] + 1) or FakeConn())
    try:
        audit.list_recent(limit=5)
    finally:
        audit.get_conn = real

    assert opened["n"] == 1 and opened["closed"] == 1


def test_list_recent_closes_connection_on_query_error():
    """查询抛异常时也必须关闭"""
    from core import audit

    closed = {"n": 0}

    class FakeConn:
        def cursor(self):
            class _C:
                def execute(self, *a, **k):
                    raise RuntimeError("boom")

                def fetchall(self):
                    return []

            return _C()

        def close(self):
            closed["n"] += 1

    real = audit.get_conn
    audit.get_conn = lambda: FakeConn()
    try:
        with pytest.raises(RuntimeError):
            audit.list_recent(limit=5)
    finally:
        audit.get_conn = real

    assert closed["n"] == 1, "异常路径没有关闭连接"
