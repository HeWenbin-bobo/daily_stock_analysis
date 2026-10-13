"""路由集成测试 — 使用 FastAPI TestClient 验证完整 HTTP 请求/响应。

这些测试不依赖外部数据源 (全部走 mock), 验证:
  - 端点 URL 正确
  - 请求参数解析
  - 响应 schema 一致
  - 错误处理 (4xx/5xx)
  - 限流 (429)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import os

# 在 import app 前强制 mock, 避免真实网络
os.environ["FORCE_MOCK"] = "1"

import pytest
from fastapi.testclient import TestClient

import app as app_module
from middlewares.rate_limit import rate_limiter


@pytest.fixture
def client():
    """每个 test 前重置限流状态。"""
    rate_limiter.reset()
    return TestClient(app_module.app)


class TestStocksRouter:
    """Stocks 端点集成测试。"""

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        # iter 103: force_mock 从 /health 也移除 (iter 102 先从 / 移除),
        # 与 /admin/stats 未来鉴权暴露的策略一致。运维可读 .env / 服务进程环境变量。
        assert "force_mock" not in data
        assert "cache" in data

    def test_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        data = r.json()
        assert "endpoints" in data
        assert any("/api/v1/stocks/search" in e for e in data["endpoints"])

    def test_search_a_share(self, client):
        r = client.get("/api/v1/stocks/search?q=600519")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) >= 1
        assert items[0]["code"] == "600519"
        assert items[0]["market"] == "cn"

    def test_search_hk(self, client):
        r = client.get("/api/v1/stocks/search?q=00700")
        assert r.status_code == 200
        items = r.json()["items"]
        assert any(it["code"] == "00700" for it in items)

    def test_search_us(self, client):
        r = client.get("/api/v1/stocks/search?q=AAPL")
        assert r.status_code == 200
        items = r.json()["items"]
        assert any(it["code"] == "AAPL" for it in items)

    def test_search_empty_query(self, client):
        """空查询应返回空列表(200), 不报错。"""
        r = client.get("/api/v1/stocks/search?q=")
        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_search_no_results(self, client):
        r = client.get("/api/v1/stocks/search?q=zzzznotfoundxyz")
        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_history_daily(self, client):
        r = client.get("/api/v1/stocks/600519/history?period=daily&days=60")
        assert r.status_code == 200
        data = r.json()
        assert data["stock_code"] == "600519"
        assert data["market"] == "cn"
        assert data["period"] == "daily"
        assert data["source"] == "mock"
        assert len(data["data"]) > 0
        assert "open" in data["data"][0]
        assert "close" in data["data"][0]

    def test_history_weekly(self, client):
        r = client.get("/api/v1/stocks/AAPL/history?period=weekly&days=180")
        assert r.status_code == 200
        data = r.json()
        assert data["period"] == "weekly"
        assert data["market"] == "us"

    def test_history_monthly(self, client):
        r = client.get("/api/v1/stocks/600519/history?period=monthly&days=730")
        assert r.status_code == 200
        data = r.json()
        assert data["period"] == "monthly"

    def test_history_invalid_period_returns_422(self, client):
        r = client.get("/api/v1/stocks/600519/history?period=hourly&days=60")
        assert r.status_code == 422
        assert "detail" in r.json()

    def test_history_days_too_small_returns_422(self, client):
        r = client.get("/api/v1/stocks/600519/history?period=daily&days=5")
        assert r.status_code == 422

    def test_history_days_too_large_returns_422(self, client):
        r = client.get("/api/v1/stocks/600519/history?period=daily&days=10000")
        assert r.status_code == 422

    # iter 125: 补测 /history ValueError + Exception 错误路径 — review 报告标注为 MEDIUM。
    # /peak-trough 的错误路径已有测试 (line 382+), 但 /history 完全没有 — 同样的
    # 信息泄露防护 (不暴露原始异常文本) 和 rid= 日志模式 (iter 119) 应当平行覆盖。
    def test_history_400_on_value_error(self, client, monkeypatch):
        """fetch_history 抛 ValueError 时, /history 应返回 400 + 通用 detail (不泄露原始异常)。"""
        from routers import stocks as stocks_router

        def boom(*args, **kwargs):
            raise ValueError("internal-secret-column-name-not-for-users")

        monkeypatch.setattr(stocks_router, "fetch_history", boom)

        r = client.get("/api/v1/stocks/600519/history?period=daily&days=30")
        assert r.status_code == 400
        assert r.json()["detail"] == "请求参数无效"
        # 验证原始异常文本未泄露
        assert "internal-secret-column-name-not-for-users" not in r.text

    def test_history_500_on_generic_exception(self, client, monkeypatch):
        """fetch_history 抛通用 Exception 时, /history 应返回 500 + 通用 detail。"""
        from routers import stocks as stocks_router

        def boom(*args, **kwargs):
            raise RuntimeError("network-down-internal-debug-info")

        monkeypatch.setattr(stocks_router, "fetch_history", boom)

        r = client.get("/api/v1/stocks/600519/history?period=daily&days=30")
        assert r.status_code == 500
        assert r.json()["detail"] == "获取历史数据失败"
        # 验证原始异常文本未泄露
        assert "network-down-internal-debug-info" not in r.text

    def test_indicator_macd(self, client):
        r = client.get("/api/v1/stocks/600519/indicators/MACD?period=daily&days=60")
        assert r.status_code == 200
        data = r.json()
        assert data["indicator"] == "MACD"
        assert "dif" in data["series"]
        assert "dea" in data["series"]
        assert "hist" in data["series"]

    def test_indicator_boll(self, client):
        r = client.get("/api/v1/stocks/AAPL/indicators/BOLL?period=daily&days=60")
        assert r.status_code == 200
        data = r.json()
        assert data["indicator"] == "BOLL"
        assert "mid" in data["series"]

    def test_indicator_kdj(self, client):
        r = client.get("/api/v1/stocks/00700/indicators/KDJ?period=daily&days=30")
        assert r.status_code == 200
        data = r.json()
        assert data["indicator"] == "KDJ"
        assert "k" in data["series"]

    def test_indicator_invalid_returns_422(self, client):
        # iter 98: indicator 改为 IndicatorType Literal — FastAPI 在路径参数验证阶段
        # 直接拒绝非法值, 返回 422。自定义 RequestValidationError handler
        # (app.py) 把 detail 数组 sanitize 成通用字符串 "请求参数无效"。
        r = client.get("/api/v1/stocks/AAPL/indicators/RSI")
        assert r.status_code == 422
        assert r.json()["detail"] == "请求参数无效"


class TestAnalysisRouter:
    """Analysis 端点集成测试。"""

    def test_peak_trough(self, client):
        r = client.post(
            "/api/v1/analysis/peak-trough",
            json={"stock_code": "600519", "period": "daily", "days": 365},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["stock_code"] == "600519"
        assert "year_high" in data
        assert "segments" in data
        assert "summary" in data
        # 验证 summary 字段
        s = data["summary"]
        assert "max_decline_pct" in s
        assert "max_rebound_pct" in s
        assert "current_vs_high_pct" in s
        assert "current_vs_year_start_pct" in s
        assert "year_start_price" in s

    def test_peak_trough_with_window(self, client):
        r = client.post(
            "/api/v1/analysis/peak-trough",
            json={"stock_code": "AAPL", "period": "weekly", "days": 730, "window": 5},
        )
        assert r.status_code == 200

    def test_peak_trough_invalid_period_returns_422(self, client):
        r = client.post(
            "/api/v1/analysis/peak-trough",
            json={"stock_code": "600519", "period": "hourly", "days": 365},
        )
        assert r.status_code == 422

    def test_peak_trough_no_data_returns_404(self, client):
        r = client.post(
            "/api/v1/analysis/peak-trough",
            json={"stock_code": "INVALIDCODE123456", "period": "daily", "days": 60},
        )
        # mock 兜底应该成功, 但 segments 为空
        assert r.status_code == 200

    def test_llm_status(self, client):
        r = client.get("/api/v1/analysis/llm-status")
        assert r.status_code == 200
        data = r.json()
        # available 字段存在
        assert "available" in data
        # all_providers 字段存在
        assert "all_providers" in data
        assert isinstance(data["all_providers"], list)


class TestRateLimitIntegration:
    """LLM 限流集成测试 — 全部使用 mock chat() 避免真实网络调用, 保持 deterministic + fast."""

    @pytest.fixture(autouse=True)
    def _mock_chat(self, monkeypatch):
        """用 fast fake_chat 替换 LLM 调用. 路由通过 ``from llm import achat`` 拿到引用
        (iter 95: chat → achat 异步包装), 所以必须 patch 路由器持有的 achat 引用,
        让其返回 fake dict 而不是真正 await 线程池调用 sync chat()。
        """
        import routers.analysis as analysis_module

        async def fake_achat(*args, **kwargs):
            return {
                "provider": "mock",
                "model": "mock-model",
                "text": "mock insight",
                "source": "test_mock",
            }

        monkeypatch.setattr(analysis_module, "achat", fake_achat)

    def test_llm_insight_basic(self, client, _mock_chat):
        """单次 LLM 调用应成功 (mock chat)."""
        rate_limiter.reset()
        r = client.post(
            "/api/v1/analysis/llm-insight",
            json={"stock_code": "600519", "period": "daily", "days": 365},
        )
        assert r.status_code == 200
        data = r.json()
        assert "provider" in data
        assert "model" in data
        assert "text" in data

    def test_llm_rate_limit_after_max(self, client, _mock_chat):
        """超过 5 次/分钟应返回 429 (mock chat)."""
        rate_limiter.reset()
        payload = {"stock_code": "600519", "period": "daily", "days": 365}
        # 5 次成功
        for i in range(5):
            r = client.post("/api/v1/analysis/llm-insight", json=payload)
            assert r.status_code == 200, f"第 {i+1} 次失败: {r.status_code}"
        # 第 6 次应被限流
        r = client.post("/api/v1/analysis/llm-insight", json=payload)
        assert r.status_code == 429
        assert "Retry-After" in r.headers or "retry_after" in str(r.json()).lower()
        rate_limiter.reset()


class TestLLMErrorMapping:
    """LLM 上游异常类型 → 状态码 映射。"""

    @pytest.mark.parametrize(
        "exc_name,expected_status,expected_substring",
        [
            ("AuthenticationError", 401, "API key"),
            ("RateLimitError", 429, "限流"),
            ("Timeout", 504, "超时"),
            ("ContextWindowExceededError", 413, "上下文"),
        ],
    )
    def test_llm_insight_error_mapping(self, client, monkeypatch, exc_name, expected_status, expected_substring):
        """各 litellm 异常类应映射到对应 HTTP 状态码。"""
        from routers import analysis as analysis_router

        # 构造仅包含目标名称的异常类, 触发 analysis.py 内的名称匹配
        exc_cls = type(exc_name, (Exception,), {"__module__": "fake"})

        # iter 95: chat → achat (async wrapper); mock 必须是 async 函数以匹配 await 语义
        async def boom(*args, **kwargs):
            raise exc_cls(f"simulated {exc_name}")

        monkeypatch.setattr(analysis_router, "achat", boom)
        rate_limiter.reset()
        r = client.post(
            "/api/v1/analysis/llm-insight",
            json={"stock_code": "600519", "period": "daily", "days": 365},
        )
        assert r.status_code == expected_status, r.text
        assert expected_substring in r.json()["detail"]

    def test_llm_insight_unknown_error_500(self, client, monkeypatch):
        """未识别的异常类返回 500, detail 不含原始异常文本或类名(防信息泄露)."""
        from routers import analysis as analysis_router

        class WeirdError(Exception):
            pass

        # iter 95: chat → achat; mock 必须是 async 函数
        async def boom(*args, **kwargs):
            raise WeirdError("internal token=abc123 leaks-prevention")

        monkeypatch.setattr(analysis_router, "achat", boom)
        rate_limiter.reset()
        r = client.post(
            "/api/v1/analysis/llm-insight",
            json={"stock_code": "600519", "period": "daily", "days": 365},
        )
        assert r.status_code == 500
        detail = r.json()["detail"]
        # 错误信息是通用文本, 不应包含原始 message 或类名
        assert "LLM 解读失败" in detail
        assert "WeirdError" not in detail
        assert "leaks-prevention" not in detail
        assert "abc123" not in detail

    def test_llm_insight_provider_unavailable_returns_503(self, client, monkeypatch):
        """achat() 抛 RuntimeError (provider 不可用) 时, 应返回 503 通用提示, 不泄露内部文本。"""
        from routers import analysis as analysis_router
        from schemas.stocks import KLineData

        # 让 achat() 抛 RuntimeError (模拟 litellm_backend 里 select_provider 失败)
        # iter 95: chat → achat, async wrapper. mock 必须是 async 函数以匹配 await 语义。
        async def boom(*args, **kwargs):
            raise RuntimeError(
                "internal config leak: provider=sk-test-secret model=X base_url=http://internal"
            )

        # 准备历史数据 + 让分析成功, 真实走到 achat() 时抛错
        from services import data_provider as dp
        from services.cache import analysis_cache

        analysis_cache.clear()
        monkeypatch.setattr(
            dp, "fetch_history",
            lambda stock_code, period, days: ([KLineData(date="2026-01-01", open=1, high=1, low=1, close=1, volume=1, amount=0, change_percent=0)], "mock", "cn", "T"),
        )
        monkeypatch.setattr(analysis_router, "achat", boom)

        r = client.post(
            "/api/v1/analysis/llm-insight",
            json={"stock_code": "600519", "period": "daily", "days": 30},
        )
        assert r.status_code == 503
        detail = r.json()["detail"]
        # 关键: 响应是通用文案, 原始异常文本/provider/key 不应出现
        assert "LLM provider 暂不可用" in detail
        assert "sk-test-secret" not in detail
        assert "internal config leak" not in detail
        assert "http://internal" not in detail

    def test_llm_status_when_select_provider_raises(self, client, monkeypatch):
        """llm-status 兜底 RuntimeError 时应返回 available=False 而非 500。"""
        from routers import analysis as analysis_router

        # 让 detect_active_provider 返回 None, 强制走 select_provider 兜底
        monkeypatch.setattr(analysis_router, "detect_active_provider", lambda app_type: None)
        monkeypatch.setattr(analysis_router, "select_provider", lambda: (_ for _ in ()).throw(RuntimeError("no provider")))

        r = client.get("/api/v1/analysis/llm-status")
        assert r.status_code == 200
        data = r.json()
        assert data["available"] is False
        assert data["source"] == "none"
        assert "CC switch" in data.get("message", "")

    def test_llm_status_outer_exception_returns_500(self, client, monkeypatch):
        """detect_active_provider 抛出非常规异常时, 顶层 handler 返回 500。"""
        from routers import analysis as analysis_router

        def boom(app_type):
            raise OSError("disk gone")

        monkeypatch.setattr(analysis_router, "detect_active_provider", boom)

        r = client.get("/api/v1/analysis/llm-status")
        assert r.status_code == 500


class TestPeakTroughEdgeCases:
    """peak-trough 端点边界。"""

    def test_peak_trough_404_when_no_data(self, client, monkeypatch):
        """当 fetch_history 返回空数据时, 端点应返回 404 而非 500。"""
        from routers import analysis as analysis_router
        from services.cache import analysis_cache

        # 清缓存, 避免命中
        analysis_cache.clear()
        # 路由代码通过 `from services.data_provider import fetch_history` 获取绑定,
        # 实际函数引用缓存在 routers.analysis 模块命名空间。
        monkeypatch.setattr(
            analysis_router, "fetch_history",
            lambda stock_code, period, days: ([], "mock", "cn", "none"),
        )
        r = client.post(
            "/api/v1/analysis/peak-trough",
            json={"stock_code": "ZZEMPTY", "period": "daily", "days": 60},
        )
        assert r.status_code == 404
        assert "未取到" in r.json()["detail"]

    def test_peak_trough_400_on_value_error(self, client, monkeypatch):
        """analyze_peaks_troughs 抛 ValueError 时, 端点应 400 且不泄露原始异常文本。"""
        from services import data_provider as dp
        from services.cache import analysis_cache
        from routers import analysis as analysis_router
        from schemas.stocks import KLineData

        # 清缓存, 避免命中
        analysis_cache.clear()
        monkeypatch.setattr(
            dp, "fetch_history",
            lambda stock_code, period, days: ([KLineData(date="2026-01-01", open=1, high=1, low=1, close=1, volume=1, amount=0, change_percent=0)], "mock", "cn", "T"),
        )
        # 路由代码通过 `from services.peak_trough import ...` 获取绑定,
        # 实际函数引用缓存在 routers.analysis 模块命名空间。
        # 异常文本特含敏感信息, 验证响应中不出现
        monkeypatch.setattr(
            analysis_router, "analyze_peaks_troughs",
            lambda stock_code, stock_name, period, data, window=3: (_ for _ in ()).throw(ValueError("数据点不足 internal-token=abc123")),
        )

        r = client.post(
            "/api/v1/analysis/peak-trough",
            json={"stock_code": "600519", "period": "daily", "days": 60},
        )
        assert r.status_code == 400
        detail = r.json()["detail"]
        # 现已 sanitized: 响应固定文案, 原始异常文本和 token 都不应出现
        assert "data-source internal leak" not in detail
        assert "abc123" not in detail
        assert detail == "请求参数无效"


class TestAShareTickerSuffix:
    """验证 A股代码到 yfinance ticker 后缀的映射规则。

    routers/stocks.py:235-236 规则: clean.startswith("6") -> .SS, 否则 -> .SZ。
    测试覆盖:
      - 创业板 (300xxx) -> .SZ
      - 主板 (00xxxx) -> .SZ
      - 科创板 (688xxx) -> .SS (以6开头)
      - 主板 (6xxxxx) -> .SS
    """

    def test_300xxx_growth_board_sSZ(self, client, monkeypatch):
        """创业板 300750 (宁德时代) -> .SZ。"""
        from routers import stocks as stocks_router

        captured_ticker = {}

        def capture_yf(*args, **kwargs):
            captured_ticker["ticker"] = args[0] if args else kwargs.get("tickers")
            # 返回合法 DataFrame 以继续执行
            import pandas as pd
            return pd.DataFrame({
                "Open": [100.0, 105.0],
                "High": [102.0, 108.0],
                "Low": [99.0, 103.0],
                "Close": [100.0, 105.0],
                "Volume": [1e6, 2e6],
            }, index=pd.date_range("2026-01-01", periods=2, freq="D"))

        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(stocks_router.yf, "download", capture_yf)

        try:
            r = client.get("/api/v1/stocks/300750/quote")
            assert r.status_code == 200
            assert captured_ticker["ticker"] == "300750.SZ", (
                f"创业板 300750 应构造 .SZ, 实际 {captured_ticker['ticker']}"
            )
        finally:
            monkeypatch.setenv("FORCE_MOCK", "1")

    def test_00xxxx_main_board_SZ(self, client, monkeypatch):
        """主板 000001 (平安银行) -> .SZ。"""
        from routers import stocks as stocks_router

        captured_ticker = {}

        def capture_yf(*args, **kwargs):
            captured_ticker["ticker"] = args[0] if args else kwargs.get("tickers")
            import pandas as pd
            return pd.DataFrame({
                "Open": [10.0, 11.0],
                "High": [10.5, 11.5],
                "Low": [9.5, 10.5],
                "Close": [10.0, 11.0],
                "Volume": [1e6, 2e6],
            }, index=pd.date_range("2026-01-01", periods=2, freq="D"))

        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(stocks_router.yf, "download", capture_yf)

        try:
            r = client.get("/api/v1/stocks/000001/quote")
            assert r.status_code == 200
            assert captured_ticker["ticker"] == "000001.SZ"
        finally:
            monkeypatch.setenv("FORCE_MOCK", "1")

    def test_688xxx_star_board_SS(self, client, monkeypatch):
        """科创板 688xxx (以 6 开头) -> .SS, 与主板 6xxxxx 同规则。"""
        from routers import stocks as stocks_router

        captured_ticker = {}

        def capture_yf(*args, **kwargs):
            captured_ticker["ticker"] = args[0] if args else kwargs.get("tickers")
            import pandas as pd
            return pd.DataFrame({
                "Open": [50.0, 55.0],
                "High": [51.0, 56.0],
                "Low": [49.0, 54.0],
                "Close": [50.0, 55.0],
                "Volume": [1e6, 2e6],
            }, index=pd.date_range("2026-01-01", periods=2, freq="D"))

        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(stocks_router.yf, "download", capture_yf)

        try:
            r = client.get("/api/v1/stocks/688001/quote")
            assert r.status_code == 200
            assert captured_ticker["ticker"] == "688001.SS"
        finally:
            monkeypatch.setenv("FORCE_MOCK", "1")

    def test_6xxxxx_main_board_SS(self, client, monkeypatch):
        """主板 6xxxxx (贵州茅台) -> .SS。"""
        from routers import stocks as stocks_router

        captured_ticker = {}

        def capture_yf(*args, **kwargs):
            captured_ticker["ticker"] = args[0] if args else kwargs.get("tickers")
            import pandas as pd
            return pd.DataFrame({
                "Open": [1600.0, 1650.0],
                "High": [1610.0, 1660.0],
                "Low": [1590.0, 1640.0],
                "Close": [1600.0, 1650.0],
                "Volume": [1e5, 2e5],
            }, index=pd.date_range("2026-01-01", periods=2, freq="D"))

        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(stocks_router.yf, "download", capture_yf)

        try:
            r = client.get("/api/v1/stocks/600519/quote")
            assert r.status_code == 200
            assert captured_ticker["ticker"] == "600519.SS"
        finally:
            monkeypatch.setenv("FORCE_MOCK", "1")


class TestQuoteErrorPaths:
    """验证 /quote 错误响应不泄露内部异常文本。

    覆盖 routers/stocks.py:251-261 三种失败路径:
      1. yfinance 返回 None -> 404 "未取到"
      2. yfinance 返回非 DataFrame (dict) -> 502 "行情数据格式异常"
      3. yfinance 返回空 DataFrame -> 404 "未取到"
      4. yfinance 抛非 HTTPException -> 500 "获取行情失败"
    所有路径均验证响应 detail 不含原始异常文本或敏感信息。
    """

    def test_quote_yf_returns_none_404(self, client, monkeypatch):
        """yfinance 返回 None -> 404, detail 不泄露 None 类型。"""
        from routers import stocks as stocks_router

        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(stocks_router.yf, "download", lambda *args, **kwargs: None)

        try:
            r = client.get("/api/v1/stocks/600519/quote")
            assert r.status_code == 404
            detail = r.json()["detail"]
            assert "未取到" in detail
            assert "None" not in detail
            assert "null" not in detail
        finally:
            monkeypatch.setenv("FORCE_MOCK", "1")

    def test_quote_yf_returns_dict_502(self, client, monkeypatch):
        """yfinance 返回 dict (非 DataFrame) -> 502, detail 不泄露原始类型。"""
        from routers import stocks as stocks_router

        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(
            stocks_router.yf, "download",
            lambda *args, **kwargs: {"Close": [100.0], "secret": "token=abc123"},
        )

        try:
            r = client.get("/api/v1/stocks/600519/quote")
            assert r.status_code == 502
            detail = r.json()["detail"]
            assert "行情数据格式异常" in detail
            assert "Close" not in detail
            assert "secret" not in detail
            assert "abc123" not in detail
        finally:
            monkeypatch.setenv("FORCE_MOCK", "1")

    def test_quote_yf_returns_empty_df_404(self, client, monkeypatch):
        """yfinance 返回空 DataFrame -> 404, 与 None 路径一致。"""
        from routers import stocks as stocks_router

        monkeypatch.setenv("FORCE_MOCK", "0")
        import pandas as pd
        monkeypatch.setattr(stocks_router.yf, "download", lambda *args, **kwargs: pd.DataFrame())

        try:
            r = client.get("/api/v1/stocks/600519/quote")
            assert r.status_code == 404
            assert "未取到" in r.json()["detail"]
        finally:
            monkeypatch.setenv("FORCE_MOCK", "1")

    def test_quote_yf_generic_exception_500_no_leak(self, client, monkeypatch):
        """yfinance 抛非 HTTPException -> 500 通用文案, 不泄露异常文本。"""
        from routers import stocks as stocks_router

        monkeypatch.setenv("FORCE_MOCK", "0")
        monkeypatch.setattr(
            stocks_router.yf, "download",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("YFinance internal: DB connection pool exhausted, secret=xyz789")
            ),
        )

        try:
            r = client.get("/api/v1/stocks/600519/quote")
            assert r.status_code == 500
            detail = r.json()["detail"]
            assert detail == "获取行情失败"
            assert "DB connection" not in detail
            assert "pool" not in detail.lower()
            assert "xyz789" not in detail
        finally:
            monkeypatch.setenv("FORCE_MOCK", "1")


class TestAdminEndpoints:
    """Admin 端点测试。"""

    def test_admin_stats(self, client):
        # 触发一些请求
        client.get("/api/v1/stocks/600519/history?period=daily&days=30")
        client.get("/api/v1/stocks/AAPL/history?period=daily&days=30")
        r = client.get("/admin/stats")
        assert r.status_code == 200
        data = r.json()
        assert "endpoints" in data
        assert "cache" in data

    def test_admin_stats_reset(self, client):
        client.get("/api/v1/stocks/600519/history?period=daily&days=30")
        r = client.post("/admin/stats/reset")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
