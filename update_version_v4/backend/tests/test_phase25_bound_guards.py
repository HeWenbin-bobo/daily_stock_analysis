# -*- coding: utf-8 -*-
"""探索期第二轮：组合/模拟盘数值边界护栏回归测试。

背景：2026-10-14 边界 fuzz 发现 portfolio.add_holding 接受 quantity=10**18
（返回 200 创建天文数字持仓），paper_trading.place_order 同样缺上限校验。
修复：两处加 cost/quantity/price > 1e9 → ValueError(400) 护栏。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_add_holding_rejects_huge_quantity():
    from modules.portfolio import add_holding
    import pytest
    with pytest.raises(ValueError, match="超出合理范围"):
        add_holding(user_id="__bound_test__", symbol="600519", cost=1.0, quantity=10**18)


def test_add_holding_rejects_huge_cost():
    from modules.portfolio import add_holding
    import pytest
    with pytest.raises(ValueError, match="超出合理范围"):
        add_holding(user_id="__bound_test__", symbol="600519", cost=10**12, quantity=1)


def test_add_holding_accepts_normal_values():
    """正常值不受护栏影响（用隔离 user_id，测完清理）。"""
    from modules.portfolio import add_holding, remove_holding
    trade_id = add_holding(user_id="__bound_test__", symbol="600036", cost=40.5, quantity=100)
    assert trade_id > 0
    try:
        remove_holding(trade_id)
    except Exception:
        pass  # remove 接口签名可能不同，清理尽力而为


def test_place_order_rejects_huge_values():
    from modules.paper_trading import place_order
    import pytest
    # quantity 超限（在 account 校验之前就会被拒，用不存在的 account_id 也不影响断言）
    with pytest.raises(ValueError, match="超出合理范围"):
        place_order(account_id=999999, symbol="600519",
                    side="buy", quantity=10**18, price=1.0)
    with pytest.raises(ValueError, match="超出合理范围"):
        place_order(account_id=999999, symbol="600519",
                    side="buy", quantity=1, price=10**12)
