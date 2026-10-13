"""Stock data schemas aligned with reference project daily_stock_analysis_v2."""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from security import (
    LLM_EXTRA_PROMPT_MAX_LENGTH,
    STOCK_CODE_MAX_LENGTH,
    STOCK_CODE_PATTERN,
)

MarketType = Literal["cn", "hk", "us"]
PeriodType = Literal["daily", "weekly", "monthly"]
# iter 98: 路径参数 enum — 让 /indicators/{indicator} 早期拒绝非法值 (422),
# 避免 fetch_history + cache write + compute_indicator 内部 ValueError 才返回 400。
IndicatorType = Literal["MACD", "BOLL", "KDJ"]


# iter 82: 直接复用 security.STOCK_CODE_PATTERN — 之前 re.compile() 同一字符串是冗余,
# 且重复构造 Pattern 对象消耗一点 CPU。


def _validate_stock_code_field(value: str) -> str:
    """Pydantic 字段校验: stock_code 长度 + 字符集。

    兼容 A/HK/US 代码 (允许 HK 前缀, .SS/.SZ/.HK 后缀), 拒绝超长与非法字符。
    返回 strip+upper 后的字符串。
    """
    if value is None:
        raise ValueError("stock_code 不能为空")
    if not isinstance(value, str):
        value = str(value)
    stripped = value.strip()
    if not stripped:
        raise ValueError("stock_code 不能为空")
    if len(stripped) > STOCK_CODE_MAX_LENGTH:
        raise ValueError(
            f"stock_code 长度超限 (上限 {STOCK_CODE_MAX_LENGTH})"
        )
    if any(ch.isspace() or (ord(ch) < 32) for ch in stripped):
        raise ValueError("stock_code 含非法字符")
    upper = stripped.upper()
    if not STOCK_CODE_PATTERN.match(upper):
        raise ValueError(
            "stock_code 格式不合法 (期望 A股 6 位数字 / 港股 1-7 位数字 / 美股 1-10 位字母数字, 可带 .SS/.SZ/.HK 后缀)"
        )
    return upper


class StockQuote(BaseModel):
    """实时行情快照。"""
    stock_code: str
    stock_name: str
    market: MarketType
    current_price: float
    change: float
    change_percent: float
    open: float
    high: float
    low: float
    prev_close: float
    volume: float
    amount: float
    update_time: datetime


class KLineData(BaseModel):
    """单根 K线数据(OHLCV)。"""
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float = 0.0
    change_percent: float = 0.0


class StockHistoryResponse(BaseModel):
    """K线历史数据响应。"""
    stock_code: str
    stock_name: str
    market: MarketType
    period: PeriodType
    data: List[KLineData]
    source: str = ""


class StockSearchItem(BaseModel):
    """股票搜索结果项。

    命名约定 (iter 64): 搜索响应使用简短的 ``code`` / ``name`` 以减小 payload;
    其它响应模型 (StockHistoryResponse / PeakTroughResponse / LLMInsight*)
    使用 ``stock_code`` / ``stock_name``。
    前端 ``StockSearchItem`` 类型同步此约定。
    """
    code: str
    name: str
    market: MarketType


class StockSearchResponse(BaseModel):
    items: List[StockSearchItem]


class PeakTroughRequest(BaseModel):
    stock_code: str
    period: PeriodType = "daily"
    days: int = Field(default=365, ge=30, le=2000)
    window: int = Field(default=3, ge=1, le=10, description="局部极值识别窗口半径")

    @field_validator("stock_code")
    @classmethod
    def _check_stock_code(cls, v: str) -> str:
        return _validate_stock_code_field(v)


class PeakTroughSegment(BaseModel):
    """一段回撤或回弹。"""
    type: Literal["decline", "rebound"]
    from_date: str
    to_date: str
    start_price: float
    end_price: float
    change_pct: float
    from_peak: bool = False  # 是否直接接在年度最高点之后


class YearHigh(BaseModel):
    """年度最高点 (peak_trough 算法识别)。"""
    date: str
    price: float


class PeakTroughSummary(BaseModel):
    """波峰波谷摘要统计 — 与前端 ``PeakTroughResponse.summary`` 字段一致。"""
    max_decline_pct: float
    max_rebound_pct: float
    current_vs_high_pct: float
    current_vs_year_start_pct: float
    current_price: float
    year_start_price: float
    segment_count: int
    decline_count: int
    rebound_count: int


class PeakTroughResponse(BaseModel):
    stock_code: str
    stock_name: str
    period: PeriodType
    year_high: YearHigh
    year_start_date: str
    analysis_date: str
    segments: List[PeakTroughSegment]
    summary: PeakTroughSummary
    # iter 121: 加 effective_window 字段 — iter 120 在 router 把 effective_window
    # 写入 payload (用户请求 window=5 实际跑 window=4 量化), 但 schema 没声明, Pydantic v2
    # 默认 extra="ignore" 会静默丢弃, 客户端永远拿不到这个值。加上 Optional 字段, 老 cache
    # 条目 (没有 effective_window) 也能通过校验。
    effective_window: Optional[int] = None


class LLMInsightRequest(BaseModel):
    stock_code: str
    period: PeriodType = "daily"
    days: int = Field(default=365, ge=30, le=2000)
    # iter 69: 之前 llm-insight 路由不传 window, 始终用默认值 — 与 /peak-trough 行为不一致。
    # 让用户也能调整滑动窗口敏感度 (peak-trough 已支持)。
    window: int = Field(default=3, ge=1, le=10)
    extra_prompt: Optional[str] = Field(
        default=None,
        max_length=LLM_EXTRA_PROMPT_MAX_LENGTH,
        description=f"用户附加问题 (上限 {LLM_EXTRA_PROMPT_MAX_LENGTH} 字符)",
    )

    @field_validator("stock_code")
    @classmethod
    def _check_stock_code(cls, v: str) -> str:
        return _validate_stock_code_field(v)

    @field_validator("extra_prompt")
    @classmethod
    def _check_extra_prompt(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str):
            v = str(v)
        stripped = v.strip()
        if not stripped:
            return None
        # 拒绝控制字符 (保留 \n \t 给多行 prompt 用)
        if any(ord(ch) < 32 and ch not in ("\n", "\t") for ch in stripped):
            raise ValueError("extra_prompt 含非法控制字符")
        return stripped


class LLMInsightResponse(BaseModel):
    provider: str
    model: str
    text: str
    source: str = "cc_switch"  # cc_switch / fallback_env


# iter 150: 用 Pydantic 模型替换 llm_status 路由的手写 dict — 与前端 LLMStatus 接口
# 对齐 (frontend/src/types.ts:67), FastAPI 自动校验 + 出现在 /docs 中。
# 之前 llm_status 返回 dict literal, 字段重命名/新增需要在两个地方手动改,
# 没有编译时检查。all_providers 项精确匹配 list_all_providers 返回的字段 (iter 108 移除 website_url)。
class LLMProviderInfo(BaseModel):
    """CC switch provider 列表项 — 来自 providers 表 SELECT id, name, is_current。"""
    id: str
    name: str
    is_current: Optional[int] = None


class LLMStatusResponse(BaseModel):
    """GET /api/v1/analysis/llm-status 返回结构。"""
    available: bool
    source: str
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    app_type: Optional[str] = None
    all_providers: List[LLMProviderInfo] = []
    # message 仅在 available=False 时携带, 可选字段避免正常响应里出现 None。
    message: Optional[str] = None