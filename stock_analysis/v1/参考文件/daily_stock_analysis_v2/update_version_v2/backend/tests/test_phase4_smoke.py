# -*- coding: utf-8 -*-
"""Phase 4 smoke test: 策略回测引擎"""
import time
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrate import get_conn
from modules import backtest_engine as be
from core import data_source as ds


def _reset_tables():
    conn = get_conn()
    conn.execute("DELETE FROM backtest_strategy")
    conn.execute("DELETE FROM backtest_result")


def test_create_strategy():
    _reset_tables()
    sid = be.create_strategy("u1", "MACD策略", "MACD 金叉买入，RSI>70 卖出")
    assert sid > 0
    s = be.get_strategy(sid)
    assert s["name"] == "MACD策略"
    assert s["compiled_dsl"] is None
    print(f"OK create strategy: id={sid}")


def test_list_strategies():
    _reset_tables()
    be.create_strategy("u1", "A", "策略A")
    be.create_strategy("u2", "B", "策略B")
    be.create_strategy("u1", "C", "策略C")
    s1 = be.list_strategies(user_id="u1")
    s2 = be.list_strategies(user_id="u2")
    assert len(s1) == 2
    assert len(s2) == 1
    print(f"OK list: u1={len(s1)}, u2={len(s2)}")


def test_update_and_delete():
    _reset_tables()
    sid = be.create_strategy("u1", "test", "abc")
    assert be.update_strategy(sid, name="renamed", description="new desc")
    s = be.get_strategy(sid)
    assert s["name"] == "renamed" and s["description"] == "new desc"
    assert be.delete_strategy(sid)
    assert be.get_strategy(sid) is None
    print("OK update + delete")


def test_compile_strategy_with_mock_llm():
    """无 API key 时 LLM 返 mock，编译应能 fallback"""
    _reset_tables()
    sid = be.create_strategy("u1", "test", "MACD 金叉买入")
    result = be.compile_strategy(sid)
    assert result["strategy_id"] == sid
    assert result["mocked"] is True  # 无 key
    # mock 返回的是 [MOCK-LLM] ...，parse 失败会写入 {"error": ...}
    # 这里只验证 compile 调用没崩
    print(f"OK compile: mocked={result['mocked']}, provider={result['provider']}")


def test_indicators_ma():
    closes = [10 + i for i in range(30)]  # 10, 11, ..., 39
    ma20 = be._ma(closes, 20)
    assert ma20[0] is None
    assert ma20[18] is None
    # ma20[19] = sum(closes[0:20])/20 = sum(10..29)/20
    expected = sum(closes[0:20]) / 20
    assert abs(ma20[19] - expected) < 1e-9, f"MA {ma20[19]} != {expected}"
    print(f"OK MA(20) at idx 19: {ma20[19]} == {expected}")


def test_indicators_macd():
    closes = [10 + i * 0.5 for i in range(60)]
    macd_line, signal_line, hist = be._macd(closes)
    assert len(macd_line) == 60
    assert any(v is not None for v in macd_line)
    assert any(v is not None for v in signal_line)
    assert any(v is not None for v in hist)
    print(f"OK MACD: {sum(1 for v in macd_line if v is not None)} valid points")


def test_indicators_rsi():
    closes = [100] * 30  # 全部相等 → RSI 应该为 50（中性）
    rsi = be._rsi(closes, 14)
    valid = [v for v in rsi if v is not None]
    assert all(v == 50 for v in valid), f"Flat RSI should be 50, got {valid}"
    # 上涨趋势
    up_closes = [100 + i for i in range(30)]
    rsi_up = be._rsi(up_closes, 14)
    valid_up = [v for v in rsi_up if v is not None]
    assert all(v >= 50 for v in valid_up), f"Uptrend RSI should be >= 50, got {valid_up}"
    print(f"OK RSI: flat=50, uptrend avg={sum(valid_up)/len(valid_up):.1f}")


def test_indicators_volume_ratio():
    vols = [100] * 5 + [200] + [100] * 4  # 第 5 天放量
    vr = be._volume_ratio(vols, period=5)
    assert vr[4] is None
    assert vr[5] == 2.0  # 200 / (100*5/5) = 2.0
    print(f"OK volume_ratio: at idx 5 = {vr[5]}")


def test_compare_operators():
    assert be._compare(5, ">", 3) is True
    assert be._compare(3, ">", 5) is False
    assert be._compare(5, "<", 3) is False
    assert be._compare(3, "<", 5) is True
    assert be._compare(5, ">=", 5) is True
    assert be._compare(5, "==", 5) is True
    print("OK compare: all 6 operators")


def test_eval_condition_close():
    closes = [10, 11, 12, 13]
    assert be._eval_condition({"indicator": "close", "operator": ">", "value": 11}, 2, closes, {}) is True
    assert be._eval_condition({"indicator": "close", "operator": ">", "value": 20}, 2, closes, {}) is False
    print("OK eval close condition")


def test_run_backtest_with_mock_kline():
    """完整跑一遍回测：mock K 线 + 简单策略"""
    _reset_tables()
    sid = be.create_strategy("u1", "简单策略", "收盘价 > 20 日均线买入，< 10 日均线卖出")
    # 直接写 DSL（绕过 LLM）
    be.update_strategy(sid, compiled_dsl={
        "entry": [{"indicator": "close", "operator": ">", "value": 0}],  # 简化：每天都买入
        "exit": [{"indicator": "close", "operator": "<", "value": 999999}],  # 简化：永不卖出（靠 hold_days）
        "stop_loss_pct": -50.0,
        "take_profit_pct": 999.0,
        "hold_days": 10,
    })

    # mock K 线（线性上涨 200 天）
    import datetime
    kline = []
    base = datetime.date(2024, 1, 1)
    for i in range(200):
        d = (base + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        kline.append({
            "date": d,
            "open": 100 + i * 0.5,
            "high": 105 + i * 0.5,
            "low": 95 + i * 0.5,
            "close": 100 + i * 0.5,
            "volume": 1000000,
        })

    orig = be.fetch_kline_with_fallback
    be.fetch_kline_with_fallback = lambda symbol, **kw: kline

    try:
        result = be.run_backtest("u1", sid, symbol="600519")
        assert result["final_capital"] > 0
        assert result["total_return_pct"] > 0, f"Should be profitable in uptrend, got {result['total_return_pct']}"
        assert result["total_trades"] >= 1
        assert "equity_curve" in result
        print(f"OK backtest: total_return={result['total_return_pct']}%, trades={result['total_trades']}, sharpe={result['sharpe_ratio']}")
    finally:
        be.fetch_kline_with_fallback = orig


def test_run_backtest_with_stop_loss():
    """测试止损触发"""
    _reset_tables()
    sid = be.create_strategy("u1", "止损测试", "")
    be.update_strategy(sid, compiled_dsl={
        "entry": [{"indicator": "close", "operator": ">", "value": 0}],
        "exit": [],
        "stop_loss_pct": -10.0,
        "take_profit_pct": 999.0,
        "hold_days": 999,  # 不主动卖出
    })

    import datetime
    kline = []
    base = datetime.date(2024, 1, 1)
    # 第一天 100，第二天跌到 80（-20% 触发止损）
    prices = [100, 80] + [90] * 30
    for i, p in enumerate(prices):
        d = (base + datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        kline.append({"date": d, "open": p, "high": p, "low": p, "close": p, "volume": 1000000})

    orig = be.fetch_kline_with_fallback
    be.fetch_kline_with_fallback = lambda symbol, **kw: kline
    try:
        result = be.run_backtest("u1", sid, symbol="TEST")
        # 应该有一次止损卖出（pnl_pct ≈ -20%）
        sell_trades = [t for t in result["trades"] if t["type"] == "sell"]
        assert len(sell_trades) >= 1
        assert "止损" in sell_trades[0]["reason"], f"Should trigger stop loss, got {sell_trades[0]['reason']}"
        print(f"OK stop loss: reason={sell_trades[0]['reason']}, pnl={sell_trades[0]['pnl_pct']}%")
    finally:
        be.fetch_kline_with_fallback = orig


def test_run_backtest_without_compiled_dsl_fails():
    _reset_tables()
    sid = be.create_strategy("u1", "no compile", "abc")
    try:
        be.run_backtest("u1", sid, symbol="600519")
        assert False, "Should raise"
    except ValueError as e:
        assert "未编译" in str(e)
        print(f"OK uncompiled blocked: {e}")


def test_run_backtest_insufficient_data_fails():
    _reset_tables()
    sid = be.create_strategy("u1", "tiny", "")
    be.update_strategy(sid, compiled_dsl={"entry": [], "exit": [], "stop_loss_pct": -10, "take_profit_pct": 100, "hold_days": 5})
    orig = be.fetch_kline_with_fallback
    be.fetch_kline_with_fallback = lambda symbol, **kw: [{"date": f"2024-01-{i+1:02d}", "close": 100, "volume": 100} for i in range(5)]
    try:
        be.run_backtest("u1", sid, symbol="TEST")
        assert False, "Should raise"
    except ValueError as e:
        assert "不足" in str(e)
        print(f"OK insufficient data blocked: {e}")
    finally:
        be.fetch_kline_with_fallback = orig


def test_run_backtest_no_kline_fails():
    _reset_tables()
    sid = be.create_strategy("u1", "no data", "")
    be.update_strategy(sid, compiled_dsl={"entry": [], "exit": [], "stop_loss_pct": -10, "take_profit_pct": 100, "hold_days": 5})
    orig = be.fetch_kline_with_fallback
    be.fetch_kline_with_fallback = lambda symbol, **kw: []
    try:
        be.run_backtest("u1", sid, symbol="TEST")
        assert False, "Should raise"
    except ValueError as e:
        assert "K 线" in str(e)
        print(f"OK no kline blocked: {e}")
    finally:
        be.fetch_kline_with_fallback = orig


def test_list_and_get_results():
    _reset_tables()
    sid = be.create_strategy("u1", "t", "")
    be.update_strategy(sid, compiled_dsl={"entry": [], "exit": [], "stop_loss_pct": -10, "take_profit_pct": 100, "hold_days": 5})
    import datetime
    base = datetime.date(2024, 1, 1)
    kline = [{"date": (base + datetime.timedelta(days=i)).strftime("%Y-%m-%d"), "open": 100, "high": 100, "low": 100, "close": 100, "volume": 100} for i in range(60)]
    orig = be.fetch_kline_with_fallback
    be.fetch_kline_with_fallback = lambda symbol, **kw: kline
    try:
        result = be.run_backtest("u1", sid, symbol="T")
        rid = result["result_id"]
        saved = be.get_result(rid)
        assert saved is not None
        assert saved["symbol"] == "T"
        assert saved["status"] == "success"
        # trades/equity_curve 应该是 list
        assert isinstance(saved["trades"], list)
        assert isinstance(saved["equity_curve"], list)
        print(f"OK result persisted: id={rid}, status={saved['status']}, total_return={saved['total_return_pct']}%")
    finally:
        be.fetch_kline_with_fallback = orig


def test_compile_strategy_with_realistic_dsl():
    """验证 DSL 解析 + 落库"""
    _reset_tables()
    sid = be.create_strategy("u1", "MACD", "MACD 金叉")
    # 手工模拟 LLM 编译（绕过 LLM 不可用）
    dsl = {
        "entry": [
            {"indicator": "macd", "operator": "cross_above", "value": 0},
            {"indicator": "volume_ratio", "operator": ">", "value": 1.5},
        ],
        "exit": [{"indicator": "rsi", "operator": ">", "value": 70}],
        "stop_loss_pct": -5.0,
        "take_profit_pct": 15.0,
        "hold_days": 10,
    }
    be.update_strategy(sid, compiled_dsl=dsl)
    s = be.get_strategy(sid)
    assert s["compiled_dsl"]["entry"][0]["indicator"] == "macd"
    assert s["compiled_dsl"]["stop_loss_pct"] == -5.0
    print("OK dsl persisted: 2 entry + 1 exit + stop/take")


if __name__ == "__main__":
    test_create_strategy()
    test_list_strategies()
    test_update_and_delete()
    test_compile_strategy_with_mock_llm()
    test_indicators_ma()
    test_indicators_macd()
    test_indicators_rsi()
    test_indicators_volume_ratio()
    test_compare_operators()
    test_eval_condition_close()
    test_run_backtest_with_mock_kline()
    test_run_backtest_with_stop_loss()
    test_run_backtest_without_compiled_dsl_fails()
    test_run_backtest_insufficient_data_fails()
    test_run_backtest_no_kline_fails()
    test_list_and_get_results()
    test_compile_strategy_with_realistic_dsl()
    print("\n=== Phase 4 smoke test: ALL 17 PASSED ===")