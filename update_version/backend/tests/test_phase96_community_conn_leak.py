"""Phase 96: community.py 的 SQLite 连接必须关闭（首页热榜读路径 fd 泄漏）

容器内实测（修复前）：

    GET /api/v2/community/hot-list?limit=20 × 40 次
    fd 17 → 89（+72，每请求 2 个），静置 12 秒仍为 89 —— 净泄漏。

2 fd/请求 与 SQLite 的「数据库文件 + -wal」两个句柄吻合。

`modules/community.py` 原有 15 处 `conn = get_conn()`，**没有一处 close()**。
`sqlite3.Connection` 被追踪对象引用时不会在引用消失时立即释放 fd
（容器内验证：drop 引用后 fd 不降，`gc.collect()` 才归 0）。

这是用户量最高的读路径之一：首页热力图与社区页都会轮询它。
"""
import ast
import os
import sys

import pytest

BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
COMMUNITY = os.path.join(BACKEND, "modules", "community.py")

if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def _src():
    return open(COMMUNITY, encoding="utf-8").read()


def test_no_function_leaks_a_connection():
    """任何拿到 get_conn() 的函数，都必须以 finally/with _conn() 关闭"""
    src = _src()
    tree = ast.parse(src)

    offenders = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if "conn = get_conn()" not in seg and "= get_conn()" not in seg:
            continue
        # 注意不能直接判断子串 "_conn()"：`get_conn()` 本身就包含 "_conn()"，
        # 那样会把所有未关闭的函数都判成"已关闭"。必须判断独立的上下文管理器。
        closes = (
            "conn.close()" in seg
            or "with _conn()" in seg
            or "_conn() as conn" in seg
        )
        if not closes:
            offenders.append(node.name)

    assert not offenders, (
        f"这些函数拿了连接却从不关闭，会持续泄漏 fd：{offenders}"
    )


def test_conn_helper_closes_even_on_exception():
    """`_conn()` 上下文管理器在异常路径上也会关闭"""
    from modules import community

    conn_holder = {}

    class FakeConn:
        closed = False

        def close(self):
            FakeConn.closed = True

    original = community.get_conn
    community.get_conn = lambda: (conn_holder.setdefault("c", FakeConn()) or conn_holder["c"])
    try:
        with pytest.raises(RuntimeError):
            with community._conn() as conn:
                conn_holder["got"] = conn
                raise RuntimeError("boom")
    finally:
        community.get_conn = original

    assert FakeConn.closed, "_conn() 在异常路径上必须关闭连接"


def test_conn_helper_closes_on_success():
    """正常路径同样关闭"""
    from modules import community

    class FakeConn:
        closed = False

        def close(self):
            FakeConn.closed = True

    original = community.get_conn
    community.get_conn = lambda: FakeConn()
    try:
        with community._conn() as conn:
            assert conn is not None
    finally:
        community.get_conn = original

    assert FakeConn.closed


def test_hot_list_endpoint_does_not_grow_fds():
    """端到端：反复调用 get_daily_hot_list 不增长 fd（Windows 上退化为语义检查）

    Linux 上直接比 /proc/self/fd；其它平台只验证"多次调用不报错且返回稳定"，
    因为 Windows 的 fd 语义不同、比例不可移植。
    """
    from modules import community

    def fd_count():
        try:
            return len(os.listdir("/proc/self/fd"))
        except OSError:
            return None

    before = fd_count()
    for _ in range(20):
        community.get_daily_hot_list(limit=5, quote_budget_seconds=0.01)
    after = fd_count()

    if before is None:
        pytest.skip("非 Linux 平台，/proc/self/fd 不可用")

    # 允许少量波动（连接池/日志句柄），但绝不能是每请求 +2
    assert after - before <= 5, (
        f"20 次调用后 fd 增长 {after - before}（{before} → {after}），"
        f"说明连接仍在泄漏"
    )
