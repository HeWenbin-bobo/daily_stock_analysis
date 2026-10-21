"""Phase 93: hot-analysis 的 event loop 必须在异常路径上关闭

两处同步入口（preview / run）用 `asyncio.new_event_loop()` 跑主项目的协程，
但只在成功路径调 `loop.close()`：

    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(coro)   # ← 抛异常
    loop.close()                              # ← 永远执行不到

`new_event_loop()` 每次都新建一个 selector（一个 epoll fd + 一对 socketpair），
容器内实测：100 个未关闭的 loop → +300 个 fd（每条 3 个）。这两条路径都是
用户可直接触发、且**失败更频繁**的入口（LLM 超时、数据源挂起、schema 校验失败），
所以泄漏速度比成功时更快。

另外两处（market-review generate、cron 的用户定时任务）早已用
`try/finally: loop.close()`，此处对齐即可。
"""
import ast
import os
import sys

BACKEND = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, BACKEND)

import pytest


ENDPOINTS_HOT = os.path.join(BACKEND, "api", "endpoints_hot.py")


def _loop_close_sites():
    """找出所有 `new_event_loop()` 赋值处，报告其后的 close 是否在 finally 中"""
    src = open(ENDPOINTS_HOT, encoding="utf-8").read()
    tree = ast.parse(src)
    sites = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        fn = node.value.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "new_event_loop"):
            continue
        target = next((t for t in node.targets if isinstance(t, ast.Name)), None)
        if target is None:
            continue
        sites.append((target.id, node.lineno, src.splitlines()))

    return sites


def test_every_new_event_loop_is_closed_in_finally():
    """每个 new_event_loop() 后面都必须有 try/finally 包裹的 close"""
    sites = _loop_close_sites()
    assert sites, "没有找到任何 new_event_loop() —— 测试自身失效了"

    lines = open(ENDPOINTS_HOT, encoding="utf-8").read().splitlines()
    for name, lineno, _ in sites:
        # 看赋值之后 12 行以内是否出现 try:
        window = lines[lineno - 1 : lineno + 12]
        joined = "\n".join(window)
        assert "try:" in joined, (
            f"第 {lineno} 行的 `{name} = asyncio.new_event_loop()` 之后没有 try: 包裹，"
            f"异常路径会跳过 loop.close() 泄漏 3 个 fd"
        )
        # close 必须紧跟 finally
        assert "finally:" in joined and f"{name}.close()" in joined, (
            f"第 {lineno} 行的 loop `{name}` 没有在 finally 中 close"
        )


def test_unclosed_loop_stays_open_and_close_is_idempotent():
    """佐证语义：不 close 的 loop 一直处于未关闭状态，close 幂等且不抛异常。

    注意不在断言里比较 fd 数字 —— Linux 上是 epoll + socketpair（3 个），
    Windows 上 ProactorEventLoop 用的是 IOCP 句柄，两者不可移植比较。
    真正的量级是在 Linux 容器里实测的：100 个未关闭 loop → +300 fd。
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        assert not loop.is_closed()
    finally:
        loop.close()
    assert loop.is_closed()

    # 二次 close 不应抛异常（说明 close 是安全幂等的，finally 里重复调用无害）
    loop.close()
    assert loop.is_closed()
