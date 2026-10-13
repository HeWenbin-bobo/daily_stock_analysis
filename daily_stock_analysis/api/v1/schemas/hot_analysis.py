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
        le=200,
        description="对交集股票调用 analyze_stock 的最大数量（防 LLM 调用爆炸）",
    )
    # 是否剔除科创板 688*
    exclude_kechuang: bool = Field(default=True)
    # 每只股票分析轮数
    rounds: int = Field(default=3, ge=1, le=5)
    # 整体最大运行时长（秒）
    max_runtime_seconds: int = Field(default=10800, ge=30, le=72000)
    # 轮间间隔（秒），用于触发 LLM 自身的随机性
    inter_round_sleep: float = Field(default=0.6, ge=0.0, le=10.0)
    # 是否使用简化报告（推荐以节省时间）
    simple_report: bool = Field(default=True)


class AnalysisRunItem(BaseModel):
    """单轮分析的结果"""

    round: int
    sentiment_score: Optional[int] = None
    operation_advice: Optional[str] = None
    decision_type: Optional[str] = None
    trend_prediction: Optional[str] = None
    model_used: Optional[str] = None
    timestamp: Optional[str] = None
    error: Optional[str] = None


class HotAnalysisPreviewRequest(BaseModel):
    """阶段1：仅抓榜 + 计算交集（不调 LLM），用于向用户预览"""

    hot_list_n: int = Field(default=200, ge=20, le=1000)
    max_intersection: int = Field(default=10, ge=1, le=200)
    exclude_kechuang: bool = Field(default=True)
    rounds: int = Field(default=3, ge=1, le=5)
    inter_round_sleep: float = Field(default=0.6, ge=0.0, le=10.0)
    simple_report: bool = Field(default=True)


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

    status: str = Field(description="ok / failed / timeout")
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
    error: Optional[str] = None
