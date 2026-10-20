# -*- coding: utf-8 -*-
"""Regression tests for two defects reproduced against the live container.

Both were found in ``docker logs dsa-v2`` (2026-10-20) and share one theme:
a guard existed on the quote path but was missing on the sibling path.

1. **Alert scanning froze for hours.** ``fetch_kline_with_fallback`` called
   ``adapter.fetch_kline`` directly while ``fetch_quote_with_fallback`` had a
   daemon-thread hard timeout. baostock's socket read has no internal timeout,
   so one stuck connection hung the scan job. APScheduler runs it with
   ``max_instances=1``, so every later run was skipped with
   ``maximum number of running instances reached`` — alerting went silent and
   the only trace was a WARNING.

2. **A rule with ``params_json='null'`` never fired.** ``update_rule`` guarded
   the write side (``None -> {}``) but the readers did not, so a legacy row
   parsed back to ``None`` and the evaluator raised
   ``'NoneType' object has no attribute 'get'`` every 5 minutes. The exception
   was swallowed into an ERROR log, so the user just saw "no alert".

Run: ``python -m pytest update_version/backend/tests/test_phase61_kline_timeout_and_null_params.py``
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from core import data_source as ds
from modules import alerts_engine as ae


# ---------------------------------------------------------------------------
# 1. K-line fetch must never block past its per-source budget
# ---------------------------------------------------------------------------
class _HangingAdapter:
    """Adapter whose fetch_kline blocks forever, like baostock on a dead socket."""

    def __init__(self) -> None:
        self.released = False
        # 真实适配器继承 DataSourceAdapter，超时路径会调用 record_failure/success
        self.health: list[tuple[str, str]] = []

    def record_failure(self, err="") -> None:
        self.health.append(("failure", err))

    def record_success(self) -> None:
        self.health.append(("success", ""))

    def is_available(self) -> bool:
        return True

    def fetch_kline(self, code, period="daily", days=120):
        while not self.released:
            time.sleep(0.05)
        return []

    def fetch_quote(self, code):
        while not self.released:
            time.sleep(0.05)
        return None


def test_kline_fetch_is_bounded_by_hard_timeout(monkeypatch) -> None:
    """A hung source must be abandoned after _KLINE_SOURCE_TIMEOUT, not waited on."""
    adapter = _HangingAdapter()
    monkeypatch.setattr(ds, "_get_adapter", lambda src: adapter if src == ds._FALLBACK_ORDER[0] else None)
    monkeypatch.setattr(ds, "_KLINE_SOURCE_TIMEOUT", 0.3)
    ds.clear_cache()

    try:
        started = time.monotonic()
        result = ds.fetch_kline_with_fallback("600519", days=60)
        elapsed = time.monotonic() - started
    finally:
        adapter.released = True

    assert elapsed < 3.0, f"fetch_kline blocked {elapsed:.1f}s despite a 0.3s budget"
    # 全部数据源失败 → mock 兜底，调用方拿到可用结构而不是空等
    assert isinstance(result, list) and result, "should fall back to mock kline, not None"


def test_kline_timeout_reports_the_source(monkeypatch) -> None:
    """The timeout must be logged as a source failure, so logs show the culprit."""
    adapter = _HangingAdapter()
    monkeypatch.setattr(ds, "_get_adapter", lambda src: adapter if src == ds._FALLBACK_ORDER[0] else None)
    monkeypatch.setattr(ds, "_KLINE_SOURCE_TIMEOUT", 0.2)
    ds.clear_cache()

    warnings: list[str] = []

    class _Capture:
        def warning(self, msg, *args):
            warnings.append(msg % args if args else msg)

        def __getattr__(self, _name):
            return lambda *a, **k: None

    monkeypatch.setattr(ds, "logger", _Capture())
    try:
        ds.fetch_kline_with_fallback("600519", days=60)
    finally:
        adapter.released = True

    assert any("kline timed out" in w for w in warnings), f"no timeout warning captured: {warnings}"


# ---------------------------------------------------------------------------
# 2. Rule params must never come back as None from the readers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("null", {}),          # 旧版 json.dumps(None) 写进去的存量行
        (None, {}),            # 列为 NULL
        ("", {}),              # 空串
        ("{not json}", {}),    # 损坏数据
        ('["a"]', {}),         # 合法 JSON 但不是对象
        ('{"multiplier": 2}', {"multiplier": 2}),
    ],
)
def test_parse_rule_params_never_returns_none(raw, expected) -> None:
    parsed = ae._parse_rule_params(raw)
    assert parsed is not None, f"{raw!r} parsed to None — evaluator would crash on .get()"
    assert parsed == expected


def test_volume_surge_evaluator_survives_null_params_rule() -> None:
    """A rule whose params were stored as 'null' must evaluate, not raise.

    This is the exact live failure: scan_all_alerts caught the AttributeError,
    logged it, and the rule silently never fired.
    """
    params = ae._parse_rule_params("null")
    quote = {"code": "600519", "price": 1700.0, "change_pct": 1.0, "volume": 3_000_000}
    kline = [
        {"date": "2026-10-16", "open": 10, "close": 11, "high": 11, "low": 10, "volume": 1000},
        {"date": "2026-10-17", "open": 11, "close": 12, "high": 12, "low": 11, "volume": 1000},
        {"date": "2026-10-20", "open": 12, "close": 12, "high": 13, "low": 12, "volume": 9000},
    ]
    # 不抛 AttributeError 即可；返回值取决于数据，此处只验证"能算出来"
    ae._evaluate_volume_surge(quote, params, kline)


def test_list_rules_never_yields_none_params(monkeypatch) -> None:
    """list_rules must hand evaluators a dict even for legacy 'null' rows."""
    from modules import alerts_engine

    monkeypatch.setattr(
        alerts_engine,
        "get_conn",
        lambda: _FakeConn([{"id": 34, "rule_type": "volume_surge", "params_json": "null", "enabled": 1,
                            "user_id": "default", "created_at": 0}]),
    )
    rules = alerts_engine.list_rules()
    assert rules and rules[0]["params"] is not None
    assert rules[0]["params"] == {}


def test_get_rule_never_yields_none_params(monkeypatch) -> None:
    from modules import alerts_engine

    monkeypatch.setattr(
        alerts_engine,
        "get_conn",
        lambda: _FakeConn([{"id": 34, "rule_type": "volume_surge", "params_json": "null", "enabled": 1,
                            "user_id": "default", "created_at": 0}]),
    )
    rule = alerts_engine.get_rule(34)
    assert rule is not None and rule["params"] == {}


class _FakeCursor:
    def __init__(self, rows) -> None:
        self._rows = rows

    def execute(self, *a, **k) -> None:
        return None

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, rows) -> None:
        self._rows = rows

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)


# ---------------------------------------------------------------------------
# 3. Scan must finish inside one interval even when every source is slow
# ---------------------------------------------------------------------------
def test_scan_passes_a_fetch_budget_to_bulk_quotes(monkeypatch) -> None:
    """fetch_quotes_bulk accepts budget_seconds; the scan must actually pass one.

    Without it a slow market for N symbols serialises the whole fallback chain
    and the round overruns the 5-minute interval — which is what silently
    disabled alerting for hours.
    """
    seen: dict = {}

    def _bulk(codes, parallel=True, budget_seconds=None):
        seen["codes"] = list(codes)
        seen["budget"] = budget_seconds
        return {c: None for c in codes}  # 全 None → 扫描到此为止

    monkeypatch.setattr(ae, "list_rules", lambda: [
        {"id": 1, "rule_type": "volume_surge", "params": {}, "enabled": 1,
         "symbol_filter": "600519,000858", "user_id": "default"},
    ])
    monkeypatch.setattr("core.data_source.fetch_quotes_bulk", _bulk)
    # quotes 全是 None 时扫描仍会去拉 K 线；不 mock 就会打真实网络（实测 33s）。
    monkeypatch.setattr("core.data_source.fetch_kline_with_fallback",
                        lambda sym, days=60, **kw: [])

    summary = ae.scan_all_alerts()

    assert seen["budget"], "scan must pass budget_seconds to fetch_quotes_bulk"
    assert seen["budget"] > 0
    assert summary["errors"] == 0


def test_scan_stops_collecting_klines_once_budget_is_spent(monkeypatch) -> None:
    """When the kline budget runs out the round must end, not keep fetching."""
    calls: list[str] = []

    def _slow_kline(sym, days=60, **kw):
        calls.append(sym)
        time.sleep(0.3)  # 模拟慢源；预算会被设成 0 让它立刻耗尽
        return []

    monkeypatch.setattr(ae, "_SCAN_FETCH_BUDGET", 0.0)
    monkeypatch.setattr(ae, "list_rules", lambda: [
        {"id": 1, "rule_type": "volume_surge", "params": {}, "enabled": 1,
         "symbol_filter": "600519,000858,000001", "user_id": "default"},
    ])
    monkeypatch.setattr("core.data_source.fetch_quotes_bulk",
                        lambda codes, parallel=True, budget_seconds=None: {c: {"price": 1} for c in codes})
    monkeypatch.setattr("core.data_source.fetch_kline_with_fallback", _slow_kline)

    started = time.monotonic()
    summary = ae.scan_all_alerts()
    elapsed = time.monotonic() - started

    assert calls == [], f"budget already spent, yet fetched {calls}"
    assert elapsed < 1.0
    assert summary["errors"] == 0


def test_scan_budget_is_shorter_than_the_cron_interval() -> None:
    """The budget must leave room for evaluation inside the 5-minute interval."""
    assert 0 < ae._SCAN_FETCH_BUDGET < 300, (
        f"budget {ae._SCAN_FETCH_BUDGET}s does not fit the 5-minute scan interval"
    )


# ---------------------------------------------------------------------------
# 4. A timed-out source must be recorded as unhealthy
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("fetcher", ["fetch_quote_with_fallback", "fetch_kline_with_fallback"])
def test_source_timeout_is_recorded_as_a_failure(monkeypatch, fetcher) -> None:
    """The hard timeout fires in the *caller's* thread, so the adapter's own
    ``record_failure`` never runs. Live evidence: ``baostock`` logged 58 quote
    timeouts while ``data_source_health`` still showed ``failure_count=0``, so
    ``/ready`` advertised a permanently-timing-out source as 100% healthy and
    the circuit breaker never tripped.
    """
    recorded: list[tuple[str, str]] = []

    class _SlowAdapter:
        def is_available(self) -> bool:
            return True

        def record_failure(self, err=""):
            recorded.append(("failure", err))

        def record_success(self):
            recorded.append(("success", ""))

        def fetch_quote(self, code):
            time.sleep(5)
            return None

        def fetch_kline(self, code, period="daily", days=120):
            time.sleep(5)
            return []

    adapter = _SlowAdapter()
    monkeypatch.setattr(ds, "_get_adapter", lambda src: adapter if src == ds._FALLBACK_ORDER[0] else None)
    monkeypatch.setattr(ds, "_QUOTE_SOURCE_TIMEOUT", 0.2)
    monkeypatch.setattr(ds, "_KLINE_SOURCE_TIMEOUT", 0.2)
    ds.clear_cache()

    getattr(ds, fetcher)("600519")

    assert recorded, f"{fetcher} timed out but recorded no health event at all"
    assert recorded[0][0] == "failure", f"expected a failure record, got {recorded}"
    assert "timed out" in recorded[0][1]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
