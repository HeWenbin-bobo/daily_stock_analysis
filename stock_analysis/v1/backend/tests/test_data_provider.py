"""data_provider 测试: detect_market, guess_name, search_stocks。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from services.data_provider import (
    detect_market,
    fetch_history,
    guess_name,
    normalize_code,
    search_stocks,
    _hk_yfinance_ticker,
    _is_transient_upstream_error,
    _with_upstream_retry,
    SOURCE_MOCK,
    SOURCE_MOCK_FALLBACK,
    SOURCE_YFINANCE,
    SOURCE_AKSHARE,
    FALLBACK_CACHE_TTL_SECONDS,
)


class TestDetectMarket:
    """detect_market 市场识别。"""

    @pytest.mark.parametrize("code,expected", [
        ("600519", "cn"),       # A股 6位
        ("300750", "cn"),       # 创业板
        ("002594", "cn"),       # 中小板
        ("00700", "hk"),        # 港股 5位
        ("0700", "hk"),         # 港股 4位 (前导0)
        ("700.HK", "hk"),       # 港股带后缀
        ("HK00700", "hk"),      # 带 HK 前缀
        ("hk700", "hk"),        # 小写前缀 + 短码
        ("AAPL", "us"),         # 美股
        ("TSLA", "us"),
        ("BRK-B", "us"),
        ("600519.SS", "cn"),    # yfinance 后缀
        ("000001.SZ", "cn"),
        ("123456.HK", "hk"),    # 显式港股后缀优先于 6 位长度
        ("00700.SS", "cn"),     # 显式 A 股后缀优先于 5 位长度
        ("600519.HK", "hk"),    # 显式港股后缀优先于 A 股长度
        ("HK600519", "hk"),     # HK 前缀优先于 6 位长度
    ])
    def test_detect_market(self, code: str, expected: str):
        assert detect_market(code) == expected


class TestNormalizeCode:
    """normalize_code 提取纯代码。"""

    def test_strip_yfinance_suffix(self):
        assert normalize_code("600519.SS", "cn") == "600519"
        assert normalize_code("000001.SZ", "cn") == "000001"

    def test_strip_hk_suffix(self):
        assert normalize_code("0700.HK", "hk") == "0700"

    def test_strip_hk_prefix(self):
        assert normalize_code("HK00700", "hk") == "00700"
        assert normalize_code("hk700", "hk") == "700"

    def test_passthrough(self):
        assert normalize_code("AAPL", "us") == "AAPL"


class TestGuessName:
    """guess_name 已知股票返回真名, 未知标记 (未匹配)。"""

    def test_known_a_share(self):
        assert guess_name("600519", "cn") == "贵州茅台"
        assert guess_name("300750", "cn") == "宁德时代"

    def test_known_hk(self):
        assert guess_name("00700", "hk") == "腾讯控股"
        assert guess_name("0700", "hk") == "腾讯控股"  # 不带前导 0 也行

    def test_known_us(self):
        assert guess_name("AAPL", "us") == "Apple Inc."
        assert guess_name("TSLA", "us") == "Tesla"

    def test_yfinance_suffix_stripped(self):
        """iter 63: guess_name 应在 A股代码带 .SS/.SZ 后缀时仍正确识别。
        (yfinance API 返回的代码有时带交易所后缀; 之前只测了裸代码。)"""
        assert guess_name("600519.SS", "cn") == "贵州茅台"
        assert guess_name("000001.SZ", "cn") == "平安银行"

    def test_unknown_marked(self):
        """未知股票应返回 '(未匹配)' 后缀, 让前端知道这是 fallback。"""
        result = guess_name("UNKNOWN123", "cn")
        assert "未匹配" in result


class TestSearchStocks:
    """search_stocks 搜索功能。"""

    def test_empty_query_returns_empty(self):
        assert search_stocks("") == []
        assert search_stocks("   ") == []

    def test_search_by_code_prefix(self):
        results = search_stocks("6005")
        assert any(r["code"] == "600519" for r in results)

    def test_search_by_chinese_name(self):
        results = search_stocks("茅台")
        assert len(results) >= 1
        assert results[0]["code"] == "600519"
        assert results[0]["market"] == "cn"

    def test_search_by_category(self):
        results = search_stocks("银行")
        # 至少有 A股银行 + 港股银行
        codes = [r["code"] for r in results]
        assert any(c.startswith("60") or c.startswith("00") for c in codes)

    def test_search_us_ticker(self):
        results = search_stocks("AAPL")
        assert any(r["code"] == "AAPL" and r["market"] == "us" for r in results)

    def test_search_case_insensitive(self):
        results_lower = search_stocks("aapl")
        results_upper = search_stocks("AAPL")
        # 都应该找到 AAPL
        assert any(r["code"] == "AAPL" for r in results_lower)
        assert any(r["code"] == "AAPL" for r in results_upper)

    def test_search_limit(self):
        results = search_stocks("")  # 空 → 空
        assert results == []
        results = search_stocks("a")  # 'a' 可能匹配多个, 应被 limit
        assert len(results) <= 30

    def test_search_no_results(self):
        results = search_stocks("xyzxyz_no_match_string")
        assert results == []


class TestHkYfinanceTicker:
    """_hk_yfinance_ticker 保留单个前导零。"""

    @pytest.mark.parametrize("code,expected", [
        ("00700", "0700.HK"),
        ("00005", "0005.HK"),
        ("09988", "9988.HK"),
        ("00001", "0001.HK"),
    ])
    def test_strips_single_leading_zero(self, code: str, expected: str):
        assert _hk_yfinance_ticker(code) == expected

    def test_passthrough_without_leading_zero(self):
        # 5 位但首位非 0 → 直接追加 .HK, 不做剥离
        assert _hk_yfinance_ticker("70000") == "70000.HK"

    def test_non_five_digit_unchanged(self):
        # 非 5 位保持原样追加 .HK (覆盖少数 7 位输入场景)
        assert _hk_yfinance_ticker("0000700") == "0000700.HK"


class TestTransientUpstreamError:
    """_is_transient_upstream_error 判断瞬时错误。"""

    def test_yf_rate_limit_is_transient(self):
        class YFRateLimitError(Exception):
            pass

        assert _is_transient_upstream_error(YFRateLimitError("rate")) is True

    def test_http_error_is_transient(self):
        class HTTPError(Exception):
            pass

        assert _is_transient_upstream_error(HTTPError("500")) is True

    def test_value_error_is_not_transient(self):
        assert _is_transient_upstream_error(ValueError("nope")) is False

    def test_generic_exception_is_not_transient(self):
        assert _is_transient_upstream_error(RuntimeError("boom")) is False

    def test_requests_module_class_is_transient(self):
        # yfinance 内部用 requests, 限流包成 requests.exceptions.* 时也应当重试
        class ReqError(Exception):
            __module__ = "requests.exceptions"

        assert _is_transient_upstream_error(ReqError("rate")) is True


class TestUpstreamRetry:
    """_with_upstream_retry 1 次重试 + 立即抛出非瞬时错误。"""

    def test_first_attempt_succeeds(self, monkeypatch):
        # 第一次就成功, 不会重试
        calls = {"n": 0}
        def fn():
            calls["n"] += 1
            return "ok"

        monkeypatch.setattr("time.sleep", lambda s: None)  # 避免 sleep 浪费时间
        result = _with_upstream_retry(fn)
        assert result == "ok"
        assert calls["n"] == 1

    def test_transient_error_retries_and_succeeds(self, monkeypatch):
        # 模拟限流 → 重试 → 成功
        class YFRateLimitError(Exception):
            pass

        calls = {"n": 0}
        def fn():
            calls["n"] += 1
            if calls["n"] < 2:
                raise YFRateLimitError("rate")
            return "ok-after-retry"

        monkeypatch.setattr("time.sleep", lambda s: None)
        result = _with_upstream_retry(fn)
        assert result == "ok-after-retry"
        assert calls["n"] == 2  # 失败 1 次 + 成功 1 次 = 2 次

    def test_transient_exhausted_raises(self, monkeypatch):
        # 2 次都失败 → 抛最后异常
        class YFRateLimitError(Exception):
            pass

        def fn():
            raise YFRateLimitError("always rate")

        monkeypatch.setattr("time.sleep", lambda s: None)
        with pytest.raises(YFRateLimitError):
            _with_upstream_retry(fn)

    def test_non_transient_does_not_retry(self, monkeypatch):
        # ValueError 不算瞬时 → 立即抛
        calls = {"n": 0}
        def fn():
            calls["n"] += 1
            raise ValueError("bad input")

        monkeypatch.setattr("time.sleep", lambda s: None)
        with pytest.raises(ValueError):
            _with_upstream_retry(fn)
        assert calls["n"] == 1  # 只调用 1 次


class TestFetchHistoryFallbackSemantics:
    """fetch_history 在 yfinance 失败时返回 mock_fallback, 显式 mock 时返回 mock。

    iter 157: 之前所有失败路径都返回 "mock", 5min 缓存塞假数据, 用户以为 AAPL 是 180。
    现在区分: 显式 FORCE_MOCK=1 → "mock"; 隐式 fallback → "mock_fallback"。
    """

    def teardown_method(self):
        """清缓存 + 还原 FORCE_MOCK=1, 避免污染其它测试。"""
        from services.cache import kline_cache
        kline_cache.clear()
        import os
        os.environ["FORCE_MOCK"] = "1"

    def test_explicit_force_mock_returns_source_mock(self, monkeypatch):
        """显式 FORCE_MOCK=1 时 source='mock' (用户主动选择的 mock)。"""
        monkeypatch.setenv("FORCE_MOCK", "1")
        from services.cache import kline_cache
        kline_cache.clear()
        data, source, market, name = fetch_history("AAPL", "daily", 30)
        assert source == SOURCE_MOCK
        assert market == "us"
        assert name == "Apple Inc."
        assert len(data) > 0

    def test_yfinance_429_returns_mock_fallback(self, monkeypatch):
        """yfinance 抛 YFRateLimitError → source='mock_fallback'。

        这是用户报告的 AAPL bug 的根因修复点: 之前 429 时静默返回 'mock' 但
        5min 缓存里塞了假数据, 用户看到 AAPL 价不对还以为是应用 bug。
        """
        monkeypatch.setenv("FORCE_MOCK", "0")
        from services.cache import kline_cache
        kline_cache.clear()
        monkeypatch.setattr("services.data_provider._fetch_us_akshare", lambda *a, **k: None)  # iter 160: akshare 是美股首选, 测 fallback 链时禁用


        class YFRateLimitError(Exception):
            pass

        # monkeypatch 整个 yfinance 模块, 让 Ticker().history() 抛限流
        import sys
        from unittest.mock import MagicMock
        mock_yf = MagicMock()
        mock_yf.Ticker = MagicMock(side_effect=lambda _t: MagicMock(
            history=MagicMock(side_effect=YFRateLimitError("Too Many Requests")),
        ))

        # 用 sys.modules 替换 yfinance — fetch_us 用的是 yf.Ticker
        # 先清掉缓存的 yfinance 引用避免冲突
        for mod in list(sys.modules.keys()):
            if mod == "yfinance":
                del sys.modules[mod]
        sys.modules["yfinance"] = mock_yf

        data, source, market, name = fetch_history("AAPL", "daily", 30)
        assert source == SOURCE_MOCK_FALLBACK
        assert market == "us"
        assert name == "Apple Inc."
        # fallback 的 mock 数据仍然有合理形状
        assert len(data) > 0
        assert all(bar.close > 0 for bar in data)

    def test_yfinance_generic_exception_returns_mock_fallback(self, monkeypatch):
        """yfinance 抛任何非瞬时错误也走 fallback。"""
        monkeypatch.setenv("FORCE_MOCK", "0")
        from services.cache import kline_cache
        kline_cache.clear()
        monkeypatch.setattr("services.data_provider._fetch_us_akshare", lambda *a, **k: None)  # iter 160: akshare 是美股首选, 测 fallback 链时禁用


        from unittest.mock import MagicMock
        import sys
        mock_yf = MagicMock()
        mock_yf.Ticker = MagicMock(side_effect=lambda _t: MagicMock(
            history=MagicMock(side_effect=RuntimeError("network unreachable")),
        ))
        for mod in list(sys.modules.keys()):
            if mod == "yfinance":
                del sys.modules[mod]
        sys.modules["yfinance"] = mock_yf

        data, source, market, name = fetch_history("AAPL", "daily", 30)
        assert source == SOURCE_MOCK_FALLBACK
        assert market == "us"
        assert len(data) > 0

    def test_yfinance_retry_succeeds_on_second_attempt(self, monkeypatch):
        """yfinance 第 1 次 429, 第 2 次成功 → source='yfinance' (而非 fallback)。

        iter 158: 重试次数从 2 升到 3 (v2 tenacity 模式), 测试覆盖到 2 次重试就够,
        因为 retry 逻辑 (max_attempts=N) 与 2/3 兼容。
        """
        monkeypatch.setenv("FORCE_MOCK", "0")
        from services.cache import kline_cache
        kline_cache.clear()
        monkeypatch.setattr("services.data_provider._fetch_us_akshare", lambda *a, **k: None)  # iter 160: akshare 是美股首选, 测 fallback 链时禁用


        import pandas as pd
        dates = pd.date_range("2026-01-01", periods=10, freq="D")
        success_df = pd.DataFrame({
            "Date": dates,
            "Open": [180.0 + i for i in range(10)],
            "High": [185.0 + i for i in range(10)],
            "Low": [179.0 + i for i in range(10)],
            "Close": [184.0 + i for i in range(10)],
            "Volume": [5e7] * 10,
        }).set_index("Date")

        class YFRateLimitError(Exception):
            pass

        from unittest.mock import MagicMock
        import sys
        call_count = {"n": 0}
        def fake_history(**kwargs):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise YFRateLimitError("rate")
            return success_df

        mock_yf = MagicMock()
        mock_yf.Ticker = MagicMock(side_effect=lambda _t: MagicMock(history=fake_history))
        for mod in list(sys.modules.keys()):
            if mod == "yfinance":
                del sys.modules[mod]
        sys.modules["yfinance"] = mock_yf

        monkeypatch.setattr("time.sleep", lambda s: None)  # 加速测试

        data, source, market, name = fetch_history("AAPL", "daily", 10)
        assert source == SOURCE_YFINANCE
        assert market == "us"
        assert call_count["n"] == 2  # 1 失败 + 1 成功
        assert data[0].close == 184.0  # 来自真实 DataFrame

    def test_yfinance_retries_three_times_before_failing(self, monkeypatch):
        """iter 158: yfinance 连续 3 次都失败 → source='mock_fallback', 调用次数 = 3。

        v2 的 tenacity 模式 stop=3 attempts, v1 现在也升级到 3 (从原来的 1)。
        """
        monkeypatch.setenv("FORCE_MOCK", "0")
        from services.cache import kline_cache
        kline_cache.clear()
        monkeypatch.setattr("services.data_provider._fetch_us_akshare", lambda *a, **k: None)  # iter 160: akshare 是美股首选, 测 fallback 链时禁用


        class YFRateLimitError(Exception):
            pass

        from unittest.mock import MagicMock
        import sys
        call_count = {"n": 0}
        def fake_history(**kwargs):
            call_count["n"] += 1
            raise YFRateLimitError("always rate")

        mock_yf = MagicMock()
        mock_yf.Ticker = MagicMock(side_effect=lambda _t: MagicMock(history=fake_history))
        for mod in list(sys.modules.keys()):
            if mod == "yfinance":
                del sys.modules[mod]
        sys.modules["yfinance"] = mock_yf

        monkeypatch.setattr("time.sleep", lambda s: None)
        # 让 fetch_us_finnhub/alphavantage 也走 None 路径, 避免新 fallback 链干扰
        monkeypatch.setattr(
            "services.us_alternate_fetchers.fetch_us_finnhub",
            lambda *a, **k: None,
            raising=False,
        )
        monkeypatch.setattr(
            "services.us_alternate_fetchers.fetch_us_alphavantage",
            lambda *a, **k: None,
            raising=False,
        )

        data, source, market, name = fetch_history("AAPL", "daily", 10)
        assert source == SOURCE_MOCK_FALLBACK
        assert call_count["n"] == 3  # iter 158: 升级到 3 attempts

    def test_us_falls_back_to_finnhub_when_yfinance_fails(self, monkeypatch):
        """iter 158: yfinance 失败 + FINNHUB_API_KEY 配置 + fetch_us_finnhub 返回数据
        → source='finnhub', 用户拿到真数据而非 mock。"""
        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setenv("FINNHUB_API_KEY", "test-key-fake")
        from services.cache import kline_cache
        kline_cache.clear()
        monkeypatch.setattr("services.data_provider._fetch_us_akshare", lambda *a, **k: None)  # iter 160: akshare 是美股首选, 测 fallback 链时禁用


        class YFRateLimitError(Exception):
            pass

        from unittest.mock import MagicMock
        import sys
        # yfinance 一直失败
        mock_yf = MagicMock()
        mock_yf.Ticker = MagicMock(side_effect=lambda _t: MagicMock(
            history=MagicMock(side_effect=YFRateLimitError("rate")),
        ))
        for mod in list(sys.modules.keys()):
            if mod == "yfinance":
                del sys.modules[mod]
        sys.modules["yfinance"] = mock_yf

        # Finnhub 返回真实数据
        from schemas.stocks import KLineData
        from datetime import date, timedelta
        bars = [
            KLineData(
                date=(date(2026, 1, 1) + timedelta(days=i)).isoformat(),
                open=180.0 + i, high=185.0 + i, low=179.0 + i, close=184.0 + i,
                volume=5e7, amount=0, change_percent=0,
            )
            for i in range(10)
        ]
        monkeypatch.setattr(
            "services.us_alternate_fetchers.fetch_us_finnhub",
            lambda *a, **k: bars,
            raising=False,
        )
        monkeypatch.setattr("time.sleep", lambda s: None)

        from services.data_provider import SOURCE_FINNHUB
        data, source, market, name = fetch_history("AAPL", "daily", 10)
        assert source == SOURCE_FINNHUB
        assert data[0].close == 184.0
        assert market == "us"
        assert name == "Apple Inc."

    def test_fallback_uses_short_cache_ttl(self, monkeypatch):
        """mock_fallback 路径的 cache TTL 是 30s, 而非默认 5min。"""
        monkeypatch.setenv("FORCE_MOCK", "0")
        from services.cache import kline_cache
        kline_cache.clear()
        monkeypatch.setattr("services.data_provider._fetch_us_akshare", lambda *a, **k: None)  # iter 160: akshare 是美股首选, 测 fallback 链时禁用


        class YFRateLimitError(Exception):
            pass

        from unittest.mock import MagicMock
        import sys
        mock_yf = MagicMock()
        mock_yf.Ticker = MagicMock(side_effect=lambda _t: MagicMock(
            history=MagicMock(side_effect=YFRateLimitError("rate")),
        ))
        for mod in list(sys.modules.keys()):
            if mod == "yfinance":
                del sys.modules[mod]
        sys.modules["yfinance"] = mock_yf

        data, source, market, name = fetch_history("AAPL", "daily", 30)
        assert source == SOURCE_MOCK_FALLBACK

        # 检查缓存里的值 TTL 是 30s, 不是 300s
        import time
        cached = kline_cache.get("AAPL|daily|days=30")
        assert cached is not None
        # 通过 stats 看 size > 0, 然后用 __dict__ 偷看 expires_at
        # TTLCache._data 是 OrderedDict; key 是 cache_key
        cache_key = "AAPL|daily|days=30"
        expires_at = kline_cache._data[cache_key][0]
        now = time.monotonic()
        # 剩余 TTL 应在 (0, 30+1) 之间 — 不能是 5min
        remaining = expires_at - now
        assert 0 < remaining <= FALLBACK_CACHE_TTL_SECONDS + 1, (
            f"expected fallback TTL ≈ 30s, got {remaining:.1f}s"
        )





class TestSingleflight:
    """iter 165: _singleflight_fetch 并发去重 — 同一 key 的 fetcher 只执行一次。"""

    def teardown_method(self):
        import services.data_provider as dp
        with dp._FLIGHT_LOCK:
            dp._FLIGHT.clear()
            dp._FLIGHT_RESULT.clear()

    def test_concurrent_same_key_dedup(self):
        """8 线程 barrier 同步同时调用同一 key → fetcher 只调 1 次, 全部拿到同一结果。"""
        import threading, time
        from concurrent.futures import ThreadPoolExecutor
        from services.data_provider import _singleflight_fetch

        calls = {'n': 0}
        lock = threading.Lock()
        barrier = threading.Barrier(8)  # 8 线程同时出发
        def fetcher():
            with lock:
                calls['n'] += 1
            time.sleep(0.2)
            return ('data', 'akshare', 'cn', 'name')
        def worker():
            barrier.wait()
            return _singleflight_fetch('K', fetcher)

        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = [ex.submit(worker) for _ in range(8)]
            results = [f.result() for f in futures]

        assert calls['n'] == 1, f'并发去重应只调 1 次, 实际 {calls["n"]}'
        assert all(r == ('data', 'akshare', 'cn', 'name') for r in results)

    def test_different_keys_parallel(self):
        """不同 key 各自执行, 互不阻塞。"""
        import time
        from concurrent.futures import ThreadPoolExecutor
        from services.data_provider import _singleflight_fetch
        import threading
        lock = threading.Lock()
        calls = {'n': 0}
        def make_fetcher(tag):
            def fetcher():
                with lock:
                    calls['n'] += 1
                time.sleep(0.1)
                return (tag,)
            return fetcher
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(lambda t=t: _singleflight_fetch(f'K{t}', make_fetcher(t))) for t in range(4)]
            results = [f.result() for f in futures]
        assert calls['n'] == 4  # 4 个不同 key → 各调 1 次

    def test_fetcher_exception_waiter_retries(self):
        """第一个线程抛异常 → 等待者能自己重试成功。"""
        import threading
        from services.data_provider import _singleflight_fetch
        calls = {'n': 0}
        lock = threading.Lock()
        def fetcher():
            with lock:
                calls['n'] += 1
                if calls['n'] == 1:
                    raise ValueError('upstream down')
            return 'ok-after-retry'
        # 第一个线程抛异常
        import pytest as _pytest
        with _pytest.raises(ValueError):
            _singleflight_fetch('K', fetcher)
        # 等待者重试成功
        assert _singleflight_fetch('K', fetcher) == 'ok-after-retry'


class TestBarsForDays:
    """iter 164: _bars_for_days 把日历天数换算成周期 bar 数。"""

    def test_daily_is_days(self):
        from services.data_provider import _bars_for_days
        assert _bars_for_days("daily", 365) == 365
        assert _bars_for_days("daily", 30) == 30

    def test_weekly_is_days_over_7(self):
        from services.data_provider import _bars_for_days
        assert _bars_for_days("weekly", 365) == 52
        assert _bars_for_days("weekly", 30) == 4

    def test_monthly_is_days_over_30(self):
        from services.data_provider import _bars_for_days
        assert _bars_for_days("monthly", 365) == 12
        assert _bars_for_days("monthly", 60) == 2

    def test_small_days_never_zero(self):
        from services.data_provider import _bars_for_days
        assert _bars_for_days("weekly", 3) == 1
        assert _bars_for_days("monthly", 3) == 1


class TestFetchUsAkshare:
    """iter 160: akshare 新浪美股接口 — 用户指出 v1 应能实时拉美股。

    实测 ak.stock_us_daily (新浪) 在 Yahoo 限流网络下返回真实数据。
    这些测试用 mock akshare 模块验证 _fetch_us_akshare 的归一化 + resample 逻辑。
    """

    @staticmethod
    def _daily_df(n: int = 5):
        import pandas as pd
        return pd.DataFrame({
            "date": pd.date_range("2026-07-27", periods=n, freq="D"),
            "open": [100.0 + i for i in range(n)],
            "high": [105.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [104.0 + i for i in range(n)],
            "volume": [1e6] * n,
        })

    def test_daily_returns_normalized_data(self, monkeypatch):
        """daily 直接用 stock_us_daily, 返回 KLineData 列表。"""
        from unittest.mock import MagicMock, patch
        mock_ak = MagicMock()
        mock_ak.stock_us_daily = MagicMock(return_value=self._daily_df(5))
        from services.data_provider import _fetch_us_akshare
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            result = _fetch_us_akshare("AAPL", "daily", 5)
        assert result is not None
        assert len(result) == 5
        assert result[0].close == 104.0
        assert result[0].amount == 0.0  # 新浪无成交额, 默认 0
        assert result[0].date == "2026-07-27"

    def test_daily_slices_to_days(self, monkeypatch):
        """请求 days < 返回行数时, 截取最近 days 根。"""
        from unittest.mock import MagicMock, patch
        mock_ak = MagicMock()
        mock_ak.stock_us_daily = MagicMock(return_value=self._daily_df(20))
        from services.data_provider import _fetch_us_akshare
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            result = _fetch_us_akshare("AAPL", "daily", 10)
        assert result is not None
        assert len(result) == 10
        assert result[0].close == 114.0  # 第 10 根 (104 + 10)

    def test_weekly_resamples_ohlcv(self, monkeypatch):
        """weekly 从 daily resample: open=first, high=max, low=min, close=last, volume=sum。"""
        import pandas as pd
        from unittest.mock import MagicMock, patch
        # 跨两周的 8 天 (周一~下周三)
        df = pd.DataFrame({
            "date": pd.date_range("2026-07-27", periods=8, freq="D"),  # 7/27周一 ~ 8/3周二
            "open": [100, 101, 102, 103, 104, 105, 106, 107.0],
            "high": [110, 111, 112, 113, 114, 115, 116, 117.0],
            "low": [99, 98, 97, 96, 95, 94, 93, 92.0],
            "close": [108, 109, 110, 111, 112, 113, 114, 115.0],
            "volume": [1e6, 2e6, 3e6, 4e6, 5e6, 6e6, 7e6, 8e6],
        })
        mock_ak = MagicMock()
        mock_ak.stock_us_daily = MagicMock(return_value=df)
        from services.data_provider import _fetch_us_akshare
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            # days=20 → _bars_for_days(weekly, 20)=2, 保留 2 根周线
            result = _fetch_us_akshare("AAPL", "weekly", 20)
        assert result is not None
        assert len(result) == 2  # 第一周 (7/27-7/31) + 第二周 (8/3-8/5 部分)
        # 第一周 (5 个交易日): open=100, high=max(110..114)=114, low=min(99..95)=95, close=112, volume=1+2+3+4+5=15e6
        assert result[0].date == "2026-07-31"
        assert result[0].open == 100.0
        assert result[0].close == 112.0
        assert result[0].high == 114.0
        assert result[0].low == 95.0
        assert result[0].volume == 15_000_000.0
        # 第二周 (部分 3 天): open=105, close=115, high=117, low=92, volume=6+7+8=21e6
        assert result[1].open == 105.0
        assert result[1].close == 115.0
        assert result[1].high == 117.0
        assert result[1].low == 92.0
        assert result[1].volume == 21_000_000.0
        # 最后一段 partial 周日期被 clamp 到实际最后交易日 (8/3), 不出现未来日期
        assert result[1].date == "2026-08-03"

    def test_akshare_failure_returns_none(self, monkeypatch):
        """akshare 抛异常 → 返回 None, 让调用方走 yfinance。"""
        from unittest.mock import MagicMock, patch
        mock_ak = MagicMock()
        mock_ak.stock_us_daily = MagicMock(side_effect=Exception("Sina DNS fail"))
        from services.data_provider import _fetch_us_akshare
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            result = _fetch_us_akshare("AAPL", "daily", 5)
        assert result is None

    def test_fetch_history_us_akshare_primary(self, monkeypatch):
        """iter 160: fetch_history 美股走 akshare 成功 → source='akshare', yfinance 不被调用。"""
        import pandas as pd
        from unittest.mock import MagicMock, patch
        monkeypatch.setenv("FORCE_MOCK", "0")
        from services.cache import kline_cache
        kline_cache.clear()

        mock_ak = MagicMock()
        mock_ak.stock_us_daily = MagicMock(return_value=self._daily_df(10))
        import yfinance as real_yf
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            with patch.object(real_yf, "Ticker", side_effect=AssertionError("yfinance 不应被调用")):
                data, source, market, name = fetch_history("AAPL", "daily", 10)
        assert source == SOURCE_AKSHARE
        assert market == "us"
        assert name == "Apple Inc."
        assert len(data) == 10
        assert data[0].close == 104.0

    def test_fetch_history_weekly_akshare(self, monkeypatch):
        """fetch_history weekly 走 akshare resample → 返回周 K。"""
        import pandas as pd
        from unittest.mock import MagicMock, patch
        monkeypatch.setenv("FORCE_MOCK", "0")
        from services.cache import kline_cache
        kline_cache.clear()

        mock_ak = MagicMock()
        mock_ak.stock_us_daily = MagicMock(return_value=self._daily_df(20))
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            data, source, market, name = fetch_history("AAPL", "weekly", 10)
        assert source == SOURCE_AKSHARE
        assert market == "us"
        assert len(data) > 0
        # weekly 每根 bar 对应一周
        assert all(bar.close > 0 for bar in data)




class TestCnNameMap:
    """iter 163: 全量 A股列表 — 内置字典只有 ~100 条, 大量股票 (昊华科技/天齐锂业) 搜不到。
    用 akshare stock_info_a_code_name 补全 (~5500 只), 进程内缓存 24h, FORCE_MOCK=1 时降级内置。
    """

    def teardown_method(self):
        import services.data_provider as dp
        dp._CN_MAP_CACHE = None
        dp._CN_MAP_LOADED_AT = 0.0
        import os
        os.environ["FORCE_MOCK"] = "1"

    def test_force_mock_uses_builtin_only(self, monkeypatch):
        """FORCE_MOCK=1 时返回内置字典, 不触发 akshare 网络调用。"""
        import services.data_provider as dp
        from unittest.mock import MagicMock, patch
        monkeypatch.setenv("FORCE_MOCK", "1")
        # 若 _get_cn_name_map 调 akshare, 这里 mock 会抛 AssertionError
        mock_ak = MagicMock()
        mock_ak.stock_info_a_code_name = MagicMock(side_effect=AssertionError("FORCE_MOCK=1 不应调 akshare"))
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            m = dp._get_cn_name_map()
        assert len(m) == len(dp.CN_NAME_HINTS)
        assert m["600519"] == "贵州茅台"

    def test_merges_akshare_full_list(self, monkeypatch):
        """FORCE_MOCK=0 + akshare 返回全量 → 合并, 覆盖内置缺失的股票。"""
        import pandas as pd
        from unittest.mock import MagicMock, patch
        import services.data_provider as dp
        monkeypatch.setenv("FORCE_MOCK", "0")
        # akshare 返回部分全量 (含内置缺失的 600378 昊华科技 / 002466 天齐锂业)
        mock_ak = MagicMock()
        mock_ak.stock_info_a_code_name = MagicMock(return_value=pd.DataFrame({
            "code": ["600378", "002466", "600519"],
            "name": ["昊华科技", "天齐锂业", "贵州茅台"],
        }))
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            m = dp._get_cn_name_map()
        assert "600378" in m and m["600378"] == "昊华科技"
        assert "002466" in m and m["002466"] == "天齐锂业"
        # 内置优先: 600519 保留内置名
        assert m["600519"] == "贵州茅台"

    def test_akshare_failure_falls_back_to_builtin(self, monkeypatch):
        """akshare 拉取失败 → 降级内置, 不抛异常。"""
        from unittest.mock import MagicMock, patch
        import services.data_provider as dp
        monkeypatch.setenv("FORCE_MOCK", "0")
        mock_ak = MagicMock()
        mock_ak.stock_info_a_code_name = MagicMock(side_effect=Exception("EastMoney down"))
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            m = dp._get_cn_name_map()
        assert len(m) == len(dp.CN_NAME_HINTS)

    def test_search_finds_full_list_stock(self, monkeypatch):
        """iter 163: 全量列表合并后, 昊华科技/天齐锂业可被搜索到。"""
        import pandas as pd
        from unittest.mock import MagicMock, patch
        import services.data_provider as dp
        monkeypatch.setenv("FORCE_MOCK", "0")
        mock_ak = MagicMock()
        mock_ak.stock_info_a_code_name = MagicMock(return_value=pd.DataFrame({
            "code": ["600378", "002466"],
            "name": ["昊华科技", "天齐锂业"],
        }))
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            r = dp.search_stocks("昊华科技")
        assert any(x["code"] == "600378" and x["market"] == "cn" for x in r)
        r2 = dp.search_stocks("002466")
        assert any(x["code"] == "002466" for x in r2)

    def test_guess_name_uses_full_list(self, monkeypatch):
        """guess_name 对内置缺失的股票返回全量名, 而非 (未匹配)。"""
        import pandas as pd
        from unittest.mock import MagicMock, patch
        import services.data_provider as dp
        monkeypatch.setenv("FORCE_MOCK", "0")
        mock_ak = MagicMock()
        mock_ak.stock_info_a_code_name = MagicMock(return_value=pd.DataFrame({
            "code": ["600378"], "name": ["昊华科技"],
        }))
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            assert dp.guess_name("600378", "cn") == "昊华科技"


class TestFetchTencent:
    """iter 162: 腾讯直连 fallback — akshare 东财不可达时兜底真实数据。

    借鉴 v2 tencent_fetcher. 响应列序是 [date, open, close, high, low, volume]
    (open/close 在 high/low 之前), 与常见 OHLC 顺序不同, 必须按此解析。
    """

    @staticmethod
    def _mock_tencent_response(symbol: str = "sh600519", freq: str = "qfqday") -> dict:
        return {
            "code": 0,
            "msg": "",
            "data": {
                symbol: {
                    freq: [
                        ["2026-08-03", "1350.00", "1330.00", "1355.00", "1328.00", "100000.000"],
                        ["2026-08-04", "1330.00", "1345.00", "1348.00", "1325.00", "120000.000"],
                        ["2026-08-05", "1345.00", "1306.45", "1348.00", "1303.00", "90000.000"],
                    ]
                }
            },
        }

    def test_daily_parses_ohlc_correctly(self):
        """腾讯列序 [date, open, close, high, low, vol] — open 在前, 不能按 OHLC 顺序读。"""
        import sys
        from unittest.mock import MagicMock, patch
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=self._mock_tencent_response())
        with patch.dict("sys.modules", {"requests": MagicMock(get=MagicMock(return_value=mock_resp))}):
            from services.data_provider import _fetch_tencent
            data = _fetch_tencent("600519", "daily", 3, "cn")
        assert data is not None
        assert len(data) == 3
        # 2026-08-03: open=1350, close=1330, high=1355, low=1328
        assert data[0].date == "2026-08-03"
        assert data[0].open == 1350.0
        assert data[0].close == 1330.0
        assert data[0].high == 1355.0
        assert data[0].low == 1328.0
        assert data[0].amount == 0.0

    def test_slices_to_days(self):
        from unittest.mock import MagicMock, patch
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=self._mock_tencent_response())
        with patch.dict("sys.modules", {"requests": MagicMock(get=MagicMock(return_value=mock_resp))}):
            from services.data_provider import _fetch_tencent
            data = _fetch_tencent("600519", "daily", 2, "cn")
        assert data is not None
        assert len(data) == 2
        assert data[0].date == "2026-08-04"  # 取最近 2 天

    def test_hk_symbol_uses_hk_prefix(self):
        """港股 code → hk00700 (保留前导 0)。"""
        import sys
        from unittest.mock import MagicMock, patch
        captured = {}
        def fake_get(url, params=None, **kwargs):
            captured["param"] = params.get("param") if params else None
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json = MagicMock(return_value=self._mock_tencent_response("hk00700"))
            return mock_resp
        with patch.dict("sys.modules", {"requests": MagicMock(get=fake_get)}):
            from services.data_provider import _fetch_tencent
            data = _fetch_tencent("00700", "daily", 3, "hk")
        assert data is not None
        assert "hk00700" in captured["param"], captured["param"]

    def test_http_error_returns_none(self):
        from unittest.mock import MagicMock, patch
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock(side_effect=Exception("HTTP 500"))
        with patch.dict("sys.modules", {"requests": MagicMock(get=MagicMock(return_value=mock_resp))}):
            from services.data_provider import _fetch_tencent
            assert _fetch_tencent("600519", "daily", 3, "cn") is None

    def test_us_market_returns_none(self):
        from services.data_provider import _fetch_tencent
        assert _fetch_tencent("AAPL", "daily", 3, "us") is None

    def test_fetch_a_share_falls_back_to_tencent(self, monkeypatch):
        """iter 162: akshare 失败 → 腾讯兜底成功 → source='tencent'。"""
        import pandas as pd
        from unittest.mock import MagicMock, patch
        import sys
        monkeypatch.setenv("FORCE_MOCK", "0")
        from services.cache import kline_cache
        kline_cache.clear()

        # akshare 抛异常
        mock_ak = MagicMock()
        mock_ak.stock_zh_a_hist = MagicMock(side_effect=Exception("EastMoney DNS fail"))
        # 腾讯返回数据
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value=self._mock_tencent_response())
        import requests as real_requests
        with patch.dict("sys.modules", {"akshare": mock_ak}):
            with patch.object(real_requests, "get", return_value=mock_resp):
                # 禁用 yfinance (tencent 成功后不该走到)
                import yfinance as real_yf
                with patch.object(real_yf, "download", side_effect=AssertionError("yfinance 不应被调用")):
                    from services.data_provider import fetch_history, SOURCE_TENCENT
                    data, source, market, name = fetch_history("600519", "daily", 3)
        assert source == SOURCE_TENCENT
        assert market == "cn"
        assert len(data) == 3
        assert data[-1].close == 1306.45


class TestSourceConstants:
    """Source 常量值稳定 — 文档/API 契约。"""

    def test_all_sources_includes_fallback(self):
        # 防止有人重命名错字符串
        assert SOURCE_MOCK_FALLBACK in ("akshare", "yfinance", "mock", "mock_fallback")
        assert SOURCE_MOCK == "mock"
        assert SOURCE_YFINANCE == "yfinance"
        assert SOURCE_AKSHARE == "akshare"
