"""pytest 配置 - 让 tests/ 能 import backend 模块。"""
import os
import sys
from pathlib import Path

# 把 backend 根目录加入 sys.path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 强制 FORCE_MOCK=1 (iter 63 test 修复):
# 个别测试可显式 monkeypatch.delenv + monkeypatch.setenv 覆盖。
os.environ.setdefault("FORCE_MOCK", "1")


# iter 165: 清理 data_provider 的 singleflight 模块级状态 (_FLIGHT / _FLIGHT_RESULT)。
# 这些是跨测试共享的全局 dict, 前一个测试残留会让后一个测试读到过期结果 (单独跑通过
# 但完整套件失败)。每个测试后清空, 保证隔离。
import pytest


@pytest.fixture(autouse=True)
def _clear_singleflight_state():
    yield
    import services.data_provider as dp
    with dp._FLIGHT_LOCK:
        dp._FLIGHT.clear()
        dp._FLIGHT_RESULT.clear()
