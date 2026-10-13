"""Schemas 包初始化。

iter 110: 补全导出 — 之前只导出 Pydantic 模型, 缺少枚举/字面量类型
(YearHigh, PeakTroughSummary, MarketType, PeriodType, IndicatorType)。
外部使用 `from schemas import YearHigh` 会 ImportError, 内部虽然走
`from schemas.stocks import X` 没问题, 但 `__all__` 不全会让 IDE 自动补全
显示不完整, 也让 "from schemas import *" 不安全。
"""
from schemas.stocks import (
    # Pydantic 模型
    KLineData,
    StockHistoryResponse,
    StockQuote,
    StockSearchItem,
    StockSearchResponse,
    PeakTroughRequest,
    PeakTroughResponse,
    PeakTroughSegment,
    LLMInsightRequest,
    LLMInsightResponse,
    LLMProviderInfo,
    LLMStatusResponse,
    YearHigh,
    PeakTroughSummary,
    # 字面量类型 — 用于路径参数 / 枚举校验
    MarketType,
    PeriodType,
    IndicatorType,
)

__all__ = [
    # Pydantic 模型
    "KLineData",
    "StockHistoryResponse",
    "StockQuote",
    "StockSearchResponse",
    "StockSearchItem",
    "PeakTroughRequest",
    "PeakTroughResponse",
    "PeakTroughSegment",
    "LLMInsightRequest",
    "LLMInsightResponse",
    "LLMProviderInfo",
    "LLMStatusResponse",
    "YearHigh",
    "PeakTroughSummary",
    # 字面量类型
    "MarketType",
    "PeriodType",
    "IndicatorType",
]