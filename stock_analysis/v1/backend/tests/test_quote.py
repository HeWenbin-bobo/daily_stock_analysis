"""Regression tests for /api/v1/stocks/{code}/quote.

覆盖 (Executor #51 - 基本契约):
  - FORCE_MOCK=1 时不调用 yfinance, 走 mock 数据。
  - 港股代码前导 0 被保留 ("700" -> response "00700")。
  - 真实行情路径 (FORCE_MOCK=0) yfinance 抛异常时, 响应不泄露原始异常文本。
  - 非法 stock_code -> 400。

注意: NaN volume / DataFrame 列校验 / prev_close=0 等边界由 quote-fix 补充,
本文件只覆盖 Executor 负责的基本安全契约。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import pandas as pd
from fastapi.testclient import TestClient

os.environ.setdefault("FORCE_MOCK", "1")

import app as app_module  # noqa: E402
from middlewares.rate_limit import rate_limiter  # noqa: E402


@pytest.fixture
def client():
    rate_limiter.reset()
    return TestClient(app_module.app)


def _quote_frame(
    *,
    closes: tuple[float, ...] = (100.0, 105.0),
    volumes: tuple[float, ...] = (1_000.0, 2_000.0),
) -> pd.DataFrame:
    """构造带 DatetimeIndex 的最小合法 yfinance 行情。"""
    periods = len(closes)
    return pd.DataFrame(
        {
            "Open": [value - 1 for value in closes],
            "High": [value + 2 for value in closes],
            "Low": [value - 2 for value in closes],
            "Close": list(closes),
            "Volume": list(volumes),
        },
        index=pd.date_range("2026-01-01", periods=periods, freq="D"),
    )


# ---------------------------------------------------------------------------
# FORCE_MOCK=1: 不调用 yfinance, 走 mock
# ---------------------------------------------------------------------------

class TestQuoteForceMockPath:
    def test_cn_quote_returns_200_without_calling_yfinance(self, client, monkeypatch):
        """FORCE_MOCK=1 时, yfinance.download 不应被调用。"""
        from routers import stocks as stocks_router

        called = {"n": 0}

        def fake_yf_download(*args, **kwargs):
            called["n"] += 1
            raise AssertionError("yfinance.download should not be called when FORCE_MOCK=1")

        monkeypatch.setattr(stocks_router.yf, "download", fake_yf_download)

        r = client.get("/api/v1/stocks/600519/quote")
        assert r.status_code == 200
        assert called["n"] == 0
        data = r.json()
        assert data["stock_code"] == "600519"
        assert data["market"] == "cn"
        # 必填字段
        for field in (
            "stock_code", "stock_name", "market",
            "current_price", "change", "change_percent",
            "open", "high", "low", "prev_close", "volume", "update_time",
        ):
            assert field in data

    def test_us_quote_returns_200_without_calling_yfinance(self, client, monkeypatch):
        from routers import stocks as stocks_router

        called = {"n": 0}

        def fake_yf_download(*args, **kwargs):
            called["n"] += 1
            raise AssertionError("yfinance must not be called under FORCE_MOCK")

        monkeypatch.setattr(stocks_router.yf, "download", fake_yf_download)

        r = client.get("/api/v1/stocks/AAPL/quote")
        assert r.status_code == 200
        assert called["n"] == 0
        assert r.json()["market"] == "us"

    def test_hk_quote_returns_200_without_calling_yfinance(self, client, monkeypatch):
        from routers import stocks as stocks_router

        called = {"n": 0}

        def fake_yf_download(*args, **kwargs):
            called["n"] += 1
            raise AssertionError("yfinance must not be called under FORCE_MOCK")

        monkeypatch.setattr(stocks_router.yf, "download", fake_yf_download)

        r = client.get("/api/v1/stocks/00700/quote")
        assert r.status_code == 200
        assert called["n"] == 0
        assert r.json()["market"] == "hk"

    def test_quote_values_are_built_from_history_data(self, client, monkeypatch):
        """mock quote 与复用的 history K 线保持同一套价格语义。"""
        from routers import stocks as stocks_router
        from schemas.stocks import KLineData

        history = [
            KLineData(
                date="2026-01-01",
                open=98.0,
                high=102.0,
                low=97.0,
                close=100.0,
                volume=1_000.0,
            ),
            KLineData(
                date="2026-01-02",
                open=103.0,
                high=108.0,
                low=101.0,
                close=105.0,
                volume=2_000.0,
            ),
        ]
        calls = []

        def history_data(stock_code, period, days):
            calls.append((stock_code, period, days))
            return history, "mock", "us", "Apple Inc."

        monkeypatch.setenv("FORCE_MOCK", "1")
        monkeypatch.setattr(stocks_router, "fetch_history", history_data)
        monkeypatch.setattr(
            stocks_router.yf,
            "download",
            lambda *args, **kwargs: pytest.fail("FORCE_MOCK must not call yfinance"),
        )

        response = client.get("/api/v1/stocks/AAPL/quote")

        assert response.status_code == 200
        assert calls == [("AAPL", "daily", 30)]
        data = response.json()
        assert data["current_price"] == 105.0
        assert data["prev_close"] == 100.0
        assert data["change"] == 5.0
        assert data["change_percent"] == 5.0
        assert data["volume"] == 2_000.0


# ---------------------------------------------------------------------------
# 港股前导 0 保留
# ---------------------------------------------------------------------------

class TestHKLeadingZeroPreservation:
    def test_hk_short_code_pads_to_5_digits(self, client):
        """输入 "700" 时, 响应 stock_code 必须是 "00700" (前导 0 不丢)。"""
        r = client.get("/api/v1/stocks/700/quote")
        assert r.status_code == 200
        data = r.json()
        assert data["stock_code"] == "00700"
        assert data["market"] == "hk"

    def test_hk_5digit_code_unchanged(self, client):
        """5 位 HK 代码不应被改动。"""
        r = client.get("/api/v1/stocks/00700/quote")
        assert r.status_code == 200
        assert r.json()["stock_code"] == "00700"

    def test_hk_with_hk_suffix_pads(self, client):
        r = client.get("/api/v1/stocks/HK700/quote")
        assert r.status_code == 200
        assert r.json()["stock_code"] == "00700"


# ---------------------------------------------------------------------------
# 真实行情路径 (FORCE_MOCK=0) - 异常不泄露
# ---------------------------------------------------------------------------

class TestQuoteYFinanceErrorMasking:
    def test_yf_exception_returns_generic_500(self, client, monkeypatch):
        """yfinance 抛异常时, 响应 detail 必须是通用文案, 不能含原始异常文本。"""
        monkeypatch.setenv("FORCE_MOCK", "0")
        try:
            from routers import stocks as stocks_router

            def boom(*args, **kwargs):
                raise RuntimeError(
                    "YFRateLimitError('Too Many Requests. Rate limited.') -- internal secret token=abc123"
                )

            monkeypatch.setattr(stocks_router.yf, "download", boom)

            r = client.get("/api/v1/stocks/AAPL/quote")
            assert r.status_code == 500
            detail = r.json()["detail"]
            # 不应包含原始异常类名 / 原始消息
            assert "YFRateLimitError" not in detail
            assert "abc123" not in detail
            assert "secret token" not in detail.lower()
            # 必须是通用文案
            assert detail == "获取行情失败"
        finally:
            monkeypatch.setenv("FORCE_MOCK", "1")

    def test_yf_empty_df_returns_404(self, client, monkeypatch):
        """yfinance 返回空 DataFrame -> 404, 不是 500。"""
        import pandas as pd
        monkeypatch.setenv("FORCE_MOCK", "0")
        try:
            from routers import stocks as stocks_router

            def empty(*args, **kwargs):
                return pd.DataFrame()

            monkeypatch.setattr(stocks_router.yf, "download", empty)

            r = client.get("/api/v1/stocks/AAPL/quote")
            assert r.status_code == 404
        finally:
            monkeypatch.setenv("FORCE_MOCK", "1")


# ---------------------------------------------------------------------------
# yfinance 数据格式与数值边界
# ---------------------------------------------------------------------------

class TestQuoteYFinanceDataValidation:
    def test_hk_ticker_preserves_provider_leading_zero(self, client, monkeypatch):
        """内部 5 位港股代码只去掉一个前导 0, 与 data_provider 保持一致。"""
        from routers import stocks as stocks_router

        requested = {}

        def download(ticker, **kwargs):
            requested["ticker"] = ticker
            return _quote_frame()

        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(stocks_router.yf, "download", download)

        response = client.get("/api/v1/stocks/00700/quote")

        assert response.status_code == 200
        assert requested["ticker"] == "0700.HK"
        assert response.json()["stock_code"] == "00700"

    def test_multiindex_columns_are_normalized(self, client, monkeypatch):
        """yfinance 单 ticker MultiIndex 列应可安全展开。"""
        from routers import stocks as stocks_router

        frame = _quote_frame()
        frame.columns = pd.MultiIndex.from_tuples(
            [(column, "AAPL") for column in frame.columns]
        )
        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(stocks_router.yf, "download", lambda *args, **kwargs: frame)

        response = client.get("/api/v1/stocks/AAPL/quote")

        assert response.status_code == 200
        assert response.json()["current_price"] == 105.0

    def test_non_dataframe_returns_safe_provider_error(self, client, monkeypatch):
        from routers import stocks as stocks_router

        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(
            stocks_router.yf, "download", lambda *args, **kwargs: {"Close": [1.0]}
        )

        response = client.get("/api/v1/stocks/AAPL/quote")

        assert response.status_code == 502
        assert response.json()["detail"] == "行情数据格式异常"

    def test_missing_required_column_returns_safe_provider_error(self, client, monkeypatch):
        from routers import stocks as stocks_router

        frame = _quote_frame().drop(columns=["High"])
        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(stocks_router.yf, "download", lambda *args, **kwargs: frame)

        response = client.get("/api/v1/stocks/AAPL/quote")

        assert response.status_code == 502
        assert response.json()["detail"] == "行情数据格式异常"
        assert "high" not in response.json()["detail"].lower()

    def test_range_index_returns_safe_provider_error(self, client, monkeypatch):
        from routers import stocks as stocks_router

        frame = _quote_frame().reset_index(drop=True)
        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(stocks_router.yf, "download", lambda *args, **kwargs: frame)

        response = client.get("/api/v1/stocks/AAPL/quote")

        assert response.status_code == 502
        assert response.json()["detail"] == "行情数据格式异常"

    def test_multiindex_index_returns_safe_provider_error(self, client, monkeypatch):
        from routers import stocks as stocks_router

        frame = _quote_frame()
        frame.index = pd.MultiIndex.from_arrays(
            [frame.index, ["AAPL"] * len(frame)], names=["Date", "Ticker"]
        )
        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(stocks_router.yf, "download", lambda *args, **kwargs: frame)

        response = client.get("/api/v1/stocks/AAPL/quote")

        assert response.status_code == 502
        assert response.json()["detail"] == "行情数据格式异常"

    def test_nan_volume_is_normalized_to_zero(self, client, monkeypatch):
        from routers import stocks as stocks_router

        frame = _quote_frame(volumes=(1_000.0, float("nan")))
        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(stocks_router.yf, "download", lambda *args, **kwargs: frame)

        response = client.get("/api/v1/stocks/AAPL/quote")

        assert response.status_code == 200
        assert response.json()["volume"] == 0.0

    def test_single_row_uses_current_close_as_safe_previous_close(self, client, monkeypatch):
        from routers import stocks as stocks_router

        frame = _quote_frame(closes=(105.0,), volumes=(2_000.0,))
        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(stocks_router.yf, "download", lambda *args, **kwargs: frame)

        response = client.get("/api/v1/stocks/AAPL/quote")

        assert response.status_code == 200
        data = response.json()
        assert data["prev_close"] == 105.0
        assert data["change"] == 0.0
        assert data["change_percent"] == 0.0

    def test_zero_previous_close_has_zero_percent_without_schema_change(
        self, client, monkeypatch
    ):
        from routers import stocks as stocks_router

        frame = _quote_frame(closes=(0.0, 10.0))
        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(stocks_router.yf, "download", lambda *args, **kwargs: frame)

        response = client.get("/api/v1/stocks/AAPL/quote")

        assert response.status_code == 200
        data = response.json()
        assert data["prev_close"] == 0.0
        assert data["change"] == 10.0
        assert data["change_percent"] == 0.0


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

class TestQuoteInputValidation:
    def test_invalid_stock_code_returns_400(self, client):
        r = client.get("/api/v1/stocks/600519%3BDROP/quote")
        assert r.status_code == 400

    def test_empty_stock_code_returns_404_or_405(self, client):
        # /quote 路径必填, 空路径 -> 404 (routing)
        r = client.get("/api/v1/stocks//quote")
        assert r.status_code in (404, 405)