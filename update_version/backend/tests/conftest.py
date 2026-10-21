# -*- coding: utf-8 -*-
"""backend/tests/conftest.py — 测试隔离（迭代3引入，迭代4定稿 v4）

核心约束：`app` 模块在收集期（test_phase12_e2e 模块级 `from app import app`）
加载时绑定了 backend api.endpoints_hot 的模块对象 E1；此后任何对 api.* 的
sys.modules 清理都会让后续 `from api import endpoints_hot` 重建出 E2 ——
app 引用 E1、测试引用 E2，两份 _MAIN_HOT_MOD / _tasks 状态分裂
（症状：SSE 测试塞任务到 M'，端点从 M 读 → 404）。

因此 v4 **绝不动 sys.modules**（模块对象在整个 pytest 会话中单例演化，
import 一律命中缓存，app 与测试共享同一 E1/M），只做两件事：

1. 模块级 + 每测试 setup：把 backend 目录强制提到 sys.path[0]。
   收集期保证 phase12 的 `from api.endpoints_v2 ...` 解析到 v4 包；
   主项目模块加载链（loader）会把主项目根提到 [0]，fixture 在下一测试
   前纠正回来。loader 自身已负责 api.* 快照/恢复（迭代4）。
2. teardown 恢复 core.cron.APS_AVAILABLE（防御未来的模块级置 False；
   phase12 源头已在迭代4修复为导入后还原）。

不清 core.*：llm_router 拖 litellm，逐测试重导会使套件耗时爆炸。
"""
import os
import sys
import tempfile

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path[:1]:
    sys.path.insert(0, BACKEND_DIR)

# ★ 探索期修复(数据损坏事故)：把测试数据库重定向到仓库之外的临时目录。
#
#   backend/data/ 是 dsa-v2 容器的 **bind mount**。此前 25 个测试文件既没有
#   设置 V4_DB_PATH、也没有隔离 history.db，在宿主机跑 pytest 时它们会直接
#   写这个目录；与运行中的容器并发写同一 SQLite 文件，实测把 v2_dev.db 写成
#   `database disk image is malformed`（损坏后只能逐表 dump 抢救）。
#
#   conftest 在测试模块被收集**之前**执行，所以这里设置的环境变量能赶在
#   `db.migrate` / `db.history` 解析路径之前生效。测试文件里若要用自己的库，
#   请用 `os.environ.setdefault(...)`（本文件已先设好，setdefault 不会覆盖）。
_TEST_DB_DIR = tempfile.mkdtemp(prefix="dsa_v2_tests_")
os.environ.setdefault("V4_DB_PATH", os.path.join(_TEST_DB_DIR, "v2_dev.db"))
os.environ.setdefault("V4_HISTORY_PATH", os.path.join(_TEST_DB_DIR, "history.db"))

import pytest


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_schema():
    """在临时测试库上建好表结构。

    ★ 探索期修复：把 DB 重定向到临时目录后，单独跑某个测试文件（如
      `pytest tests/test_phase2_smoke.py`）会因为临时库里没有任何表而报
      `no such table: notification_channel`。全量跑时恰好有别的用例先调了
      `migrate()`，所以问题被掩盖，属于典型的顺序依赖。
      这里在会话开始时就按当前 `db.migrate.DB_PATH` 建表（幂等 CREATE IF NOT EXISTS）。
    """
    try:
        from db.migrate import migrate
        migrate()
    except Exception as exc:  # noqa: BLE001 - 建表失败就让具体用例暴露真实错误
        import warnings
        warnings.warn(f"conftest: test schema migration failed: {exc}")
    yield


@pytest.fixture(autouse=True)
def _reset_ratelimit_buckets():
    """每个测试开始前清空限流桶。

    `core.ratelimit._buckets` 是**进程级全局**的滑动窗口，测试之间不会自然过期。
    多个模块会打同一批受限端点（`/notifications/test` 5/min、
    `/market-review/generate` 1/min、`/backtest/hyperopt` 2/min …），一旦前一个
    用例把配额吃满，后一个用例的断言就从"校验分支返回 400/404"变成"收到 429"。

    实测症状：`test_phase77` 单独跑 5 passed，与 `test_phase76` 一起跑就 2 failed
    （429 ≠ 404/400）。这类失败与业务代码无关，纯属测试顺序耦合，且极易被误判成
    限流器 bug。生产环境不限流是对的，所以只在这里重置，不改 LIMITS。
    """
    ratelimit = sys.modules.get("core.ratelimit") or _import_soft("core.ratelimit")
    if ratelimit is not None:
        lock = getattr(ratelimit, "_buckets_lock", None)
        buckets = getattr(ratelimit, "_buckets", None)
        if buckets is not None:
            if lock is not None:
                with lock:
                    buckets.clear()
            else:
                buckets.clear()
    yield


def _import_soft(name):
    try:
        import importlib

        return importlib.import_module(name)
    except Exception:
        return None


@pytest.fixture(autouse=True)
def _isolate_backend_globals():
    cron_mod = sys.modules.get("core.cron")
    snap_aps = getattr(cron_mod, "APS_AVAILABLE", None) if cron_mod is not None else None

    # setup：backend 优先（防御主项目根/loader 把它挤下 [0]）
    while BACKEND_DIR in sys.path:
        sys.path.remove(BACKEND_DIR)
    sys.path.insert(0, BACKEND_DIR)

    yield

    if snap_aps is not None:
        try:
            from core import cron as _cron
            _cron.APS_AVAILABLE = snap_aps
        except Exception:
            pass
