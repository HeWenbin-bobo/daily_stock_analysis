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

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path[:1]:
    sys.path.insert(0, BACKEND_DIR)

import pytest


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
