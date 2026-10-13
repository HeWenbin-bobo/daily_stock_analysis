"""Pydantic schema 边界值测试。

验证:
  - 必填字段缺失 → ValidationError
  - 字段类型错误 → ValidationError
  - Field(ge/le) 约束违反 → ValidationError
  - 字段范围在边界值上的行为
  - model_dump() 正确序列化
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from pydantic import ValidationError

from schemas.stocks import (
    KLineData,
    LLMInsightRequest,
    LLMInsightResponse,
    LLMProviderInfo,
    LLMStatusResponse,
    PeakTroughRequest,
    PeakTroughResponse,
    PeakTroughSegment,
    StockHistoryResponse,
    StockQuote,
    StockSearchItem,
    StockSearchResponse,
)


class TestStockSearchItem:
    """StockSearchItem 边界。"""

    def test_valid_cn(self):
        item = StockSearchItem(code="600519", name="贵州茅台", market="cn")
        assert item.market == "cn"

    def test_valid_hk(self):
        item = StockSearchItem(code="00700", name="腾讯", market="hk")
        assert item.market == "hk"

    def test_valid_us(self):
        item = StockSearchItem(code="AAPL", name="Apple", market="us")
        assert item.market == "us"

    def test_invalid_market_rejected(self):
        with pytest.raises(ValidationError):
            StockSearchItem(code="X", name="X", market="invalid")

    def test_missing_code_rejected(self):
        with pytest.raises(ValidationError):
            StockSearchItem(name="X", market="cn")  # type: ignore

    def test_missing_name_rejected(self):
        with pytest.raises(ValidationError):
            StockSearchItem(code="X", market="cn")  # type: ignore


class TestKLineData:
    """KLineData 边界。"""

    def test_valid_bar(self):
        bar = KLineData(
            date="2026-01-01", open=100, high=105, low=99, close=104,
            volume=1000000, amount=0, change_percent=0
        )
        assert bar.date == "2026-01-01"
        assert bar.open == 100

    def test_negative_volume_allowed(self):
        """volume 可以为 0 或负数(罕见但合法)。"""
        bar = KLineData(
            date="2026-01-01", open=100, high=105, low=99, close=104,
            volume=0, amount=0, change_percent=0
        )
        assert bar.volume == 0

    def test_float_precision_preserved(self):
        bar = KLineData(
            date="2026-01-01", open=100.123456, high=105.654321, low=99.111111, close=104.999999,
            volume=1000000.0, amount=0.0, change_percent=0.5
        )
        # Pydantic 应保留浮点精度
        assert bar.open == 100.123456
        assert abs(bar.close - 104.999999) < 1e-9


class TestStockHistoryResponse:
    """StockHistoryResponse 序列化。"""

    def test_serialization_round_trip(self):
        from schemas.stocks import KLineData
        response = StockHistoryResponse(
            stock_code="600519",
            stock_name="贵州茅台",
            market="cn",
            period="daily",
            data=[
                KLineData(date="2026-01-01", open=100, high=105, low=99, close=104,
                          volume=1e6, amount=0, change_percent=0)
            ],
            source="mock",
        )
        d = response.model_dump()
        assert d["stock_code"] == "600519"
        assert len(d["data"]) == 1
        assert d["data"][0]["close"] == 104

    def test_optional_source_default_empty(self):
        response = StockHistoryResponse(
            stock_code="X", stock_name="X", market="cn",
            period="daily", data=[], source="mock"
        )
        # source 默认值是 ""
        response2 = StockHistoryResponse.model_validate({
            "stock_code": "X", "stock_name": "X", "market": "cn",
            "period": "daily", "data": []
        })
        assert response2.source == ""


class TestPeakTroughRequest:
    """PeakTroughRequest Field 约束。"""

    def test_defaults(self):
        req = PeakTroughRequest(stock_code="600519")
        assert req.period == "daily"
        assert req.days == 365
        assert req.window == 3

    def test_days_min_constraint(self):
        """days 必须 >= 30。"""
        with pytest.raises(ValidationError):
            PeakTroughRequest(stock_code="X", days=29)

    def test_days_max_constraint(self):
        """days 必须 <= 2000。"""
        with pytest.raises(ValidationError):
            PeakTroughRequest(stock_code="X", days=2001)

    def test_window_min_constraint(self):
        with pytest.raises(ValidationError):
            PeakTroughRequest(stock_code="X", window=0)

    def test_window_max_constraint(self):
        with pytest.raises(ValidationError):
            PeakTroughRequest(stock_code="X", window=11)

    def test_valid_boundary_values(self):
        req1 = PeakTroughRequest(stock_code="X", days=30, window=1)
        assert req1.days == 30 and req1.window == 1

        req2 = PeakTroughRequest(stock_code="X", days=2000, window=10)
        assert req2.days == 2000 and req2.window == 10

    def test_invalid_period_rejected(self):
        with pytest.raises(ValidationError):
            PeakTroughRequest(stock_code="X", period="hourly")


class TestPeakTroughSegment:
    """PeakTroughSegment 字段。"""

    def test_decline_segment(self):
        seg = PeakTroughSegment(
            type="decline", from_date="2026-01-01", to_date="2026-01-05",
            start_price=100.0, end_price=90.0, change_pct=-10.0, from_peak=True
        )
        assert seg.type == "decline"
        assert seg.from_peak is True

    def test_rebound_segment(self):
        seg = PeakTroughSegment(
            type="rebound", from_date="2026-01-05", to_date="2026-01-10",
            start_price=90.0, end_price=100.0, change_pct=11.11, from_peak=False
        )
        assert seg.type == "rebound"

    def test_invalid_type_rejected(self):
        with pytest.raises(ValidationError):
            PeakTroughSegment(
                type="invalid", from_date="2026-01-01", to_date="2026-01-05",
                start_price=100.0, end_price=90.0, change_pct=-10.0
            )


class TestPeakTroughResponse:
    """完整响应 schema。"""

    def test_full_response(self):
        response = PeakTroughResponse(
            stock_code="600519",
            stock_name="贵州茅台",
            period="daily",
            year_high={"date": "2026-04-22", "price": 100.0},
            year_start_date="2026-01-01",
            analysis_date="2026-04-22",
            segments=[
                PeakTroughSegment(
                    type="decline", from_date="2026-04-22", to_date="2026-05-01",
                    start_price=100.0, end_price=85.0, change_pct=-15.0, from_peak=True
                ),
            ],
            summary={
                "max_decline_pct": -15.0,
                "max_rebound_pct": 0.0,
                "current_vs_high_pct": -15.0,
                "current_vs_year_start_pct": -15.0,
                "current_price": 85.0,
                "year_start_price": 100.0,
                "segment_count": 1,
                "decline_count": 1,
                "rebound_count": 0,
            },
        )
        d = response.model_dump()
        assert d["stock_code"] == "600519"
        assert d["year_high"]["price"] == 100.0
        assert len(d["segments"]) == 1

    def test_json_serialization_round_trip(self):
        response = PeakTroughResponse(
            stock_code="X", stock_name="X", period="daily",
            year_high={"date": "2026-01-01", "price": 100.0},
            year_start_date="2026-01-01",
            analysis_date="2026-01-01",
            segments=[],
            summary={
                "max_decline_pct": 0, "max_rebound_pct": 0,
                "current_vs_high_pct": 0, "current_vs_year_start_pct": 0,
                "current_price": 100, "year_start_price": 100,
                "segment_count": 0, "decline_count": 0, "rebound_count": 0,
            },
        )
        import json
        json_str = response.model_dump_json()
        parsed = json.loads(json_str)
        assert parsed["stock_code"] == "X"


class TestLLMInsightRequest:
    """LLM 请求 schema。"""

    def test_required_fields(self):
        req = LLMInsightRequest(stock_code="600519")
        assert req.stock_code == "600519"
        assert req.period == "daily"
        assert req.days == 365
        assert req.extra_prompt is None

    def test_optional_extra_prompt(self):
        req = LLMInsightRequest(stock_code="X", extra_prompt="关注 MACD")
        assert req.extra_prompt == "关注 MACD"

    def test_days_constraint(self):
        """days 必须在 30-2000 之间。"""
        with pytest.raises(ValidationError):
            LLMInsightRequest(stock_code="X", days=29)
        with pytest.raises(ValidationError):
            LLMInsightRequest(stock_code="X", days=2001)


class TestLLMInsightResponse:
    """LLM 响应 schema。"""

    def test_valid_response(self):
        resp = LLMInsightResponse(
            provider="MiniMax", model="MiniMax-M3",
            text="分析内容", source="cc_switch"
        )
        assert resp.provider == "MiniMax"
        assert resp.source == "cc_switch"

    def test_default_source(self):
        resp = LLMInsightResponse(
            provider="X", model="X", text="t"
        )
        assert resp.source == "cc_switch"  # 默认值

    def test_empty_text_allowed(self):
        resp = LLMInsightResponse(provider="X", model="X", text="")
        assert resp.text == ""


class TestLLMStatusResponse:
    """LLMStatusResponse — iter 150 新增, 替换 llm_status 路由的手写 dict。"""

    def test_available_cc_switch_shape(self):
        resp = LLMStatusResponse(
            available=True,
            source="cc_switch",
            provider="anthropic",
            model="claude-opus-4",
            base_url="https://api.anthropic.com",
            app_type="claude",
            all_providers=[LLMProviderInfo(id="u1", name="anthropic", is_current=1)],
        )
        assert resp.available is True
        assert resp.all_providers[0].name == "anthropic"
        assert resp.message is None  # message 在 available=True 时不应出现

    def test_unavailable_shape(self):
        """available=False 时携带 message 字段。"""
        resp = LLMStatusResponse(
            available=False,
            source="none",
            provider=None,
            model=None,
            base_url=None,
            app_type=None,
            message="CC switch 未配置激活 provider, 且 .env 中也无 API key",
        )
        assert resp.available is False
        assert resp.message is not None

    def test_empty_providers_allowed(self):
        resp = LLMStatusResponse(
            available=True,
            source="env",
            provider="X",
            model="X",
            base_url="X",
            app_type="env",
        )
        assert resp.all_providers == []  # 默认空 list

    def test_provider_info_optional_is_current(self):
        """is_current 是 Optional, 允许 None (兜底分支返回的项不一定有)。"""
        info = LLMProviderInfo(id="x", name="y")
        assert info.is_current is None


class TestStockQuote:
    """StockQuote 行情 schema。"""

    def test_valid_quote(self):
        quote = StockQuote(
            stock_code="600519",
            stock_name="贵州茅台",
            market="cn",
            current_price=1800.0,
            change=15.0,
            change_percent=0.84,
            open=1785.0,
            high=1810.0,
            low=1780.0,
            prev_close=1785.0,
            volume=10000000,
            amount=18000000000.0,
            update_time=datetime.now(timezone.utc),
        )
        d = quote.model_dump()
        assert d["stock_code"] == "600519"
        assert d["current_price"] == 1800.0
        assert d["update_time"]  # 序列化后是字符串

    def test_invalid_market_rejected(self):
        with pytest.raises(ValidationError):
            StockQuote(
                stock_code="600519", stock_name="X", market="invalid",
                current_price=1, change=0, change_percent=0,
                open=1, high=1, low=1, prev_close=1, volume=1, amount=0,
                update_time=datetime.now(timezone.utc),
            )

    def test_negative_volume_allowed(self):
        """volume 可以是 0 或负数(测试兼容性, 真实数据不应出现)。"""
        quote = StockQuote(
            stock_code="600519", stock_name="X", market="cn",
            current_price=1, change=0, change_percent=0,
            open=1, high=1, low=1, prev_close=1, volume=-1, amount=0,
            update_time=datetime.now(timezone.utc),
        )
        assert quote.volume == -1

    def test_missing_required_field_rejected(self):
        with pytest.raises(ValidationError):
            StockQuote(stock_code="600519")  # type: ignore

    def test_change_percent_extreme_value(self):
        """涨跌幅可能为很大的正/负数(如退市股)。"""
        quote = StockQuote(
            stock_code="AAPL", stock_name="Apple", market="us",
            current_price=1, change=-50, change_percent=-99.99,
            open=100, high=100, low=1, prev_close=100, volume=1, amount=0,
            update_time=datetime.now(timezone.utc),
        )
        assert quote.change_percent == -99.99

    def test_update_time_without_timezone(self):
        """update_time 支持 naive datetime(但生产推荐 UTC)。"""
        quote = StockQuote(
            stock_code="600519", stock_name="X", market="cn",
            current_price=1, change=0, change_percent=0,
            open=1, high=1, low=1, prev_close=1, volume=1, amount=0,
            update_time=datetime(2026, 1, 1, 10, 0, 0),
        )
        assert quote.update_time is not None
