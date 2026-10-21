# -*- coding: utf-8 -*-
"""Regression test: every sys.path/sys.modules surgery site must share one lock.

``endpoints_hot`` has four places that temporarily rewrite process-global state
(``sys.path`` and ``sys.modules["api*"/"src*"]``) to import main-project modules:

  * ``_import_main_hot_analysis``
  * ``_load_main_hot_analysis``
  * the ETF rotation runner
  * ``_get_intent_resolver``

The first three took ``_MAIN_HOT_MOD_LOCK``; the fourth took a private
``_intent_lock``. Two unrelated locks guarding one piece of global state is the
same race the shared lock exists to prevent: one thread can be mid-import while
another deletes ``src.*`` from ``sys.modules``, yielding a half-initialised
module or ModuleNotFoundError — an intermittent 500 that is very hard to catch.

Run: ``python -m pytest update_version/backend/tests/test_phase88_module_surgery_single_lock.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest


def _hot():
    """Lazy import: endpoints_hot reorders sys.path at import time."""
    from api import endpoints_hot
    return endpoints_hot


def test_intent_lock_is_the_shared_lock() -> None:
    eh = _hot()
    assert eh._intent_lock is eh._MAIN_HOT_MOD_LOCK, (
        "intent resolver 用的是独立锁，与其它三处改写 sys.path 的路径互斥失效"
    )


def test_shared_lock_is_reentrant() -> None:
    """RLock: `_load_main_hot_analysis` 持锁后再调 `_import_main_hot_analysis` 不会自锁。"""
    import threading
    eh = _hot()
    assert isinstance(eh._MAIN_HOT_MOD_LOCK, type(threading.RLock()))


def test_nested_acquisition_does_not_deadlock() -> None:
    eh = _hot()
    lock = eh._MAIN_HOT_MOD_LOCK

    def _nested():
        with lock:
            with lock:      # 重入：同一线程必须能再次获取
                pass

    import threading
    t = threading.Thread(target=_nested, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "同一线程重入共享锁时死锁了"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
