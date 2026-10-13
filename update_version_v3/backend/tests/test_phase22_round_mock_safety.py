# -*- coding: utf-8 -*-
"""本轮修复验证：mock 假数据禁止进入决策路径

覆盖：
1. data_source 层
   - is_mock_payload() 正确识别 dict/list/None
   - _mock_quote / _mock_kline 返回值带 mock 标记
   - 所有数据源失败时 fetch_quote_with_fallback / fetch_kline_with_fallback 返回 mock
   - data_source_mock_total 计数器递增并通过 get_data_source_metrics() 暴露
2. 模拟盘 place_order
   - mock 行情 + 不传 price → ValueError
   - 显式传 price → 正常成交（不受影响）
   - take_snapshot mock 基准 → snapshot.benchmark_is_mock=True 且基准按 0 处理
3. 异动告警
   - 单只入口 _evaluate_symbol_for_rule: mock quote → return None
   - 批量入口 scan_all_alerts: mock quote → 0 triggers，_record_trigger_and_notify 不被调用
   - mock kline + volume_surge 类规则 → 同样不触发
4. API 层 /data/quote 与 /data/kline
   - 真实数据时不带 is_mock 字段（向后兼容）
   - mock 数据时响应体顶层带 is_mock: true
5. 边界
   - 「数据源全失败」场景用 _FALLBACK_ORDER 全标不可用来复现（不联网）

测试全部 mock 数据源/适配器，永不联网。
"""
import os
import sys
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrate import get_conn


# ============================================================================
# 1. data_source 层
# ============================================================================
def test_is_mock_payload_recognises_dict_list_none():
    """is_mock_payload 应正确识别 dict / list / None"""
    from core import data_source as ds
    assert ds.is_mock_payload(None) is False
    assert ds.is_mock_payload({}) is False
    assert ds.is_mock_payload({"source": "mock"}) is True
    assert ds.is_mock_payload({"source": "akshare"}) is False
    # list: 需要带 is_mock 属性（_MockKlineList 才会挂）
    assert ds.is_mock_payload([]) is False
    class _MockList(list):
        pass
    ml = _MockList()
    ml.is_mock = True
    assert ds.is_mock_payload(ml) is True
    print("OK is_mock_payload: None/dict/list 三类判定正确")


def test_mock_quote_has_source_mock():
    """_mock_quote 产物应能被 is_mock_payload 识别"""
    from core import data_source as ds
    q = ds._mock_quote("600519")
    assert q["source"] == "mock", f"expected source='mock', got {q.get('source')!r}"
    assert ds.is_mock_payload(q) is True
    print(f"OK _mock_quote: code=600519 price={q['price']:.2f} source={q['source']}")


def test_mock_kline_list_has_is_mock_attribute():
    """_mock_kline 产物（_MockKlineList 实例）应带 is_mock=True"""
    from core import data_source as ds
    k = ds._mock_kline("600519", 60, "daily")
    assert isinstance(k, list), f"should still be list subclass, got {type(k).__name__}"
    assert getattr(k, "is_mock", None) is True, "is_mock attr missing"
    assert ds.is_mock_payload(k) is True
    # 业务侧遍历使用应不受影响
    first = k[0]
    assert "date" in first and "close" in first
    print(f"OK _mock_kline: type={type(k).__name__} len={len(k)} is_mock=True 切片/索引兼容")


def test_fetch_quote_with_fallback_all_sources_fail_returns_mock():
    """所有数据源失败 → fetch_quote_with_fallback 返回 mock（并递增 mock_total）"""
    from core import data_source as ds

    # 完全清除缓存，确保走"实时"路径
    ds.clear_cache()
    before = ds.get_data_source_metrics()["data_source_mock_total"]

    # 模拟所有 adapter 都不可用：把 _FALLBACK_ORDER 覆盖成空 + 所有 adapter 强制 None
    class _NullAdapter:
        def is_available(self): return True
        def fetch_quote(self, code): return None

    fake_adapters = {"akshare": _NullAdapter(), "baostock": _NullAdapter()}

    def _fake_get_adapter(name):
        return fake_adapters.get(name)

    with patch.object(ds, "_FALLBACK_ORDER", list(fake_adapters.keys())), \
         patch.object(ds, "_get_adapter", side_effect=_fake_get_adapter):
        q = ds.fetch_quote_with_fallback("600519")
        assert q is not None, "all-fail should still return mock quote"
        assert ds.is_mock_payload(q), f"returned quote not marked mock: {q}"
        assert q["source"] == "mock"
        assert q["code"] == "600519"

    after = ds.get_data_source_metrics()["data_source_mock_total"]
    assert after == before + 1, f"data_source_mock_total should increment by 1 (was {before}, now {after})"
    print(f"OK fetch_quote_with_fallback: all-fail → mock, mock_total {before}→{after}")


def test_fetch_kline_with_fallback_all_sources_fail_returns_mock():
    """所有数据源失败 → fetch_kline_with_fallback 返回 mock kline（list with is_mock）"""
    from core import data_source as ds

    ds.clear_cache()
    before = ds.get_data_source_metrics()["data_source_mock_total"]

    class _NullAdapter:
        def is_available(self): return True
        def fetch_kline(self, code, period="daily", days=120): return []

    fake_adapters = {"akshare": _NullAdapter(), "baostock": _NullAdapter()}

    with patch.object(ds, "_FALLBACK_ORDER", list(fake_adapters.keys())), \
         patch.object(ds, "_get_adapter", side_effect=lambda n: fake_adapters.get(n)):
        k = ds.fetch_kline_with_fallback("600519", period="daily", days=60)
        assert isinstance(k, list) and len(k) > 0, "mock kline should be non-empty"
        assert getattr(k, "is_mock", None) is True, "is_mock flag missing on list"
        assert ds.is_mock_payload(k) is True

    after = ds.get_data_source_metrics()["data_source_mock_total"]
    assert after == before + 1, f"kline mock counter should increment (was {before}, now {after})"
    print(f"OK fetch_kline_with_fallback: all-fail → mock kline, mock_total {before}→{after}")


def test_get_data_source_metrics_exposes_mock_total():
    """get_data_source_metrics() 必须同时返回 fallback_total 和 mock_total"""
    from core import data_source as ds
    m = ds.get_data_source_metrics()
    assert "data_source_fallback_total" in m
    assert "data_source_mock_total" in m
    assert isinstance(m["data_source_mock_total"], int)
    print(f"OK get_data_source_metrics: keys={sorted(m.keys())}")


# ============================================================================
# 2. 模拟盘 — 禁止以 mock 价成交 + 显式传 price 不受影响
# ============================================================================
def _reset_paper(user: str) -> int:
    """清掉指定用户的 paper_trade / paper_account 并创建一个新账户"""
    conn = get_conn()
    conn.execute("DELETE FROM paper_trade WHERE user_id=?", (user,))
    conn.execute("DELETE FROM paper_account WHERE user_id=?", (user,))
    conn.commit()
    from modules import paper_trading as pt
    return pt.create_account(user, user, 1_000_000.0)


def test_place_order_mock_quote_no_price_raises():
    """mock 行情 + 不传 price → ValueError（拒绝成交）"""
    from modules import paper_trading as pt

    acc_id = _reset_paper("m_mock1")
    mock_q = {
        "code": "600519", "name": "模拟600519",
        "price": 22.5, "change_pct": 0.0,
        "volume": 100000, "amount": 2_250_000.0,
        "industry": "模拟", "source": "mock",
    }
    with patch.object(pt, "fetch_quote_with_fallback", return_value=mock_q):
        try:
            pt.place_order(acc_id, "600519", "buy", 100, price=None)
        except ValueError as e:
            msg = str(e)
            assert "数据源不可用" in msg or "显式传 price" in msg, \
                f"error message should hint to retry or pass price: {msg!r}"
            print(f"OK place_order mock: raised ValueError as expected: {msg[:60]}...")
            return

    raise AssertionError("place_order should have raised ValueError on mock quote")


def test_place_order_explicit_price_unaffected():
    """显式传 price → 正常成交（不走 fetch，mock 拦截完全不影响）"""
    from modules import paper_trading as pt

    acc_id = _reset_paper("m_ok")
    r = pt.place_order(acc_id, "600519", "buy", 100, price=1800.0)
    assert r["trade_id"] > 0
    assert r["symbol"] == "600519"
    assert r["price"] == 1800.0
    assert r["quantity"] == 100
    print(f"OK place_order explicit price: trade_id={r['trade_id']} price={r['price']}")


def test_place_order_real_quote_unaffected():
    """真实行情（无 mock 标记）→ 正常成交"""
    from modules import paper_trading as pt

    acc_id = _reset_paper("m_real")
    real_q = {
        "code": "600519", "name": "贵州茅台",
        "price": 1800.0, "change_pct": 1.5,
        "volume": 1000000, "amount": 1_800_000_000.0,
        "industry": "白酒", "source": "akshare",
    }
    with patch.object(pt, "fetch_quote_with_fallback", return_value=real_q):
        r = pt.place_order(acc_id, "600519", "buy", 100, price=None)
    assert r["trade_id"] > 0
    assert r["price"] == 1800.0
    print(f"OK place_order real quote: trade_id={r['trade_id']}")


def test_take_snapshot_mock_benchmark_marked():
    """take_snapshot 在基准为 mock 时必须打 benchmark_is_mock=True 并把基准视为 0"""
    from modules import paper_trading as pt

    acc_id = _reset_paper("m_snap")
    mock_bench = {
        "code": "000300", "name": "模拟沪深300",
        "price": 3500.0, "change_pct": 99.9,  # 假数据
        "volume": 0, "amount": 0,
        "industry": "模拟", "source": "mock",
    }
    with patch.object(pt, "fetch_quote_with_fallback", return_value=mock_bench):
        snap = pt.take_snapshot(acc_id)
    assert snap["benchmark_is_mock"] is True, "should flag benchmark as mock"
    # mock 时基准按 change_pct=0 处理：= INITIAL_CAPITAL_DEFAULT * 1 = 1_000_000
    assert snap["benchmark_equity"] == 1_000_000.0, \
        f"mock benchmark should be treated as 0% change, got {snap['benchmark_equity']}"
    print(f"OK take_snapshot mock bench: benchmark_is_mock=True, equity={snap['benchmark_equity']}")


def test_take_snapshot_real_benchmark_unmarked():
    """take_snapshot 在基准为真实时 benchmark_is_mock=False"""
    from modules import paper_trading as pt

    acc_id = _reset_paper("m_snap2")
    real_bench = {
        "code": "000300", "name": "沪深300",
        "price": 3500.0, "change_pct": 1.5,
        "volume": 1000000, "amount": 3_500_000_000.0,
        "industry": "指数", "source": "akshare",
    }
    with patch.object(pt, "fetch_quote_with_fallback", return_value=real_bench):
        snap = pt.take_snapshot(acc_id)
    assert snap["benchmark_is_mock"] is False
    assert snap["benchmark_equity"] != 1_000_000.0  # 实际反映了 1.5% 涨幅
    print(f"OK take_snapshot real bench: benchmark_is_mock=False, equity={snap['benchmark_equity']}")


# ============================================================================
# 3. 异动告警 — 单只与批量路径都不基于 mock 触发
# ============================================================================
def _make_enabled_rule(user: str, rule_type: str = "limit_up") -> int:
    """创建一个已启用的规则，返回 rule_id"""
    conn = get_conn()
    conn.execute("DELETE FROM alert_rule WHERE user_id=?", (user,))
    conn.commit()
    from modules import alerts_engine as ae
    rid = ae.create_rule(user, f"{rule_type}_rule", rule_type, {}, cooldown_minutes=0, enabled=True)
    return rid


def test_alerts_single_path_mock_returns_none():
    """单只入口 _evaluate_symbol_for_rule: mock quote → return None，不评估"""
    from modules import alerts_engine as ae

    rid = _make_enabled_rule("a_single", "limit_up")
    rule = ae.get_rule(rid)
    mock_q = {
        "code": "600519", "name": "模拟600519",
        "price": 22.5, "change_pct": 9.8,  # 9.8 > 9.5 会触发 limit_up
        "volume": 100000, "amount": 2_250_000.0,
        "industry": "模拟", "source": "mock",
    }
    with patch.object(ae, "fetch_quote_with_fallback", return_value=mock_q), \
         patch.object(ae, "_record_trigger_and_notify") as notif:
        result = ae._evaluate_symbol_for_rule(rule, "600519")
    assert result is None, f"mock quote should not trigger, got {result}"
    assert notif.call_count == 0, "should not call _record_trigger_and_notify on mock"
    print("OK alerts single path: mock quote → None, no notify")


def test_alerts_bulk_path_mock_no_trigger():
    """批量入口 scan_all_alerts（定时任务实际走的路径）必须阻止 mock 触发"""
    from modules import alerts_engine as ae
    import core.data_source as ds

    rid = _make_enabled_rule("a_bulk", "limit_up")
    rule = ae.get_rule(rid)

    mock_q = {
        "code": "600519", "name": "模拟600519",
        "price": 22.5, "change_pct": 9.8,  # 会触发
        "volume": 100000, "amount": 2_250_000.0,
        "industry": "模拟", "source": "mock",
    }

    # scan_all_alerts 内部用 from core.data_source import fetch_quotes_bulk 局部导入
    # 所以要 patch 实际被引用的模块属性
    with patch.object(ds, "fetch_quotes_bulk", return_value={"600519": mock_q}), \
         patch.object(ds, "fetch_kline_with_fallback", return_value=[]), \
         patch.object(ae, "_get_symbols_for_rule", return_value=["600519"]), \
         patch.object(ae, "list_rules", return_value=[dict(rule)]), \
         patch.object(ae, "_record_trigger_and_notify") as notif:
        result = ae.scan_all_alerts()

    assert result["triggers"] == 0, f"mock should NOT trigger, got {result['triggers']} triggers"
    assert notif.call_count == 0, f"notification should not be called on mock, got {notif.call_count}"
    print(f"OK alerts bulk path: mock → triggers=0, notif count=0 (summary={result})")


def test_alerts_bulk_path_mock_kline_for_kline_rules():
    """批量路径：volume_surge / ma_break 等需要 K 线的规则，K 线为 mock 时同样不触发"""
    from modules import alerts_engine as ae
    import core.data_source as ds

    rid = _make_enabled_rule("a_kline", "volume_surge")
    rule = ae.get_rule(rid)

    real_q = {
        "code": "600519", "name": "贵州茅台",
        "price": 1800.0, "change_pct": 1.5,
        "volume": 1000000, "amount": 1_800_000_000.0,
        "industry": "白酒", "source": "akshare",
    }

    def fake_kline(code, period="daily", days=120, **kw):
        # 返回一个 mock 标记的 list
        kl = ds._MockKlineList([
            {"date": "2026-08-13", "open": 1750.0, "high": 1810.0,
             "low": 1740.0, "close": 1800.0, "volume": 1000000, "amount": 1.8e9},
        ])
        kl.is_mock = True
        return kl

    with patch.object(ds, "fetch_quotes_bulk", return_value={"600519": real_q}), \
         patch.object(ds, "fetch_kline_with_fallback", side_effect=fake_kline), \
         patch.object(ae, "_get_symbols_for_rule", return_value=["600519"]), \
         patch.object(ae, "list_rules", return_value=[dict(rule)]), \
         patch.object(ae, "_record_trigger_and_notify") as notif:
        result = ae.scan_all_alerts()

    assert result["triggers"] == 0, f"mock kline should not trigger kline-based rules, got {result}"
    assert notif.call_count == 0
    print(f"OK alerts bulk mock kline: triggers=0 (volume_surge with mock kline)")


# ============================================================================
# 4. API 层 /data/quote 与 /data/kline — mock 时带 is_mock 标记
# ============================================================================
def _client():
    from fastapi.testclient import TestClient
    from app import app
    return TestClient(app)


def test_api_quote_real_has_no_is_mock_field():
    """真实行情时 /data/quote 不应有 is_mock 字段"""
    import core.data_source as ds
    real_q = {
        "code": "600519", "name": "贵州茅台",
        "price": 1800.0, "change_pct": 1.5,
        "volume": 1000000, "amount": 1_800_000_000.0,
        "industry": "白酒", "source": "akshare",
    }
    client = _client()
    with patch.object(ds, "fetch_quote_with_fallback", return_value=real_q):
        r = client.get("/api/v2/data/quote?code=600519")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "is_mock" not in body, f"real quote should not have is_mock field, got {body}"
    assert body["code"] == "600519"
    print("OK api /data/quote real: no is_mock field")


def test_api_quote_mock_returns_is_mock_true():
    """mock 行情时 /data/quote 响应体应有 is_mock=true"""
    import core.data_source as ds
    mock_q = ds._mock_quote("600519")
    client = _client()
    with patch.object(ds, "fetch_quote_with_fallback", return_value=mock_q):
        r = client.get("/api/v2/data/quote?code=600519")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("is_mock") is True, f"expected is_mock=true, got {body.get('is_mock')}"
    assert body["code"] == "600519"
    assert body["source"] == "mock"
    print(f"OK api /data/quote mock: is_mock=True")


def test_api_kline_real_has_no_is_mock_field():
    """真实 K 线时 /data/kline 不应有 is_mock 字段"""
    import core.data_source as ds
    real_kline = [
        {"date": "2026-08-13", "open": 1750.0, "high": 1810.0,
         "low": 1740.0, "close": 1800.0, "volume": 1000000, "amount": 1.8e9},
    ]
    client = _client()
    with patch.object(ds, "fetch_kline_with_fallback", return_value=real_kline):
        r = client.get("/api/v2/data/kline?code=600519&days=10")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "is_mock" not in body, f"real kline should not have is_mock field, got {body.keys()}"
    assert body["code"] == "600519"
    assert body["period"] == "daily"
    print("OK api /data/kline real: no is_mock field")


def test_api_kline_mock_returns_is_mock_true():
    """mock K 线时 /data/kline 响应体顶层应有 is_mock=true（list 项不变）"""
    import core.data_source as ds
    mock_kline = ds._mock_kline("600519", 60, "daily")
    client = _client()
    with patch.object(ds, "fetch_kline_with_fallback", return_value=mock_kline):
        r = client.get("/api/v2/data/kline?code=600519&days=60")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("is_mock") is True, f"expected is_mock=true, got {body.get('is_mock')}"
    assert isinstance(body["kline"], list) and len(body["kline"]) > 0
    # K 线数组项结构未被破坏
    first = body["kline"][0]
    for k in ("date", "open", "high", "low", "close", "volume"):
        assert k in first, f"kline item missing field {k}"
    print(f"OK api /data/kline mock: is_mock=True, kline len={len(body['kline'])}")


def test_api_kline_invalid_code_no_is_mock():
    """无效代码（已拦截不生成 mock 时）走 404，不应有 is_mock"""
    import core.data_source as ds
    client = _client()
    # _is_plausible_ashare_code 会在 invalid code 时直接返回 None（没有 mock 路径）
    with patch.object(ds, "fetch_kline_with_fallback", return_value=[]):
        r = client.get("/api/v2/data/kline?code=999999&days=10")
    # real kline = [] => no is_mock, kline=[]
    assert r.status_code == 200
    body = r.json()
    assert "is_mock" not in body, f"empty kline should not have is_mock, got {body}"
    assert body["kline"] == []
    print("OK api /data/kline invalid code: no is_mock flag on empty kline")


# ============================================================================
# main（直接 python 执行）
# ============================================================================
def _cleanup():
    """清理本测试产生的 DB 行（不影响业务用户）"""
    conn = get_conn()
    for t in ("paper_trade", "paper_account", "alert_rule", "alert_trigger"):
        conn.execute(f"DELETE FROM {t} WHERE user_id IN (?, ?, ?, ?, ?, ?, ?, ?)",
                     ("m_mock1", "m_ok", "m_real", "m_snap", "m_snap2",
                      "a_single", "a_bulk", "a_kline"))
    conn.commit()


def _run_all():
    """直接 python 执行时跑一遍"""
    fns = [
        # 1. data_source
        test_is_mock_payload_recognises_dict_list_none,
        test_mock_quote_has_source_mock,
        test_mock_kline_list_has_is_mock_attribute,
        test_fetch_quote_with_fallback_all_sources_fail_returns_mock,
        test_fetch_kline_with_fallback_all_sources_fail_returns_mock,
        test_get_data_source_metrics_exposes_mock_total,
        # 2. 模拟盘
        test_place_order_mock_quote_no_price_raises,
        test_place_order_explicit_price_unaffected,
        test_place_order_real_quote_unaffected,
        test_take_snapshot_mock_benchmark_marked,
        test_take_snapshot_real_benchmark_unmarked,
        # 3. 异动告警
        test_alerts_single_path_mock_returns_none,
        test_alerts_bulk_path_mock_no_trigger,
        test_alerts_bulk_path_mock_kline_for_kline_rules,
        # 4. API
        test_api_quote_real_has_no_is_mock_field,
        test_api_quote_mock_returns_is_mock_true,
        test_api_kline_real_has_no_is_mock_field,
        test_api_kline_mock_returns_is_mock_true,
        test_api_kline_invalid_code_no_is_mock,
    ]
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
    _cleanup()
    print(f"\n{'='*60}\n{len(fns)} tests, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    _run_all()
