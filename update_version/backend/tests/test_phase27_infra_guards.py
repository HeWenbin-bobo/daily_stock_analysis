# -*- coding: utf-8 -*-
"""Regression tests for the 2026-10-19 exploration round (infra + security).

Each test pins a defect that was reproduced against the live container:

* `ratelimit` crashed with ``TypeError`` once the bucket cap was reached
  (``dict.popitem(last=False)`` on a plain ``dict``) — that exception is raised
  inside the lock held by ``check_limit``, so **every** rate-limited endpoint
  starts returning 500 and never recovers.
* SMTP used ``context=None``, which on CPython 3.12 resolves to
  ``ssl._create_unverified_context`` → certificate validation disabled, so a
  MITM could impersonate any SMTP server and harvest the mailbox password.
* ``_run_user_scheduled_task`` closed its temporary event loop right after the
  wrapper coroutine returned; the real analysis ran as a ``create_task``
  background task and was destroyed before executing.

Run: ``python -m pytest update_version/backend/tests/test_phase27_infra_guards.py``
"""

from __future__ import annotations

import asyncio
import os
import ssl
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("V4_DB_PATH", str(BACKEND / "data" / "v2_test_phase27.db"))

import pytest


# ---------------------------------------------------------------------------
# rate limiter bucket eviction
# ---------------------------------------------------------------------------
def test_rate_limit_evicts_from_front_without_type_error() -> None:
    """撞上 BUCKETS_MAX 时必须正常淘汰，而不是抛 TypeError 打挂所有限流端点。"""
    import core.ratelimit as rl
    from collections import defaultdict, deque

    original = rl._BUCKETS_MAX
    original_buckets = rl._buckets
    try:
        rl._BUCKETS_MAX = 5
        # 必须是带 factory 的 defaultdict：check_limit 依赖 `_buckets[key]` 自动建桶
        rl._buckets = defaultdict(deque)
        for i in range(30):
            allowed, _ = rl.check_limit(f"10.9.9.{i}", "/api/v2/data/quote")
            assert allowed, f"unexpected block at i={i}"
        assert len(rl._buckets) <= 5, f"buckets not capped: {len(rl._buckets)}"
    finally:
        rl._BUCKETS_MAX = original
        rl._buckets = original_buckets


def test_rate_limit_still_blocks_after_eviction() -> None:
    """淘汰逻辑不能把限流本身弄坏。"""
    import core.ratelimit as rl

    endpoint = "/api/v2/notifications/test"  # 5 / 60s
    ip = "203.0.113.77"
    rl.reset_limit(ip, endpoint)
    allowed = [rl.check_limit(ip, endpoint)[0] for _ in range(7)]
    assert allowed[:5] == [True] * 5
    assert allowed[5:] == [False, False]
    rl.reset_limit(ip, endpoint)


# ---------------------------------------------------------------------------
# SMTP TLS verification
# ---------------------------------------------------------------------------
def test_smtp_uses_a_verifying_context() -> None:
    """notifier 必须显式传 create_default_context，不能依赖 context=None。"""
    source = (BACKEND / "core" / "notifier.py").read_text(encoding="utf-8")

    assert "ssl.create_default_context()" in source
    assert "starttls(context=ssl.create_default_context())" in source
    # 不允许出现裸的 SMTP_SSL(host, port, timeout=15) / starttls()
    assert "SMTP_SSL(host, port, timeout=15)\n" not in source
    assert "server.starttls()\n" not in source


def test_default_stdlib_context_is_unverified_on_this_runtime() -> None:
    """记录这个平台事实：context=None 在 3.12 上确实不校验证书。

    这不是产品断言，而是解释上一条测试为何必须存在；若未来 CPython 改了
    默认值，这条会失败并提示可以简化，但显式传 context 永远无害。
    """
    if not hasattr(ssl, "_create_unverified_context"):
        pytest.skip("private ssl helper unavailable")
    assert ssl._create_stdlib_context is ssl._create_unverified_context


# ---------------------------------------------------------------------------
# scheduled-task event loop must not destroy the background analysis
# ---------------------------------------------------------------------------
def test_scheduled_task_runner_drains_pending_tasks() -> None:
    """源码契约：run_until_complete 之后必须 gather 剩余 task 才 close loop。"""
    source = (BACKEND / "core" / "cron.py").read_text(encoding="utf-8")

    assert "asyncio.all_tasks(loop)" in source
    assert "asyncio.gather(*pending, return_exceptions=True)" in source


def test_background_task_survives_wrapper_return_pattern() -> None:
    """行为级复现：外壳协程 return 后，后台 task 必须仍能被同一 loop 跑完。

    这正是 `run_async_analysis` + `create_task` 的形状。注意必须用
    **独立的 loop**（`run_until_complete` 返回后再 `all_tasks(loop)`），
    与 cron.py 的用法一致；在运行中的协程里调 `asyncio.all_tasks()` 会把
    当前 task 也算进去，gather 自己会死锁。
    """
    ran: list[str] = []

    async def background() -> None:
        await asyncio.sleep(0)
        ran.append("done")

    async def wrapper() -> None:
        asyncio.create_task(background())  # 与 run_async_analysis 一致：不 await
        return

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(wrapper())
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        assert pending, "wrapper 返回时应留有 pending 的 background task"
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    finally:
        loop.close()

    assert ran == ["done"], "后台 task 必须真正执行完"
