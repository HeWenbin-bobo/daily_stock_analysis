# -*- coding: utf-8 -*-
"""
===================================
三榜交集分析 Schema
===================================
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class HotAnalysisRunRequest(BaseModel):
    """触发三榜交集 + 三轮分析 的请求体"""

    # 抓取榜单时取的条数（单榜）
    hot_list_n: int = Field(
        default=200,
        ge=20,
        le=1000,
        description="每个榜单抓取的条数（任务要求'前1000'，akshare 实际最多 ~100）",
    )
    # 交集中实际进入 LLM 分析的最大股票数
    max_intersection: int = Field(
        default=10,
        ge=1,
        le=300,
        description="对交集股票调用 analyze_stock 的最大数量（防 LLM 调用爆炸）",
    )
    # 是否剔除科创板 688*
    exclude_kechuang: bool = Field(default=True)
    # 每只股票分析轮数
    rounds: int = Field(default=3, ge=1, le=5)
    # 整体最大运行时长（秒）
    # ★ round 7：UI 提供 60/120/180/240/300/600/1200/1800 分钟选项（最大 1800min=108000s），
    #   之前 le=72000 让 1800min 选项静默 422；提到 200000 给余量。
    max_runtime_seconds: int = Field(
        default=10800,
        ge=30,
        le=200000,
        description="整体最大运行时长（秒）。UI 默认 180 分钟 = 10800 秒；最高 1800 分钟 = 108000 秒",
    )
    # 轮间间隔（秒），用于触发 LLM 自身的随机性
    inter_round_sleep: float = Field(default=0.6, ge=0.0, le=10.0)
    # 是否使用简化报告（推荐以节省时间）
    simple_report: bool = Field(default=True)
    # 每次 LLM 调用的超时时间（秒）
    llm_timeout_seconds: float = Field(
        default=180.0,
        ge=30,
        le=600,
        description="每次 LLM 调用的超时时间（秒），默认 180，范围 30-600",
    )

    # 自定义过滤规则
    min_score: float = Field(default=60.0, ge=0, le=100, description="最低评分阈值")
    allowed_advice: List[str] = Field(
        default_factory=lambda: ["买入", "加仓", "持有", "BUY", "HOLD", "ADD"],
        description="允许的建议类型",
    )
    exclude_keywords: List[str] = Field(
        default_factory=lambda: ["卖出", "减仓", "SELL", "REDUCE"],
        description="排除关键词",
    )
    require_all_rounds: bool = Field(default=True, description="是否要求所有轮都通过")
    webhook_url: Optional[str] = Field(default=None, description="Webhook 通知 URL（可选）")


class AnalysisRunItem(BaseModel):
    """单轮分析的结果"""

    round: int
    # ★ round 7：改为 float。LLM 经常返回 0-100 浮点分（如 85.5），int 会丢精度甚至 422
    sentiment_score: Optional[float] = None
    operation_advice: Optional[str] = None
    decision_type: Optional[str] = None
    trend_prediction: Optional[str] = None
    model_used: Optional[str] = None
    timestamp: Optional[str] = None
    error: Optional[str] = None


class HotAnalysisPreviewRequest(BaseModel):
    """阶段1：仅抓榜 + 计算交集（不调 LLM），用于向用户预览"""

    hot_list_n: int = Field(default=200, ge=20, le=1000)
    max_intersection: int = Field(default=10, ge=1, le=300)
    exclude_kechuang: bool = Field(default=True)
    rounds: int = Field(default=3, ge=1, le=5)
    inter_round_sleep: float = Field(default=0.6, ge=0.0, le=10.0)
    simple_report: bool = Field(default=True)
    # 每次 LLM 调用的超时时间（秒），透传给 run 阶段
    llm_timeout_seconds: float = Field(
        default=180.0,
        ge=30,
        le=600,
        description="每次 LLM 调用的超时时间（秒），默认 180，范围 30-600",
    )

    # 自定义过滤规则
    min_score: float = Field(default=60.0, ge=0, le=100, description="最低评分阈值")
    allowed_advice: List[str] = Field(
        default_factory=lambda: ["买入", "加仓", "持有", "BUY", "HOLD", "ADD"],
        description="允许的建议类型",
    )
    exclude_keywords: List[str] = Field(
        default_factory=lambda: ["卖出", "减仓", "SELL", "REDUCE"],
        description="排除关键词",
    )
    require_all_rounds: bool = Field(default=True, description="是否要求所有轮都通过")


class HotAnalysisPreviewResponse(BaseModel):
    """阶段1响应：抓取结果 + 预估分析时间"""

    status: str = Field(description="ok / failed")
    started_at: str
    finished_at: str
    duration_seconds: float

    # 与 RunResponse 同名同义
    hot_lists_summary: Dict[str, int] = Field(default_factory=dict)
    hot_lists_status: Dict[str, str] = Field(default_factory=dict)
    hot_lists_notes: Dict[str, str] = Field(default_factory=dict)
    raw_intersection_count: int = 0
    filtered_intersection_count: int = 0
    # ★ 关键字段：实际进入 LLM 分析的目标数（已应用 max_intersection 上限）
    target_count: int = 0
    # ★ 预估分析时间（秒）+ 人类可读字符串
    estimated_seconds: int = 0
    estimated_human: str = ""
    # 透传给前端的辅助字段
    rounds: int = 3
    intersection_stocks: List[Dict[str, Any]] = Field(
        default_factory=list, description="交集股票列表（代码+名称+来源）"
    )
    error: Optional[str] = None


class HotAnalysisStockResult(BaseModel):
    """通过过滤的股票详情"""

    code: str
    name: str
    in_sources: List[str] = Field(default_factory=list)
    analyses: List[AnalysisRunItem]
    pass_reason: str = Field(description="score / advice / score+advice / incomplete")
    avg_score: float = 0.0
    final_advice: str = ""


class HotAnalysisRunResponse(BaseModel):
    """三榜交集分析的完整响应"""

    status: str = Field(description="ok / failed / timeout / cancelled")
    started_at: str
    finished_at: str
    duration_seconds: float

    # 三榜原始抓取条数
    # 格式: {"东方财富": int, "雪球": int, "同花顺": int, "used_lists_count": int}
    hot_lists_summary: Dict[str, int] = Field(default_factory=dict)
    # 每个榜单的抓取状态: {"东方财富": "ok"|"failed"|"fallback", ...}
    hot_lists_status: Dict[str, str] = Field(default_factory=dict)
    # 每个榜单的实现说明（仅异常时填）:
    # e.g. {"同花顺": "akshare 同名接口已下线；底层 = stock_hot_deal_xq (雪球 deals)"}
    hot_lists_notes: Dict[str, str] = Field(default_factory=dict)
    # 交集（含科创板时）
    raw_intersection_count: int = 0
    # 剔除科创板后剩余
    filtered_intersection_count: int = 0
    # 实际进入 LLM 分析的股票数
    analyzed_stock_count: int = 0
    # 通过过滤的股票数
    passed_stock_count: int = 0

    results: List[HotAnalysisStockResult] = Field(default_factory=list)
    filtered_stocks: List[FilteredStockItem] = Field(default_factory=list, description="被过滤掉的股票")
    filter_config: Dict[str, Any] = Field(default_factory=dict, description="使用的过滤配置")
    notification_sent: bool = Field(default=False, description="是否已发送通知")
    error: Optional[str] = None
    error_code: Optional[str] = Field(
        default=None,
        description="结构化错误码，如 PIPELINE_INIT_FAILED / LLM_ANALYSIS_FAILED / INTERSECTION_FAILED 等",
    )


class SingleStockRequest(BaseModel):
    """单股分析请求"""

    code: str = Field(description="股票代码或名称")
    rounds: int = Field(default=3, ge=1, le=5)
    simple_report: bool = Field(default=True)
    llm_timeout_seconds: float = Field(default=180.0, ge=30, le=600)
    inter_round_sleep: float = Field(default=0.6, ge=0, le=10, description="轮间间隔（秒）；0 表示无间隔")


class SingleStockResponse(BaseModel):
    """单股分析响应"""

    status: str
    code: str
    name: str
    analyses: List[AnalysisRunItem] = Field(default_factory=list)
    avg_score: float = 0.0
    final_advice: str = ""
    pass_reason: str = ""
    error: Optional[str] = None
    error_code: Optional[str] = Field(
        default=None,
        description="结构化错误码，与 HotAnalysisRunResponse 对齐（INVALID_INPUT_CODE_OR_NAME / PIPELINE_INIT_FAILED 等）",
    )


class AnalysisHistoryItem(BaseModel):
    """历史记录单项"""

    id: int = 0
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    status: str = ""
    hot_lists_summary: Dict[str, int] = Field(default_factory=dict)
    hot_lists_status: Dict[str, str] = Field(default_factory=dict)
    hot_lists_notes: Dict[str, str] = Field(default_factory=dict)
    raw_intersection_count: int = 0
    filtered_intersection_count: int = 0
    analyzed_stock_count: int = 0
    passed_stock_count: int = 0
    filter_config: Dict[str, Any] = Field(default_factory=dict)
    results: List[HotAnalysisStockResult] = Field(default_factory=list)


class AnalysisHistoryResponse(BaseModel):
    """历史记录列表响应"""

    total: int
    page: int
    page_size: int
    items: List[AnalysisHistoryItem] = Field(default_factory=list)


class CompareRequest(BaseModel):
    """对比分析请求"""

    run_id_1: int = Field(description="第一次分析 ID")
    run_id_2: int = Field(description="第二次分析 ID")


class CompareResult(BaseModel):
    """对比结果"""

    run_1: AnalysisHistoryItem = Field(default_factory=AnalysisHistoryItem)
    run_2: AnalysisHistoryItem = Field(default_factory=AnalysisHistoryItem)
    new_stocks: List[str] = Field(default_factory=list, description="新增股票代码")
    removed_stocks: List[str] = Field(default_factory=list, description="退出股票代码")
    score_changes: List[Dict[str, Any]] = Field(default_factory=list, description="评分变化")
    error: Optional[str] = None


class WebhookConfig(BaseModel):
    """Webhook 配置"""

    url: str = Field(description="Webhook URL")
    method: str = Field(default="POST")
    headers: Dict[str, str] = Field(default_factory=dict)


class NotificationConfig(BaseModel):
    """通知配置"""

    webhook: Optional[WebhookConfig] = None
    enabled: bool = Field(default=False)


class TaskProgress(BaseModel):
    """任务进度"""

    task_id: str
    status: str  # 'running' | 'completed' | 'failed' | 'cancelled'
    phase: str  # 'fetching' | 'analyzing' | 'completed'
    total_stocks: int = 0
    completed_stocks: int = 0
    current_stock: str = ""
    current_round: int = 0
    passed_count: int = 0
    error: Optional[str] = None
    started_at: str = ""
    elapsed_seconds: float = 0.0


# ============================================================
# 自选股跟踪
# ============================================================
class WatchlistItem(BaseModel):
    """自选股"""
    id: int = 0
    code: str
    name: str = ""
    notes: str = ""
    created_at: str = ""


class WatchlistResponse(BaseModel):
    """自选股列表响应"""
    items: List[WatchlistItem] = Field(default_factory=list)


class WatchlistRequest(BaseModel):
    """添加自选股请求"""
    code: str = Field(description="股票代码")
    name: str = Field(default="", description="股票名称")
    notes: str = Field(default="", description="备注")


# ============================================================
# 分析模板
# ============================================================
class AnalysisTemplate(BaseModel):
    """分析模板（过滤规则预设）"""
    id: int = 0
    name: str
    description: str = ""
    config: Dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False
    created_at: str = ""
    updated_at: str = ""


class TemplateRequest(BaseModel):
    """保存模板请求"""
    name: str = Field(description="模板名称")
    description: str = Field(default="")
    config: Dict[str, Any] = Field(description="过滤规则配置")
    is_default: bool = Field(default=False)


class TemplateListResponse(BaseModel):
    """模板列表响应"""
    items: List[AnalysisTemplate] = Field(default_factory=list)


# ============================================================
# 定时任务
# ============================================================
class ScheduledTask(BaseModel):
    """定时任务"""
    id: int = 0
    name: str
    enabled: bool = True
    cron_expr: str = Field(description="cron 表达式，如 '0 15 * * 1-5'")
    config: Dict[str, Any] = Field(default_factory=dict)
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None
    last_result_id: Optional[int] = None
    created_at: str = ""


class ScheduledTaskRequest(BaseModel):
    """创建/更新定时任务请求"""
    name: str
    enabled: bool = True
    cron_expr: str = Field(description="cron 表达式")
    config: Dict[str, Any] = Field(default_factory=dict)
    task_id: Optional[int] = Field(default=None, description="更新时传入")


class ScheduledTaskListResponse(BaseModel):
    """定时任务列表响应"""
    items: List[ScheduledTask] = Field(default_factory=list)


# ============================================================
# 行情数据
# ============================================================
class MarketDataItem(BaseModel):
    """个股行情数据"""
    code: str
    name: str = ""
    price: float = 0.0
    change_pct: float = 0.0
    volume: float = 0.0
    turnover: float = 0.0
    high: float = 0.0
    low: float = 0.0
    open: float = 0.0
    market_cap: float = 0.0


class MarketDataResponse(BaseModel):
    """行情数据响应"""
    items: List[MarketDataItem] = Field(default_factory=list)
    updated_at: str = ""


# ============================================================
# PDF 报告
# ============================================================
class PDFReportRequest(BaseModel):
    """PDF 报告请求"""
    run_id: Optional[int] = Field(default=None, description="历史记录 ID（为空则使用最近一次）")


# ============================================================
# 过滤后股票（未通过）
# ============================================================
class FilteredStockItem(BaseModel):
    """被过滤掉的股票"""
    code: str
    name: str = ""
    in_sources: List[str] = Field(default_factory=list)
    filter_reason: str = Field(description="过滤原因: rejected_sell / rejected_filter / incomplete")
    avg_score: float = 0.0
    final_advice: str = ""
    analyses: List[AnalysisRunItem] = Field(default_factory=list)


# ============================================================
# 大盘复盘（每日报告）
# ============================================================
class MarketReviewRequest(BaseModel):
    """大盘复盘请求"""
    review_date: Optional[str] = Field(
        default=None,
        description="复盘日期 YYYY-MM-DD，默认今天",
        pattern=r"^(\d{4}-\d{2}-\d{2})?$",
    )
    region: str = Field(
        default="cn",
        description="市场区域（当前仅支持 cn）",
        pattern="^(cn)$",
    )
    persist: bool = Field(default=True, description="是否持久化到 DB")


class MarketReviewItem(BaseModel):
    """一次大盘复盘记录"""
    id: int
    review_date: str
    region: str = "cn"
    status: str = "ok"
    summary: str = ""
    breadth: Dict[str, Any] = Field(default_factory=dict)
    indices: Dict[str, Any] = Field(default_factory=dict)
    sectors: Dict[str, Any] = Field(default_factory=dict)
    signals: Dict[str, Any] = Field(default_factory=dict)
    warnings: List[str] = Field(
        default_factory=list,
        description="非致命数据源失败列表（例如某板块抓取失败）",
    )
    report_markdown: str = ""
    duration_seconds: float = 0.0
    error: str = ""
    created_at: str = ""
    stocks_fetch_seconds: float = 0.0


class MarketReviewListResponse(BaseModel):
    """复盘记录列表（分页）"""
    items: List[MarketReviewItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
