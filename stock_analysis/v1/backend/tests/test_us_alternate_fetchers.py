"""us_alternate_fetchers 测试:Finnhub + AlphaVantage。

iter 158: 借鉴 v2 daily_stock_analysis_v2/data_provider/{finnhub,alphavantage}_fetcher.py
的实现, 用 requests 直接调 REST API。这些 fetcher 在缺失 API key 时返回 None,
不抛异常, 让调用方自然跳过该节点。

测试要点: fetch_us_* 在函数体内 import requests, 所以 patch 目标应是顶层 ``requests.get``
而不是 ``services.us_alternate_fetchers.requests``。
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from services.us_alternate_fetchers import (
    fetch_us_alphavantage,
    fetch_us_finnhub,
    _normalize_us_df,
)


class TestFinnhubFetcher:
    """fetch_us_finnhub: 缺失 key / 非日 K / 请求失败 / 成功 四路径。"""

    def teardown_method(self):
        os.environ.pop("FINNHUB_API_KEY", None)

    def test_returns_none_when_api_key_missing(self):
        """无 FINNHUB_API_KEY → 跳过, 不抛异常。"""
        os.environ.pop("FINNHUB_API_KEY", None)
        result = fetch_us_finnhub("AAPL", "daily", 60)
        assert result is None

    def test_returns_none_for_non_daily_period(self):
        """仅支持日 K, 周/月直接跳过。"""
        os.environ["FINNHUB_API_KEY"] = "test-fake-key"
        assert fetch_us_finnhub("AAPL", "weekly", 60) is None
        assert fetch_us_finnhub("AAPL", "monthly", 60) is None

    def test_returns_data_on_success(self):
        """请求成功 → 返回归一化后的 KLineData 列表。"""
        os.environ["FINNHUB_API_KEY"] = "test-fake-key"

        # Mock 整个 requests.get: 返回标准 Finnhub candle 响应
        import time
        base_ts = int(time.time()) - 5 * 86400
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={
            "s": "ok",
            "c": [184.0, 185.0, 186.0, 187.0, 188.0],
            "h": [185.0, 186.0, 187.0, 188.0, 189.0],
            "l": [183.0, 184.0, 185.0, 186.0, 187.0],
            "o": [183.5, 184.5, 185.5, 186.5, 187.5],
            "v": [1000000] * 5,
            "t": [base_ts + i * 86400 for i in range(5)],
        })

        with patch("requests.get", return_value=mock_resp), \
             patch("time.sleep"):
            result = fetch_us_finnhub("AAPL", "daily", 5)

        assert result is not None
        assert len(result) == 5
        assert result[0].close == 184.0
        assert result[0].open == 183.5
        assert result[0].volume == 1000000
        # date 应该是 YYYY-MM-DD 格式
        assert len(result[0].date) == 10

    def test_returns_none_on_http_error(self):
        """HTTP 错误 → 返回 None, 不抛。"""
        os.environ["FINNHUB_API_KEY"] = "test-fake-key"

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock(side_effect=Exception("HTTP 500"))

        with patch("requests.get", return_value=mock_resp), \
             patch("time.sleep"):
            result = fetch_us_finnhub("AAPL", "daily", 60)

        assert result is None

    def test_returns_none_on_empty_response(self):
        """Finnhub 返回 s='no_data' 或 c=[] → 返回 None。"""
        os.environ["FINNHUB_API_KEY"] = "test-fake-key"

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"s": "no_data"})

        with patch("requests.get", return_value=mock_resp), \
             patch("time.sleep"):
            result = fetch_us_finnhub("AAPL", "daily", 60)

        assert result is None


class TestAlphaVantageFetcher:
    """fetch_us_alphavantage: 缺失 key / 限流 / 成功路径。"""

    def teardown_method(self):
        os.environ.pop("ALPHAVANTAGE_API_KEY", None)

    def test_returns_none_when_api_key_missing(self):
        assert fetch_us_alphavantage("AAPL", "daily", 60) is None

    def test_returns_none_for_non_daily_period(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-fake-key"
        assert fetch_us_alphavantage("AAPL", "weekly", 60) is None

    def test_returns_data_on_success(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-fake-key"

        from datetime import date, timedelta
        # 用 today 附近的日期, 否则 cutoff_date 过滤会清空所有数据
        today = date.today()
        d1 = (today - timedelta(days=2)).isoformat()
        d2 = (today - timedelta(days=1)).isoformat()
        d3 = today.isoformat()

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        # AlphaVantage 返回 Time Series (Daily), 最新在前
        mock_resp.json = MagicMock(return_value={
            "Meta Data": {"1. Information": "Daily Prices"},
            "Time Series (Daily)": {
                d3: {
                    "1. open": "187.5", "2. high": "189.0", "3. low": "187.0",
                    "4. close": "188.0", "5. volume": "1000000",
                },
                d2: {
                    "1. open": "186.5", "2. high": "188.0", "3. low": "186.0",
                    "4. close": "187.0", "5. volume": "900000",
                },
                d1: {
                    "1. open": "185.5", "2. high": "187.0", "3. low": "185.0",
                    "4. close": "186.0", "5. volume": "800000",
                },
            },
        })

        with patch("requests.get", return_value=mock_resp), \
             patch("time.sleep"):
            result = fetch_us_alphavantage("AAPL", "daily", 60)

        assert result is not None
        assert len(result) == 3
        # 升序排: d1 在前
        assert result[0].date == d1
        assert result[0].close == 186.0
        assert result[-1].date == d3
        assert result[-1].close == 188.0

    def test_returns_none_on_rate_limit(self):
        """AlphaVantage 限流 (Note 字段) → 返回 None, 不抛。"""
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-fake-key"

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={
            "Note": "Thank you for using Alpha Vantage! Our standard API call frequency is 5 calls per minute and 25 calls per day.",
        })

        with patch("requests.get", return_value=mock_resp), \
             patch("time.sleep"):
            result = fetch_us_alphavantage("AAPL", "daily", 60)

        assert result is None

    def test_returns_none_on_api_error(self):
        os.environ["ALPHAVANTAGE_API_KEY"] = "test-fake-key"

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={
            "Error Message": "Invalid API call. Please retry or visit the documentation.",
        })

        with patch("requests.get", return_value=mock_resp), \
             patch("time.sleep"):
            result = fetch_us_alphavantage("AAPL", "daily", 60)

        assert result is None


class TestNormalizeUsDf:
    """_normalize_us_df 内部 helper 健壮性。"""

    def test_empty_df_returns_empty_list(self):
        import pandas as pd
        result = _normalize_us_df(pd.DataFrame(), "test", ["open", "high", "low", "close", "volume"])
        assert result == []

    def test_missing_required_column_raises(self):
        import pandas as pd
        df = pd.DataFrame({"date": ["2026-01-01"], "open": [1.0]})
        with pytest.raises(ValueError, match="缺少"):
            _normalize_us_df(df, "test", ["open", "high", "low", "close", "volume"])

    def test_valid_data_returns_klinedata_list(self):
        import pandas as pd
        from schemas.stocks import KLineData
        df = pd.DataFrame({
            "date": ["2026-01-01", "2026-01-02"],
            "open": [100.0, 101.0],
            "high": [105.0, 106.0],
            "low": [99.0, 100.0],
            "close": [104.0, 105.0],
            "volume": [1_000_000.0, 1_100_000.0],
        })
        result = _normalize_us_df(df, "test", ["open", "high", "low", "close", "volume"])
        assert len(result) == 2
        assert all(isinstance(r, KLineData) for r in result)
        assert result[0].close == 104.0
        assert result[0].amount == 0.0  # 默认 0
        assert result[0].change_percent == 0.0  # 默认 0
