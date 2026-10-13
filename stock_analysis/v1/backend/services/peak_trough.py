"""波峰波谷分析算法。

从当前年度最高点之后开始, 用滑动窗口识别局部极大/极小值,
计算每个波峰波谷间的回撤/回弹比例。

参考 daily_stock_analysis_v2 中 K线相关分析逻辑, 但本应用独立实现。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import List, Tuple

import numpy as np
import pandas as pd

from schemas.stocks import (
    KLineData,
    PeakTroughResponse,
    PeakTroughSegment,
    PeakTroughSummary,
    PeriodType,
    YearHigh,
)

logger = logging.getLogger(__name__)


def analyze_peaks_troughs(
    stock_code: str,
    stock_name: str,
    # iter 140: 收紧到 PeriodType Literal — 与 routers 入口 (Query Literal) 对齐,
    # 让类型检查器在直接调用此函数的测试/脚本中也能验证 period 取值。
    period: PeriodType,
    data: List[KLineData],
    window: int = 3,
) -> PeakTroughResponse:
    """主入口。

    Args:
        data: 按时间升序的 K线数据(从老到新)
        window: 局部极值识别窗口半径

    步骤:
        1. 取本年度(基于 data 最后日期的年份) 的所有数据
        2. 找年度最高点 year_high
        3. 从 year_high 之后开始, 滑动窗口识别 peaks/troughs
        4. 计算相邻极值间的回撤(peak→trough)和回弹(trough→peak)
    """
    if not data:
        raise ValueError("无数据可供分析")

    df = pd.DataFrame([d.model_dump() for d in data])
    df["date_dt"] = pd.to_datetime(df["date"])
    df = df.sort_values("date_dt").reset_index(drop=True)

    # 本年度: 使用最后一条数据的年份
    last_year = int(df["date_dt"].iloc[-1].year)
    this_year_df = df[df["date_dt"].dt.year == last_year].reset_index(drop=True)
    # 注: this_year_df 不可能为空, 因为它必包含最后一条数据(同年份)

    if len(this_year_df) < 5:
        raise ValueError("本年度数据点不足,无法分析")

    # 找年度最高点
    high_idx = this_year_df["high"].idxmax()
    year_high_date = this_year_df.loc[high_idx, "date_dt"].strftime("%Y-%m-%d")
    year_high_price = float(this_year_df.loc[high_idx, "high"])

    # 从 high_idx 之后开始分析(包含 high_idx 自身作为起点)
    after_high = this_year_df.iloc[high_idx:].reset_index(drop=True)
    if len(after_high) < 3:
        # 最高点之后数据太少
        current_price = float(after_high.iloc[-1]["close"]) if not after_high.empty else year_high_price
        current_vs_high = (
            (current_price - year_high_price) / year_high_price * 100
            if year_high_price
            else 0.0
        )
        return PeakTroughResponse(
            stock_code=stock_code,
            stock_name=stock_name,
            period=period,
            # iter 83: 用 YearHigh 模型而非裸 dict — 未来 schema 加验证时不会悄悄失效
            year_high=YearHigh(date=year_high_date, price=round(year_high_price, 4)),
            year_start_date=this_year_df.iloc[0]["date_dt"].strftime("%Y-%m-%d"),
            analysis_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            segments=[],
            summary={
                "max_decline_pct": 0.0,
                "max_rebound_pct": 0.0,
                "current_vs_high_pct": round(current_vs_high, 2),
                "current_vs_year_start_pct": round(
                    (current_price - float(this_year_df.iloc[0]["close"]))
                    / float(this_year_df.iloc[0]["close"])
                    * 100
                    if float(this_year_df.iloc[0]["close"])
                    else 0.0,
                    2,
                ),
                "current_price": current_price,
                "year_start_price": float(this_year_df.iloc[0]["close"]),
                "segment_count": 0,
                "decline_count": 0,
                "rebound_count": 0,
            },
        )

    # 滑动窗口识别局部极大/极小值
    peaks: List[int] = []  # 局部极大在 after_high 中的索引
    troughs: List[int] = []

    highs = after_high["high"].values
    lows = after_high["low"].values
    n = len(after_high)

    # 修正 MEDIUM: 数据量不足时自动收缩 window, 否则循环不执行 → 0 个极值
    # iter 112: 用 effective_window 而非重赋值 window 参数 — 避免函数入参被静默改写,
    # 让 caller 看到的 window=8 永远是 caller 传入的值 (即便我们内部用更小的 effective 值)。
    if n < 2 * window + 1:
        old_window = window
        effective_window = max(1, (n - 1) // 2)
        logger.warning(
            "peak_trough 数据点不足 (n=%d, requested window=%d), 自动收缩到 window=%d",
            n, old_window, effective_window,
        )
    else:
        effective_window = window

    # iter 77: 用 numpy 向量化 rolling max/min, 替换每根 bar 都创建 Python list slice
    # 之前 window=3 时每次 analyze_peaks_troughs 调用创建 ~2016 个临时 list
    # numpy 路径仅分配 2 个 array + 2 个滚动结果
    highs_arr = np.asarray(highs, dtype=float)
    lows_arr = np.asarray(lows, dtype=float)
    # pandas rolling(center=True) 严格等价于 "i 与两侧 window 内的所有点比较"
    highs_s = pd.Series(highs_arr)
    lows_s = pd.Series(lows_arr)
    rolling_max = highs_s.rolling(window=2 * effective_window + 1, center=True, min_periods=1).max().to_numpy()
    rolling_min = lows_s.rolling(window=2 * effective_window + 1, center=True, min_periods=1).min().to_numpy()

    for i in range(effective_window, n):
        # 局部极大: high[i] 大于两侧窗口内的所有 high
        # iter 164: range 上限从 n - effective_window 改为 n — 之前末尾 effective_window 根
        # bar 完全不参与极值检测, 真正的低点/高点如果落在最后窗口内会被漏掉
        # (e.g. 600378 昊华科技 2026-08-03 low=37.5 被漏, 波谷误判为 07-21 的 38.56)。
        # rolling(min_periods=1) 对末尾 bar 只用左侧窗口比较, 右侧无数据不参与。
        if highs[i] == rolling_max[i]:
            # 避免紧邻重复
            if not peaks or i - peaks[-1] > effective_window:
                peaks.append(i)
        # 局部极小: low[i] 小于两侧窗口内的所有 low
        if lows[i] == rolling_min[i]:
            if not troughs or i - troughs[-1] > effective_window:
                troughs.append(i)

    # 合并极值并强制高低点交替。局部检测在噪声、平台或连续同向走势中可能
    # 产生连续的同类极值, 不能直接按索引配对, 否则会生成方向相反的段。
    all_extremes: List[Tuple[int, str]] = [
        *((p, "high") for p in peaks),
        *((t, "low") for t in troughs),
    ]
    all_extremes.sort(key=lambda item: (item[0], 0 if item[1] == "high" else 1))

    # 从年度高点开始运行交替状态机:
    # - 首个有效点必须是 <= 年度高点的 low;
    # - low 后只接受不低于它的 high;
    # - high 后只接受不高于它的 low;
    # 同类极值则保留更极端的那个, 反向或无效候选直接忽略。
    accepted: List[Tuple[int, str]] = []
    for idx, kind in all_extremes:
        values = lows if kind == "low" else highs
        price = float(values[idx])

        if not accepted:
            if kind == "low" and price <= year_high_price:
                accepted.append((idx, kind))
            continue

        previous_idx, previous_kind = accepted[-1]
        previous_values = lows if previous_kind == "low" else highs
        previous_price = float(previous_values[previous_idx])

        if kind == previous_kind:
            is_more_extreme = (
                price < previous_price if kind == "low" else price > previous_price
            )
            if is_more_extreme:
                accepted[-1] = (idx, kind)
            continue

        # Opposite extrema are accepted only when their movement has the
        # expected sign. Equality is valid and represents a zero-length swing.
        if previous_kind == "low" and kind == "high" and price >= previous_price:
            accepted.append((idx, kind))
        elif previous_kind == "high" and kind == "low" and price <= previous_price:
            accepted.append((idx, kind))

    # 将最后收盘价作为尚未确认的极值纳入状态机, 同时把仍在延续的
    # 最后一个 low 合并到当前价, 避免产生两个连续 decline 段。
    current_price = float(after_high.iloc[-1]["close"])
    current_idx = n - 1
    if accepted:
        previous_idx, previous_kind = accepted[-1]
        previous_values = lows if previous_kind == "low" else highs
        previous_price = float(previous_values[previous_idx])
        # iter 164: current_idx == previous_idx (最后一根已是滑动窗口识别的极值) 时不重复
        # 添加 — 之前末根被识别为 high 时会再 append 同 idx 的 low, 产生零长度 segment
        # (e.g. 600378 修改后多出 decline 08-05->08-05 44.07->44.07)。
        if previous_kind == "high":
            if current_price <= previous_price and current_idx != previous_idx:
                accepted.append((current_idx, "low"))
            elif current_idx != previous_idx:
                accepted[-1] = (current_idx, "high")
        elif current_price >= previous_price and current_idx != previous_idx:
            accepted.append((current_idx, "high"))
        elif current_idx != previous_idx:
            accepted[-1] = (current_idx, "low")
    elif current_price <= year_high_price:
        accepted.append((current_idx, "low"))

    segments: List[PeakTroughSegment] = []
    prev_price = year_high_price
    prev_date = year_high_date
    prev_is_high = True

    for idx, kind in accepted:
        values = lows if kind == "low" else highs
        curr_price = float(values[idx]) if idx != current_idx else current_price
        curr_date = after_high.iloc[idx]["date_dt"].strftime("%Y-%m-%d")
        change = (curr_price - prev_price) / prev_price * 100 if prev_price else 0.0
        # iter 83: NaN/Inf guard — pandas NaN 泄漏会让 round() 返回 NaN, Pydantic v2
        # model_dump() 会序列化为字符串 "NaN", 前端 JSON.parse 直接挂掉。
        # math.isfinite 同时挡掉 NaN 和 ±Inf, 退化到 0.0。
        if not math.isfinite(change):
            change = 0.0

        if kind == "low":
            # The state machine guarantees a non-positive decline.
            change = min(change, 0.0)
            segment_type = "decline"
            from_peak = prev_is_high
        else:
            # The state machine guarantees a non-negative rebound.
            change = max(change, 0.0)
            segment_type = "rebound"
            from_peak = False

        segments.append(
            PeakTroughSegment(
                type=segment_type,
                from_date=prev_date,
                to_date=curr_date,
                start_price=round(prev_price, 4),
                end_price=round(curr_price, 4),
                change_pct=round(change, 2),
                from_peak=from_peak,
            )
        )
        prev_price = curr_price
        prev_date = curr_date
        prev_is_high = kind == "high"

    # 计算 summary
    declines = [s for s in segments if s.type == "decline"]
    rebounds = [s for s in segments if s.type == "rebound"]
    max_decline = min((s.change_pct for s in declines), default=0.0)
    max_rebound = max((s.change_pct for s in rebounds), default=0.0)
    current_vs_high = (current_price - year_high_price) / year_high_price * 100 if year_high_price else 0.0
    # 修复 MEDIUM: 区分 year_start vs year_high
    year_start_price = float(this_year_df.iloc[0]["close"])
    current_vs_year_start = (
        (current_price - year_start_price) / year_start_price * 100 if year_start_price else 0.0
    )

    logger.info(
        "peak_trough %s/%s year_high=%.2f on %s, %d segments (decline=%d rebound=%d), max_d=%.2f%% max_r=%.2f%%",
        stock_code, period, year_high_price, year_high_date,
        len(segments), len(declines), len(rebounds), max_decline, max_rebound,
    )

    return PeakTroughResponse(
        stock_code=stock_code,
        stock_name=stock_name,
        period=period,
        year_high=YearHigh(date=year_high_date, price=round(year_high_price, 4)),
        year_start_date=this_year_df.iloc[0]["date_dt"].strftime("%Y-%m-%d"),
        analysis_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        segments=segments,
        summary=PeakTroughSummary(
            max_decline_pct=round(max_decline, 2),
            max_rebound_pct=round(max_rebound, 2),
            current_vs_high_pct=round(current_vs_high, 2),
            current_vs_year_start_pct=round(current_vs_year_start, 2),
            current_price=round(current_price, 4),
            year_start_price=round(year_start_price, 4),
            segment_count=len(segments),
            decline_count=len(declines),
            rebound_count=len(rebounds),
        ),
    )