# -*- coding: utf-8 -*-
"""v4 第二轮迭代2 CRITICAL 回归修复验证：
异动规则 params 数值字段入库校验（validate_rule_params）

背景：v2 在 Round 53（8/27）修过该 CRITICAL —— params={"multiplier":"abc"} 能入库，
扫描时 float("abc") 抛 ValueError 被吞 → 规则永不触发且用户无感知。
但 v4 复制自 v3，v3 复制时丢失了该校验（v2/v3/v4 各自独立演进，修复未逐版继承）。
实测 v4 曾接受 multiplier="abc" / threshold_pct="xyz" 创建 200 入库（rule 1678/1679）。

修复：把 v2 的 _PARAM_NUMERIC_FIELDS + validate_rule_params 移植到 v4 alerts_engine，
create_rule/update_rule 入库前调用校验；端点层 ValueError→400 转换已存在。

测试全部 mock DB，永不联网。
"""
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 与 phase 系列测试一致：backend 目录在 sys.path[0]，import 解析到 v4 backend 包
from modules import alerts_engine as ae  # noqa: E402

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


def test_validate_rule_params():
    print("\n== [v4回归] validate_rule_params ==")
    # 合法值不抛
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

    # 非法值抛 ValueError
    for rt, params, label in [
        ("volume_surge", {"multiplier": "abc"}, "multiplier='abc' 应拒绝"),
        ("ma_break", {"period": -5}, "period=-5 应拒绝"),
        ("opening_spike", {"threshold_pct": []}, "threshold_pct=[] 应拒绝"),
        ("volume_surge", {"multiplier": None}, "multiplier=None 应通过(用默认值)"),
    ]:
        try:
            ae.validate_rule_params(rt, params)
            if "应通过" in label:
                check(label, True)
            else:
                check(label, False)
        except ValueError:
            if "应通过" in label:
                check(label, False)
            else:
                check(label, True)


def test_create_rule_rejects_invalid_params():
    print("\n== [v4回归] create_rule 拒绝非法 params ==")
    with patch("modules.alerts_engine.get_conn") as mock_get:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.lastrowid = 1
        mock_conn.cursor.return_value = mock_cur
        mock_get.return_value = mock_conn
        # 非法 → ValueError（端点上抛会转 400）
        try:
            ae.create_rule("u1", "bad", "volume_surge", {"multiplier": "abc"})
            check("非法 params 应抛 ValueError", False)
        except ValueError:
            check("非法 params 应抛 ValueError", True)
        # 合法不抛
        try:
            ae.create_rule("u1", "good", "volume_surge", {"multiplier": 2.0})
            check("合法 params 不抛异常", True)
        except ValueError:
            check("合法 params 不抛异常", False)


def test_update_rule_rejects_invalid_params():
    print("\n== [v4回归] update_rule 拒绝非法 params ==")
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


if __name__ == "__main__":
    test_validate_rule_params()
    test_create_rule_rejects_invalid_params()
    test_update_rule_rejects_invalid_params()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
