# -*- coding: utf-8 -*-
"""Phase 3 smoke test: 异动规则引擎"""
import time
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrate import get_conn
from modules import alerts_engine as ae
from core import data_source as ds


def _reset_tables():
    conn = get_conn()
    conn.execute("DELETE FROM alert_rule")
    conn.execute("DELETE FROM alert_trigger")
    conn.execute("DELETE FROM notification_channel")
    conn.execute("DELETE FROM notification_log")


def test_list_rule_types():
    types = ae.list_rule_types()
    assert len(types) == 8
    assert any(t["type"] == "opening_spike" for t in types)
    print(f"OK rule types: {len(types)} types registered")


def test_create_rule():
    _reset_tables()
    rid = ae.create_rule(
        user_id="u1",
        name="茅台异动",
        rule_type="opening_spike",
        params={"threshold_pct": 3.0},
    )
    assert rid > 0
    rules = ae.list_rules(user_id="u1")
    assert len(rules) == 1
    assert rules[0]["params"]["threshold_pct"] == 3.0
    print(f"OK create rule: id={rid}")


def test_create_rule_invalid_type():
    try:
        ae.create_rule("u1", "bad", "invalid_type", {})
        assert False, "Should raise"
    except ValueError as e:
        assert "Unknown rule_type" in str(e)
        print(f"OK invalid rule type blocked: {e}")


def test_update_and_delete_rule():
    _reset_tables()
    rid = ae.create_rule("u1", "test", "limit_up", {})
    assert ae.update_rule(rid, name="renamed", enabled=False)
    rule = ae.get_rule(rid)
    assert rule["name"] == "renamed"
    assert rule["enabled"] == 0
    assert ae.delete_rule(rid)
    assert ae.get_rule(rid) is None
    print(f"OK update + delete: id={rid}")


def test_evaluate_opening_spike():
    quote = {"code": "600519", "price": 1500, "change_pct": 3.5}
    triggered = ae._evaluate_opening_spike(quote, {"threshold_pct": 3.0})
    assert triggered is not None
    assert triggered["metric"] == 3.5
    print(f"OK opening_spike: 3.5% > 3.0% threshold")

    no_trigger = ae._evaluate_opening_spike({"change_pct": 2.5}, {"threshold_pct": 3.0})
    assert no_trigger is None
    print("OK opening_spike: 2.5% < 3.0% no trigger")


def test_evaluate_closing_spike():
    triggered = ae._evaluate_closing_spike({"change_pct": 2.3}, {"threshold_pct": 2.0})
    assert triggered is not None
    no_trigger = ae._evaluate_closing_spike({"change_pct": 1.5}, {"threshold_pct": 2.0})
    assert no_trigger is None
    print("OK closing_spike: threshold check")


def test_evaluate_volume_surge():
    quote = {"volume": 1500}
    kline = [{"volume": 500}, {"volume": 600}, {"volume": 700}, {"volume": 800}]  # avg(500,600,700) = 600
    triggered = ae._evaluate_volume_surge(quote, {"multiplier": 1.5}, kline)
    assert triggered is not None
    assert "1.5" in triggered["commentary"]
    # 不够放量
    no_trigger = ae._evaluate_volume_surge({"volume": 600}, {"multiplier": 1.5}, kline)
    assert no_trigger is None
    print("OK volume_surge: 1500 > 600*1.5")


def test_evaluate_volume_surge_insufficient_data():
    quote = {"volume": 1500}
    kline = [{"volume": 100}]  # 只有 1 条
    triggered = ae._evaluate_volume_surge(quote, {"multiplier": 1.5}, kline)
    assert triggered is None, "Should return None with insufficient data"
    print("OK volume_surge: <4 klines -> None")


def test_evaluate_ma_break_above():
    quote = {"price": 100}
    kline = [{"close": 95 + i * 0.5} for i in range(20)]  # ma = (95 + 96 + ... + 104.5) / 20 ≈ 99.75
    triggered = ae._evaluate_ma_break(quote, {"period": 20, "direction": "above"}, kline)
    assert triggered is not None
    assert triggered["metric"] == 100
    print(f"OK ma_break above: 100 > MA {triggered['threshold']:.2f}")


def test_evaluate_ma_break_below():
    quote = {"price": 90}
    kline = [{"close": 95 + i * 0.5} for i in range(20)]
    triggered = ae._evaluate_ma_break(quote, {"period": 20, "direction": "below"}, kline)
    assert triggered is not None
    print("OK ma_break below: 90 < MA")


def test_evaluate_limit_up_down():
    triggered = ae._evaluate_limit_up({"change_pct": 10.0}, {})
    assert triggered is not None
    triggered_down = ae._evaluate_limit_down({"change_pct": -10.0}, {})
    assert triggered_down is not None
    # 边界
    edge_up = ae._evaluate_limit_up({"change_pct": 9.4}, {})
    assert edge_up is None, "9.4% should not trigger"
    edge_down = ae._evaluate_limit_down({"change_pct": -9.4}, {})
    assert edge_down is None
    print("OK limit_up/down: thresholds check")


def test_evaluate_volume_ratio():
    triggered = ae._evaluate_volume_ratio({"volume_ratio": 3.5}, {"threshold": 2.0})
    assert triggered is not None
    no_trigger = ae._evaluate_volume_ratio({"volume_ratio": 1.5}, {"threshold": 2.0})
    assert no_trigger is None
    print("OK volume_ratio: 3.5 > 2.0")


def test_evaluate_north_flow():
    triggered = ae._evaluate_north_flow({"north_flow_wan": 8000}, {"threshold_wan": 5000})
    assert triggered is not None
    print("OK north_flow: 8000万 > 5000万")


def test_get_symbols_from_filter():
    _reset_tables()
    rid = ae.create_rule(
        "u1", "test", "limit_up", {},
        symbol_filter="600519,000858,002156",
    )
    rule = ae.get_rule(rid)
    symbols = ae._get_symbols_for_rule(rule)
    assert symbols == ["600519", "000858", "002156"]
    print(f"OK symbols: {symbols}")


def test_record_trigger_and_notify_with_mock_data_source():
    """端到端：mock 数据源 + 创建规则 + 扫描 + 验证 trigger 记录 + 通知"""
    _reset_tables()
    # 加一个推送通道（指向不可达 URL，但 quiet hours 关掉）
    from core.notifier import add_channel
    ch_id = add_channel("u2", "wechat_work", "https://127.0.0.1:1/nonexistent")

    # mock 数据源 — ★ Round 4：scan_all_alerts 改为走 fetch_quotes_bulk（位于 core.data_source），
    #   因此 patch 的应是 core.data_source.fetch_quote_with_fallback 而非 ae.fetch_quote_with_fallback
    from core import data_source as ds
    orig_quote = ds.fetch_quote_with_fallback
    def mock_quote(code):
        return {"code": code, "name": f"测试-{code}", "price": 1500, "change_pct": 10.0}
    ds.fetch_quote_with_fallback = mock_quote

    # 创建规则
    rid = ae.create_rule(
        "u2", "涨停监控", "limit_up", {},
        symbol_filter="600519",
    )

    try:
        summary = ae.scan_all_alerts()
        assert summary["scanned_rules"] == 1
        assert summary["triggers"] == 1, f"Expected 1 trigger, got {summary}"
        print(f"OK scan: {summary}")

        # 验证 trigger 表有记录
        triggers = ae.list_triggers(user_id="u2")
        assert len(triggers) == 1
        assert triggers[0]["symbol"] == "600519"
        assert triggers[0]["metric_value"] == 10.0
        print(f"OK trigger recorded: {triggers[0]['symbol']} metric={triggers[0]['metric_value']}")

        # 验证推送日志（虽然推送失败，但 log 应记录 failed）
        logs = _get_channel_logs_for_user("u2")
        assert len(logs) >= 1
        assert logs[0]["status"] == "failed"
        print(f"OK notify log: status={logs[0]['status']}, error={logs[0]['error_message'][:30]}")

        # 验证 cooldown：再次扫描应该不再 notify（但 trigger 表可能再写）
        logs_before = len(logs)
        ae.scan_all_alerts()
        logs_after = _get_channel_logs_for_user("u2")
        assert len(logs_after) == logs_before, "Cooldown should prevent duplicate notify"
        print(f"OK cooldown: no duplicate notify (logs stayed at {logs_before})")
    finally:
        from core import data_source as ds
        ds.fetch_quote_with_fallback = orig_quote


def _get_channel_logs_for_user(user_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM notification_log WHERE user_id=? ORDER BY created_at DESC", (user_id,))
    return [dict(r) for r in cur.fetchall()]


def test_scan_handles_data_source_failure_gracefully():
    """数据源全失败时，扫描不应抛异常"""
    _reset_tables()
    rid = ae.create_rule("u3", "test", "limit_up", {}, symbol_filter="600519")
    # 不 mock，fetch_quote_with_fallback 会返回 None（数据源失败）
    summary = ae.scan_all_alerts()
    assert summary["errors"] == 0  # 不应算 error
    assert summary["triggers"] == 0
    print(f"OK graceful: data source fail -> no crash ({summary})")


def test_scan_multiple_rules_different_types():
    """多个不同类型规则同时扫描"""
    _reset_tables()
    ae.create_rule("u4", "r1", "limit_up", {}, symbol_filter="600519")
    ae.create_rule("u4", "r2", "opening_spike", {"threshold_pct": 3.0}, symbol_filter="600519")
    ae.create_rule("u4", "r3", "volume_ratio", {"threshold": 2.0}, symbol_filter="600519")

    # ★ Round 4：scan 走 fetch_quotes_bulk，patch 路径必须改为 ds
    from core import data_source as ds
    orig = ds.fetch_quote_with_fallback
    def mock_quote(code):
        return {"code": code, "name": "test", "price": 100, "change_pct": 10.0, "volume_ratio": 3.5}
    ds.fetch_quote_with_fallback = mock_quote

    try:
        summary = ae.scan_all_alerts()
        # 三条规则都该 trigger
        assert summary["scanned_rules"] == 3
        assert summary["triggers"] >= 3, f"Expected 3+ triggers, got {summary}"
        print(f"OK multi-rule: {summary}")
    finally:
        ds.fetch_quote_with_fallback = orig


def test_disabled_rule_not_scanned():
    _reset_tables()
    ae.create_rule("u5", "test", "limit_up", {}, symbol_filter="600519", enabled=False)
    summary = ae.scan_all_alerts()
    assert summary["scanned_rules"] == 0, f"Disabled rule should not be scanned"
    print("OK disabled rule skipped")


if __name__ == "__main__":
    test_list_rule_types()
    test_create_rule()
    test_create_rule_invalid_type()
    test_update_and_delete_rule()
    test_evaluate_opening_spike()
    test_evaluate_closing_spike()
    test_evaluate_volume_surge()
    test_evaluate_volume_surge_insufficient_data()
    test_evaluate_ma_break_above()
    test_evaluate_ma_break_below()
    test_evaluate_limit_up_down()
    test_evaluate_volume_ratio()
    test_evaluate_north_flow()
    test_get_symbols_from_filter()
    test_record_trigger_and_notify_with_mock_data_source()
    test_scan_handles_data_source_failure_gracefully()
    test_scan_multiple_rules_different_types()
    test_disabled_rule_not_scanned()
    print("\n=== Phase 3 smoke test: ALL 18 PASSED ===")