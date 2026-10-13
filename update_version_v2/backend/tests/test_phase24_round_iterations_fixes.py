# -*- coding: utf-8 -*-
"""第四轮 /loop 迭代5（Round 56）修复回归测试：
固化迭代1/迭代2 的关键修复，防止未来回归：

[迭代1 - Round52]
1. analyze-single 超时不再被 ThreadPoolExecutor shutdown(wait=True) 阻塞
   （旧实现 with executor 退出时等后台分析线程，超时形同虚设 → 改显式
   executor + shutdown(wait=False, cancel_futures=True)）
2. /hot-analysis/market-data 主源失败时回退 v2 data_source.fetch_quotes_bulk
   （旧实现复用主项目 _fetch_market_data 仅 akshare，失败返回空 → 自选股行情全 "-"）

[迭代2 - Round53]
3. 异动规则 create_rule/update_rule 校验 params 数值字段（multiplier="abc" 拒绝）
4. create_alert_rule 端点 ValueError → 400（对齐 update_alert_rule 既有模式）

测试全部 mock 数据源/DB/服务，永不联网、永不真跑全市场抓取。
"""
import os
import sys
import json
import importlib.util
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BACKEND = Path(__file__).resolve().parent.parent  # update_version_v2/backend/
_MAIN_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # daily_stock_analysis_v2/
if str(_MAIN_ROOT) not in sys.path:
    sys.path.append(str(_MAIN_ROOT))

PASS = 0
FAIL = 0


def check(name: str, cond: bool):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}")


# ============================================================================
# 1. alerts_engine.params 校验（迭代2 修复）
# ============================================================================
from modules import alerts_engine as ae  # noqa: E402


def test_validate_rule_params():
    print("\n== [迭代2] validate_rule_params ==")
    # 合法值
    try:
        ae.validate_rule_params("volume_surge", {"multiplier": 2.5})
        ae.validate_rule_params("ma_break", {"period": 20})
        ae.validate_rule_params("opening_spike", {"threshold_pct": "3.5"})  # 数字字符串
        ae.validate_rule_params("volume_surge", {})  # 空 params
        ae.validate_rule_params("volume_surge", None)  # None
        ae.validate_rule_params("limit_up", {"whatever": "x"})  # 无关字段不校验
        ok = True
    except ValueError as e:
        ok = False
        print(f"  unexpected ValueError: {e}")
    check("合法参数不抛异常", ok)

    # 非法值
    try:
        ae.validate_rule_params("volume_surge", {"multiplier": "abc"})
        check("multiplier='abc' 应拒绝", False)
    except ValueError:
        check("multiplier='abc' 应拒绝", True)

    try:
        ae.validate_rule_params("ma_break", {"period": -5})
        check("period=-5 应拒绝", False)
    except ValueError:
        check("period=-5 应拒绝", True)

    try:
        ae.validate_rule_params("opening_spike", {"threshold_pct": []})
        check("threshold_pct=[] 应拒绝", False)
    except ValueError:
        check("threshold_pct=[] 应拒绝", True)


def test_create_rule_rejects_invalid_params():
    print("\n== [迭代2] create_rule 拒绝非法 params ==")
    with patch("modules.alerts_engine.get_conn") as mock_get:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.lastrowid = 1
        mock_conn.cursor.return_value = mock_cur
        mock_get.return_value = mock_conn
        try:
            ae.create_rule("u1", "bad", "volume_surge", {"multiplier": "abc"})
            check("非法 params 应抛 ValueError", False)
        except ValueError:
            check("非法 params 应抛 ValueError", True)
        # 正常规则不抛
        try:
            ae.create_rule("u1", "good", "volume_surge", {"multiplier": 2.0})
            check("合法 params 不抛异常", True)
        except ValueError:
            check("合法 params 不抛异常", False)


def test_update_rule_rejects_invalid_params():
    print("\n== [迭代2] update_rule 拒绝非法 params ==")
    with patch("modules.alerts_engine.get_rule") as mock_get, \
         patch("modules.alerts_engine.get_conn") as mock_getc:
        mock_get.return_value = {"id": 1, "user_id": "u1", "rule_type": "volume_surge"}
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_conn.cursor.return_value = mock_cur
        mock_getc.return_value = mock_conn
        # 同批更新 rule_type + params（用新 rule_type 校验）
        try:
            ae.update_rule(1, rule_type="volume_surge", params={"multiplier": "abc"})
            check("update 非法 params 应抛 ValueError", False)
        except ValueError:
            check("update 非法 params 应抛 ValueError", True)
        # 不传 rule_type → 读库旧值校验
        try:
            ae.update_rule(1, params={"multiplier": 1.8})
            check("update 合法 params 不抛异常", True)
        except ValueError:
            check("update 合法 params 不抛异常", False)


# ============================================================================
# 2. analyze-single 超时实现（迭代1 修复）
# ============================================================================
def test_analyze_single_timeout_code():
    """验证 endpoints_hot.analyze_single / preview / run 均使用显式 executor +
    shutdown(wait=False)，而非 with ThreadPoolExecutor（后者超时后仍阻塞等待后台线程）。"""
    print("\n== [迭代1] 超时实现（analyze_single/preview/run）==")
    import api.endpoints_hot as eh
    src = Path(eh.__file__).read_text(encoding="utf-8")
    check("使用 ThreadPoolExecutor", "ThreadPoolExecutor" in src)
    check("显式 executor 变量（非 with）", "_executor = concurrent.futures.ThreadPoolExecutor" in src)
    check("shutdown(wait=False)", "shutdown(wait=False" in src)
    check("cancel_futures=True", "cancel_futures=True" in src)
    check("analyze_single 超时抛 504", "HTTPException(" in src and "504" in src)
    # 全文件不再有任何 `with ThreadPoolExecutor()`（旧实现阻塞退出）
    check("不再使用 with ThreadPoolExecutor",
          "with concurrent.futures.ThreadPoolExecutor()" not in src)


# ============================================================================
# 3. market-data fallback（迭代1 修复）
# ============================================================================
def test_market_data_fallback_code():
    """验证 /hot-analysis/market-data 主源失败时回退 v2 fetch_quotes_bulk。"""
    print("\n== [迭代1] market-data fallback ==")
    import api.endpoints_hot as eh
    src = Path(eh.__file__).read_text(encoding="utf-8")
    check("主源失败回退 fetch_quotes_bulk", "fetch_quotes_bulk" in src)
    check("代码长度补齐判断", "len(items) < len(code_list)" in src)
    check("保留主源结果（existing_codes）", "existing_codes" in src)


def test_market_data_fallback_behavior():
    """mock 主源返回空 + v2 fetch_quotes_bulk 返回行情 → 结果应补齐。"""
    print("\n== [迭代1] market-data fallback 行为 ==")
    import asyncio
    import api.endpoints_hot as eh

    fake_quote = {
        "code": "600519", "name": "贵州茅台", "price": 1302.8,
        "change_pct": -0.89, "volume": 100, "amount": 200,
        "high": 1310, "low": 1290, "open": 1304,
    }
    with patch.object(eh, "_load_main_hot_analysis") as mock_load, \
         patch.object(eh, "asyncio") as mock_asyncio:
        mock_hot = MagicMock()
        mock_hot._fetch_market_data.return_value = []  # 主源失败返回空
        mock_load.return_value = mock_hot
        # mock asyncio.to_thread 直接返回主源结果（同步执行 lambda）
        def _to_thread(fn, *a, **kw):
            return fn(*a, **kw)
        mock_asyncio.to_thread.side_effect = _to_thread

        # mock v2 data_source.fetch_quotes_bulk
        with patch("core.data_source.fetch_quotes_bulk", return_value={"600519": fake_quote}) as mock_bulk:
            result = asyncio.run(eh.hot_analysis_market_data("600519"))
            items = result["items"]
            check("回退后 items 非空", len(items) == 1)
            check("回退 code 正确", items[0]["code"] == "600519")
            check("回退 price 正确", items[0]["price"] == 1302.8)
            check("fetch_quotes_bulk 被调用", mock_bulk.called)


# ============================================================================
# 4. create_alert_rule 端点 400 转换（迭代2 修复）
# ============================================================================
def test_create_alert_rule_endpoint_400():
    """create_alert_rule 端点把 ValueError 转 400（而非冒泡 500）。"""
    print("\n== [迭代2] create_alert_rule 端点 400 ==")
    import api.endpoints_v2 as ev2
    from fastapi import HTTPException
    src = Path(ev2.__file__).read_text(encoding="utf-8")
    check("create_alert_rule 有 try/except", "except ValueError as e:" in src.split("@router.post(\"/alerts/rules\")")[1].split("def create_alert_rule")[1])
    check("转 HTTPException(400", 'HTTPException(400' in src)

    # 行为验证：mock create_rule 抛 ValueError → 端点应抛 400
    with patch("modules.alerts_engine.create_rule", side_effect=ValueError("参数 multiplier 必须是数字")):
        try:
            ev2.create_alert_rule({"user_id": "u1", "name": "n", "rule_type": "volume_surge",
                                   "params": {"multiplier": "abc"}})
            check("非法 params 端点返回 400", False)
        except HTTPException as e:
            check("非法 params 端点返回 400", e.status_code == 400)
            check("400 消息含参数错误", "参数错误" in str(e.detail))


if __name__ == "__main__":
    test_validate_rule_params()
    test_create_rule_rejects_invalid_params()
    test_update_rule_rejects_invalid_params()
    test_analyze_single_timeout_code()
    test_market_data_fallback_code()
    test_market_data_fallback_behavior()
    test_create_alert_rule_endpoint_400()
    print(f"\n=== Round 56 regression: {PASS} PASSED, {FAIL} FAILED ===")
    sys.exit(1 if FAIL else 0)
