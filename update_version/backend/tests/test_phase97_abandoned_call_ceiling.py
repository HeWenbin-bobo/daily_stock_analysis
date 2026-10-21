"""Phase 97: 被放弃的上游调用必须有上限，不能无限泄漏 fd

`call_with_timeout` 用 daemon 线程跑上游调用，超时就放弃 —— 但那个线程**仍在跑**，
期间持有 requests/urllib 的连接（每条 1~2 个 fd）。akshare 没有内部超时，
单次调用实测最坏 197 秒。

容器内实测（修复前，`GET /community/hot-list?limit=5`）：
  - 单次请求 +8~10 fd，耗时 ≈ 8.0s（等于 _HOT_LIST_QUOTE_BUDGET_SECONDS）
  - 连续打请求后 fd 堆到 1580，静置 45s 仍上涨（被放弃的线程还在跑）
  - `limit=1`（1 个 code，预算内完成）→ 0.086s，fd 零增长
  - 线程数始终为 1：泄漏的是**线程里持有的连接**，不是线程本身

放任下去会先耗尽进程 fd，然后所有端点（包括与行情毫无关系的）一起 EMFILE。

修复：记录"仍在运行的被放弃调用"，超过上限（默认 32）时直接抛 TimeoutError
让调用方走降级路径，而不是继续接纳新的挂起调用。
"""
import os
import sys
import threading
import time

import pytest

BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from core import data_source as ds  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_abandoned():
    """每个用例前后清空 abandoned 记账，避免相互污染"""
    with ds._abandoned_lock:
        ds._abandoned.clear()
    yield
    with ds._abandoned_lock:
        ds._abandoned.clear()


def test_abandoned_calls_are_recorded():
    """超时的调用必须被记账，否则无从判断"还有多少在跑\""""
    started = threading.Event()

    def slow():
        started.set()
        time.sleep(2)
        return "late"

    with pytest.raises(TimeoutError):
        ds.call_with_timeout(slow, 0.2, "probe")

    assert started.is_set()
    with ds._abandoned_lock:
        alive = [t for t in ds._abandoned if t.is_alive()]
    assert alive, "超时后应记录仍在运行的线程"


def test_finished_abandoned_calls_are_reaped():
    """已经跑完的线程应从记账里移除，列表不能只增不减"""

    def quick():
        time.sleep(0.4)
        return "done"

    with pytest.raises(TimeoutError):
        ds.call_with_timeout(quick, 0.05, "probe-quick")

    time.sleep(0.8)  # 让 worker 跑完

    # 再调一次，触发记账清理
    with pytest.raises(TimeoutError):
        ds.call_with_timeout(lambda: time.sleep(0.5), 0.05, "probe-reap")

    with ds._abandoned_lock:
        alive = [t for t in ds._abandoned if t.is_alive()]
    assert len(alive) <= 1, f"已结束的线程没有被回收: {len(ds._abandoned)} 条记账"


def test_ceiling_refuses_new_calls_instead_of_leaking_fds(monkeypatch):
    """已达上限时，新调用应立刻失败（走降级），而不是再抛弃一个线程"""
    monkeypatch.setattr(ds, "_MAX_ABANDONED_THREADS", 2)

    def slow():
        time.sleep(3)
        return "late"

    # 制造两个被放弃的调用
    for _ in range(2):
        with pytest.raises(TimeoutError):
            ds.call_with_timeout(slow, 0.05, "fill")
        time.sleep(0.02)

    with ds._abandoned_lock:
        assert len([t for t in ds._abandoned if t.is_alive()]) >= 2

    # 第三次：应在启动线程之前就被拒绝
    started = threading.Event()

    def tracked():
        started.set()
        return "should-not-run"

    with pytest.raises(TimeoutError) as ei:
        ds.call_with_timeout(tracked, 0.05, "rejected")

    assert "refusing to leak more fds" in str(ei.value) or "abandoned" in str(ei.value), (
        f"应当因上限被拒，实际错误: {ei.value}"
    )
    assert not started.is_set(), "被拒绝的调用不应启动线程"


def test_ceiling_does_not_break_normal_success(monkeypatch):
    """上限只影响"放弃"这条路，正常快调用不受影响"""
    monkeypatch.setattr(ds, "_MAX_ABANDONED_THREADS", 1)

    assert ds.call_with_timeout(lambda: 42, 5.0, "fast") == 42


def test_ceiling_is_configurable_by_env(monkeypatch):
    """上限可以通过环境变量覆盖（部署侧可调）"""
    import importlib

    monkeypatch.setenv("DATA_SOURCE_MAX_ABANDONED_THREADS", "7")
    reloaded = importlib.reload(ds)
    try:
        assert reloaded._MAX_ABANDONED_THREADS == 7
    finally:
        monkeypatch.delenv("DATA_SOURCE_MAX_ABANDONED_THREADS", raising=False)
        importlib.reload(ds)
