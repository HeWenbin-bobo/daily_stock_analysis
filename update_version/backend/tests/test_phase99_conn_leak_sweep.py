"""Phase 99: 全后端连接泄漏的结构性防线

`sqlite3.Connection` 不会因为引用消失就释放 fd —— 容器内实测：drop 引用后
fd 不降，只有 `gc.collect()` 才归 0。每条连接占 **2 个 fd**（数据库 + -wal）。

实测累计到的泄漏（都在容器里量化过）：

| 模块 | 触发频率 | 实测 |
|---|---|---|
| `core/cron.py` 的 job listener | 每次 cron 执行 | 每天 ~76 次 |
| `modules/community.py` 热榜 | 首页/社区页轮询 | 40 次请求 fd 17→89 |
| `core/audit.py` record | **每个写请求** | /audit/recent 每次 +1 |
| `core/data_source.update_health` | 每次行情/K线调用 | 堆积主要来源 |
| `modules/*` 各业务模块 | 各端点 | 逐个体现在 fd 上 |

这个测试用 AST 扫全后端：任何函数只要拿了 `get_conn()` 就必须在同一个函数里
出现 `conn.close()` 或 `with _conn() as conn`。它不依赖运行环境，
所以能在 CI 里拦住"新写的端点又忘了关连接"。
"""
import ast
import os

import pytest

BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

# 已知且已确认安全的例外：函数把连接**返回**给调用方，由调用方收口关闭。
# 例：community._open_conn 返回连接，_load_resource 用 try/finally close。
_RETURNS_CONNECTION = {
    ("modules/community.py", "_open_conn"),
}


def _iter_py_files():
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__")]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def test_every_get_conn_call_is_closed():
    offenders = []

    for path in _iter_py_files():
        rel = os.path.relpath(path, BACKEND).replace(os.sep, "/")
        try:
            src = open(path, encoding="utf-8").read()
            tree = ast.parse(src)
        except (UnicodeDecodeError, SyntaxError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            seg = ast.get_source_segment(src, node) or ""
            if "= get_conn()" not in seg:
                continue
            if (rel, node.name) in _RETURNS_CONNECTION:
                continue
            closed = (
                "conn.close()" in seg
                or "_conn() as conn" in seg
                or "with _conn()" in seg
            )
            if not closed:
                offenders.append(f"{rel}::{node.name}")

    assert not offenders, (
        "以下函数拿了 SQLite 连接却从不关闭（每处会长期占用 2 个 fd）：\n  "
        + "\n  ".join(offenders)
    )


def test_no_cursor_used_after_its_connection_closes():
    """把 try/finally 改写成 `with _conn()` 时最容易踩的坑：

    `with` 块退出即关闭连接，但紧接着的 `except` 分支若还拿着块内那个 `cur`
    继续执行，就会抛 `ProgrammingError: Cannot operate on a closed database`。

    这个错误真实发生过：`notifier.add_channel` 的 UNIQUE 冲突分支在
    `with` 块外复用 `cur`，被 test_phase2_smoke 抓住。它不会泄漏 fd，
    但会让「重名渠道 = 更新」这条正常业务路径直接 500。
    """
    offenders = []

    for path in _iter_py_files():
        rel = os.path.relpath(path, BACKEND).replace(os.sep, "/")
        try:
            src = open(path, encoding="utf-8").read()
            tree = ast.parse(src)
        except (UnicodeDecodeError, SyntaxError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            body_has_with = any(
                isinstance(stmt, ast.With) and "_conn" in ast.dump(stmt)
                for stmt in node.body
            )
            if not body_has_with:
                continue
            for handler in node.handlers:
                # handler 内部（含嵌套 if/for）只要开了自己的 _conn()，
                # 里面用的 cur 就属于新连接，安全。
                # 注意不能用 `any(... for stmt in handler.body)` 只看顶层 ——
                # 常见写法是 `if "UNIQUE" in str(e): with _conn() as conn: ...`，
                # 此时 With 是 If 的子节点，浅层判断会漏掉。
                # 注意 ast.dump 里函数调用渲染成 `Name(id='_conn')`，
                # 没有 `_conn()` 这个子串 —— 判据必须查 `_conn`。
                handler_opens_own_conn = "_conn" in (ast.dump(handler) or "")
                if not handler_opens_own_conn:
                    seg = ast.get_source_segment(src, handler) or ""
                    if "cur.execute" in seg or "conn.execute" in seg:
                        offenders.append(f"{rel}:{handler.lineno}")

    assert not offenders, (
        "这些 except 分支在 `with _conn()` 之后仍使用块内的游标/连接，"
        "运行时会抛 'Cannot operate on a closed database'：\n  "
        + "\n  ".join(offenders)
    )


def test_registry_helper_shape():
    """`_conn()` 辅助函数在关键模块里必须是 contextmanager 且 finally 关闭"""
    must_have = [
        "core/audit.py",
        "core/notifier.py",
        "modules/community.py",
        "modules/paper_trading.py",
        "modules/alerts_engine.py",
        "modules/backtest_engine.py",
        "modules/hyperopt_engine.py",
        "modules/portfolio.py",
    ]
    missing = []
    for rel in must_have:
        p = os.path.join(BACKEND, rel.replace("/", os.sep))
        if not os.path.exists(p):
            missing.append(f"{rel}: 文件不存在")
            continue
        src = open(p, encoding="utf-8").read()
        if "@contextmanager" not in src or "def _conn()" not in src:
            missing.append(f"{rel}: 缺少 @contextmanager def _conn()")
            continue
        if "conn.close()" not in src:
            missing.append(f"{rel}: _conn 里没有 conn.close()")

    assert not missing, "连接辅助函数缺失或不完整：\n  " + "\n  ".join(missing)


def test_readme_documents_the_fd_pitfall():
    """把这类坑写进文档，避免下次又踩（README 已列"已知问题"章节）"""
    readme = os.path.join(os.path.dirname(BACKEND), "README.md")
    if not os.path.exists(readme):
        pytest.skip("README 不存在")
    text = open(readme, encoding="utf-8").read()
    # 不强制措辞，但至少出现一次 fd / 文件描述符 相关说明
    assert ("fd" in text.lower()) or ("文件描述符" in text), (
        "README 未提及 fd 泄漏这一坑，建议补进「已知问题 / 运维注意」章节"
    )
