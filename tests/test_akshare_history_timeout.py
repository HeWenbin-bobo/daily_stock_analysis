# -*- coding: utf-8 -*-
"""Regression tests for Akshare historical fallback timeout handling."""

import multiprocessing
import sys
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from data_provider.akshare_fetcher import AkshareFetcher, _akshare_call_with_timeout
from data_provider.base import DataFetchError


def _sleep_for(seconds: float) -> None:
    time.sleep(seconds)


def _return_value(value):
    return value


def test_akshare_call_with_timeout_uses_spawn_context(monkeypatch) -> None:
    requested_methods = []
    call_order = []

    class FakeConnection:
        def __init__(self, messages):
            self.messages = messages

        def send(self, value):
            self.messages.append(value)

        def poll(self, timeout):
            return bool(self.messages)

        def recv(self):
            if not self.messages:
                raise EOFError
            return self.messages.pop(0)

        def close(self):
            pass

    class FakeProcess:
        def __init__(self, target, args, name, daemon):
            self.target = target
            self.args = args
            self.name = name
            self.daemon = daemon

        def start(self):
            self.target(*self.args)

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return False

        def terminate(self):
            pass

        def kill(self):
            pass

    class FakeContext:
        def Pipe(self, duplex=False):
            messages = []
            return FakeConnection(messages), FakeConnection(messages)

        Process = FakeProcess

    def fake_get_context(method=None):
        call_order.append("get_context")
        requested_methods.append(method)
        return FakeContext()

    def fake_freeze_support():
        call_order.append("freeze_support")

    monkeypatch.setattr(
        "data_provider.akshare_fetcher.multiprocessing.get_context",
        fake_get_context,
    )
    monkeypatch.setattr(
        "data_provider.akshare_fetcher.multiprocessing.freeze_support",
        fake_freeze_support,
    )

    result = _akshare_call_with_timeout(
        _return_value,
        "ok",
        timeout=1,
        call_name="unit-default-context",
    )

    assert result == "ok"
    assert requested_methods == ["spawn"]
    assert call_order == ["freeze_support", "get_context"]


def test_akshare_call_with_timeout_returns_promptly() -> None:
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="unit-hang"):
        _akshare_call_with_timeout(
            _sleep_for,
            0.2,
            timeout=0.01,
            call_name="unit-hang",
        )

    assert time.monotonic() - started < 0.5


def test_akshare_call_with_timeout_reaps_timed_out_worker_process() -> None:
    call_name = "unit-hang-reap"

    with pytest.raises(TimeoutError, match=call_name):
        _akshare_call_with_timeout(
            _sleep_for,
            5,
            timeout=0.01,
            call_name=call_name,
        )

    leaked = [
        process
        for process in multiprocessing.active_children()
        if process.name == f"akshare-{call_name}"
    ]
    assert leaked == []


@pytest.mark.parametrize(
    ("method_name", "api_name", "call_name", "code", "expected_symbol", "frame_kind"),
    [
        ("_fetch_stock_data_em", "stock_zh_a_hist", "ak.stock_zh_a_hist", "600519", "600519", "date"),
        ("_fetch_stock_data_sina", "stock_zh_a_daily", "ak.stock_zh_a_daily", "605218", "sh605218", "date"),
        ("_fetch_stock_data_tx", "stock_zh_a_hist_tx", "ak.stock_zh_a_hist_tx", "605218", "sh605218", "date"),
        ("_fetch_etf_data", "fund_etf_hist_em", "ak.fund_etf_hist_em", "512400", "512400", "zh"),
        ("_fetch_us_data", "stock_us_daily", "ak.stock_us_daily", "AAPL", "AAPL", "date"),
        ("_fetch_hk_data", "stock_hk_hist", "ak.stock_hk_hist", "HK00700", "00700", "zh"),
    ],
)
def test_all_daily_history_calls_use_timeout_wrapper(
    monkeypatch,
    method_name: str,
    api_name: str,
    call_name: str,
    code: str,
    expected_symbol: str,
    frame_kind: str,
) -> None:
    """Every akshare daily channel must run under the hard timeout.

    The manager-level failover loop has no per-request budget, so a single
    unbounded call here strands the whole chain. ``_fetch_stock_data_em`` was
    the only channel missing the wrapper and this parametrisation is what
    keeps a future one from slipping through the same way.
    """
    captured = {}

    def fake_call(func, *args, timeout=None, call_name="", **kwargs):
        captured["func"] = func
        captured["timeout"] = timeout
        captured["call_name"] = call_name
        captured["kwargs"] = kwargs
        if frame_kind == "zh":
            return pd.DataFrame(
                {
                    "日期": ["2026-05-25"],
                    "开盘": [10.0],
                    "最高": [10.5],
                    "最低": [9.8],
                    "收盘": [10.2],
                    "成交量": [1000],
                    "成交额": [20000],
                }
            )
        return pd.DataFrame(
            {
                "date": ["2026-05-25"],
                "open": [10.0],
                "high": [10.5],
                "low": [9.8],
                "close": [10.2],
                "volume": [1000],
                "amount": [20000],
            }
        )

    fake_api_func = object()
    fake_akshare = SimpleNamespace(**{api_name: fake_api_func})
    monkeypatch.setitem(sys.modules, "akshare", fake_akshare)
    monkeypatch.setattr("data_provider.akshare_fetcher._akshare_call_with_timeout", fake_call)

    fetcher = AkshareFetcher(sleep_min=0, sleep_max=0)
    fetcher._history_call_timeout = 7

    method = getattr(fetcher, method_name)
    method(code, "2026-05-01", "2026-05-25")

    assert captured["func"] is fake_api_func
    assert captured["timeout"] == 7, f"{call_name} must inherit the bounded history timeout"
    assert captured["call_name"] == call_name
    assert captured["kwargs"]["symbol"] == expected_symbol
    assert captured["kwargs"]["adjust"] == "qfq"


@pytest.mark.parametrize(
    ("method_name", "api_name", "call_name", "code"),
    [
        ("_fetch_stock_data_em", "stock_zh_a_hist", "ak.stock_zh_a_hist", "600519"),
        ("_fetch_etf_data", "fund_etf_hist_em", "ak.fund_etf_hist_em", "512400"),
        ("_fetch_us_data", "stock_us_daily", "ak.stock_us_daily", "AAPL"),
        ("_fetch_hk_data", "stock_hk_hist", "ak.stock_hk_hist", "HK00700"),
    ],
)
def test_daily_history_timeout_surfaces_as_timeout(
    monkeypatch,
    method_name: str,
    api_name: str,
    call_name: str,
    code: str,
) -> None:
    """A hung channel must surface as TimeoutError so failover can proceed."""

    def fake_call(func, *args, timeout=None, call_name="", **kwargs):
        raise TimeoutError(f"{call_name} 调用超过 {timeout}s，已放弃等待")

    monkeypatch.setitem(sys.modules, "akshare", SimpleNamespace(**{api_name: object()}))
    monkeypatch.setattr("data_provider.akshare_fetcher._akshare_call_with_timeout", fake_call)

    fetcher = AkshareFetcher(sleep_min=0, sleep_max=0)
    fetcher._history_call_timeout = 7

    with pytest.raises((TimeoutError, DataFetchError)) as excinfo:
        getattr(fetcher, method_name)(code, "2026-05-01", "2026-05-25")

    assert call_name in str(excinfo.value)


def test_stock_data_falls_back_after_em_timeout(monkeypatch) -> None:
    fetcher = AkshareFetcher(sleep_min=0, sleep_max=0)
    tx_df = pd.DataFrame({"日期": ["2026-05-25"], "收盘": [10.2]})

    monkeypatch.setattr(
        fetcher,
        "_fetch_stock_data_em",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("em timeout")),
    )
    monkeypatch.setattr(fetcher, "_fetch_stock_data_sina", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(fetcher, "_fetch_stock_data_tx", lambda *args, **kwargs: tx_df)

    result = fetcher._fetch_stock_data("605218", "2026-05-01", "2026-05-25")

    assert result is tx_df


def test_stock_data_falls_back_after_sina_timeout(monkeypatch) -> None:
    fetcher = AkshareFetcher(sleep_min=0, sleep_max=0)
    tx_df = pd.DataFrame({"日期": ["2026-05-25"], "收盘": [10.2]})

    monkeypatch.setattr(fetcher, "_fetch_stock_data_em", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        fetcher,
        "_fetch_stock_data_sina",
        lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("sina timeout")),
    )
    monkeypatch.setattr(fetcher, "_fetch_stock_data_tx", lambda *args, **kwargs: tx_df)

    result = fetcher._fetch_stock_data("605218", "2026-05-01", "2026-05-25")

    assert result is tx_df
