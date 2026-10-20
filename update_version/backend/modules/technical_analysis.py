# -*- coding: utf-8 -*-
"""
技术分析模块 —— 从 stock_analysis/v1 移植而来，适配 update_version 的 dict 数据格式。

包含：
  - 波峰波谷识别算法（analyze_peaks_troughs）
  - 技术指标计算（MACD / BOLL / KDJ）

与 v1 的差异：
  - v1 使用 Pydantic 模型（KLineData / PeakTroughResponse），v2 使用纯 dict，
    便于与 data_source.fetch_kline_with_fallback 返回的 dict list 直接对接。
  - 算法逻辑与 v1 完全一致（滑动窗口识别局部极值 + 交替状态机 + 年度高点）。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# KDJ RSV divisor lower-bound，防止 high_n == low_n 时除以 0 产生极端值
KDJ_SPREAD_EPSILON = 1e-10

# 支持的周期（与 data_source 对齐）
VALID_PERIODS = ("daily", "weekly", "monthly", "quarterly", "yearly")
VALID_INDICATORS = ("MACD", "BOLL", "KDJ")


def _bars_to_df(data: List[Dict[str, Any]]) -> pd.DataFrame:
    """把 dict list 转成按时间升序的 DataFrame（指标计算用）。"""
    if not data:
        raise ValueError("数据点不足")
    df = pd.DataFrame(data)
    df["date_dt"] = pd.to_datetime(df["date"])
    return df.sort_values("date_dt").reset_index(drop=True)


# ============================================================================
# 波峰波谷分析
# ============================================================================
def analyze_peaks_troughs(
    stock_code: str,
    stock_name: str,
    period: str,
    data: List[Dict[str, Any]],
    window: int = 3,
) -> Dict[str, Any]:
    """波峰波谷分析主入口。

    Args:
        data: 按时间升序的 K 线数据（dict list，含 date/open/high/low/close）
        window: 局部极值识别窗口半径

    步骤：
        1. 取本年度所有数据
        2. 找年度最高点 year_high
        3. 从 year_high 之后用滑动窗口识别 peaks/troughs
        4. 交替状态机构建回撤/回弹段
    """
    if not data:
        raise ValueError("无数据可供分析")

    df = pd.DataFrame(data)
    df["date_dt"] = pd.to_datetime(df["date"])
    df = df.sort_values("date_dt").reset_index(drop=True)

    last_year = int(df["date_dt"].iloc[-1].year)
    this_year_df = df[df["date_dt"].dt.year == last_year].reset_index(drop=True)

    # ★ 探索期修复：`quarterly`/`yearly` 一年只有 4/1 根 K 线，永远凑不满 5 根，
    #   接口恒定 400（实测）；`monthly` 在年初、`daily` 在 1 月初也会季节性失败。
    #   数据点不足时回退到**全量数据**（仍然是有意义的一段区间），而不是直接报错；
    #   只有全量也不足时才失败。
    if len(this_year_df) < 5:
        if len(df) < 5:
            raise ValueError("本年度数据点不足，无法分析")
        logger.info(
            "peaks/troughs: 本年度仅 %d 根（period=%s），回退到全量 %d 根",
            len(this_year_df), period, len(df),
        )
        this_year_df = df.copy()

    high_idx = int(this_year_df["high"].idxmax())
    year_high_date = this_year_df.loc[high_idx, "date_dt"].strftime("%Y-%m-%d")
    year_high_price = float(this_year_df.loc[high_idx, "high"])

    after_high = this_year_df.iloc[high_idx:].reset_index(drop=True)
    year_start_price = float(this_year_df.iloc[0]["close"])

    def _summary(current_price: float, segments: List[Dict[str, Any]]) -> Dict[str, Any]:
        declines = [s for s in segments if s["type"] == "decline"]
        rebounds = [s for s in segments if s["type"] == "rebound"]
        max_decline = min((s["change_pct"] for s in declines), default=0.0)
        max_rebound = max((s["change_pct"] for s in rebounds), default=0.0)
        current_vs_high = (
            (current_price - year_high_price) / year_high_price * 100 if year_high_price else 0.0
        )
        current_vs_year_start = (
            (current_price - year_start_price) / year_start_price * 100 if year_start_price else 0.0
        )
        return {
            "max_decline_pct": round(max_decline, 2),
            "max_rebound_pct": round(max_rebound, 2),
            "current_vs_high_pct": round(current_vs_high, 2),
            "current_vs_year_start_pct": round(current_vs_year_start, 2),
            "current_price": round(current_price, 4),
            "year_start_price": round(year_start_price, 4),
            "segment_count": len(segments),
            "decline_count": len(declines),
            "rebound_count": len(rebounds),
        }

    if len(after_high) < 3:
        current_price = float(after_high.iloc[-1]["close"]) if not after_high.empty else year_high_price
        return {
            "stock_code": stock_code,
            "stock_name": stock_name,
            "period": period,
            "year_high": {"date": year_high_date, "price": round(year_high_price, 4)},
            "year_start_date": this_year_df.iloc[0]["date_dt"].strftime("%Y-%m-%d"),
            "analysis_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "segments": [],
            "summary": _summary(current_price, []),
            "effective_window": window,
        }

    highs = after_high["high"].values
    lows = after_high["low"].values
    n = len(after_high)

    # 数据量不足时自动收缩 window
    if n < 2 * window + 1:
        effective_window = max(1, (n - 1) // 2)
        logger.warning(
            "peak_trough 数据点不足 (n=%d, requested window=%d), 自动收缩到 window=%d",
            n, window, effective_window,
        )
    else:
        effective_window = window

    highs_arr = np.asarray(highs, dtype=float)
    lows_arr = np.asarray(lows, dtype=float)
    highs_s = pd.Series(highs_arr)
    lows_s = pd.Series(lows_arr)
    rolling_max = highs_s.rolling(window=2 * effective_window + 1, center=True, min_periods=1).max().to_numpy()
    rolling_min = lows_s.rolling(window=2 * effective_window + 1, center=True, min_periods=1).min().to_numpy()

    peaks: List[int] = []
    troughs: List[int] = []
    for i in range(effective_window, n):
        if highs[i] == rolling_max[i]:
            if not peaks or i - peaks[-1] > effective_window:
                peaks.append(i)
        if lows[i] == rolling_min[i]:
            if not troughs or i - troughs[-1] > effective_window:
                troughs.append(i)

    all_extremes: List[Tuple[int, str]] = [
        *((p, "high") for p in peaks),
        *((t, "low") for t in troughs),
    ]
    all_extremes.sort(key=lambda item: (item[0], 0 if item[1] == "high" else 1))

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
            is_more_extreme = price < previous_price if kind == "low" else price > previous_price
            if is_more_extreme:
                accepted[-1] = (idx, kind)
            continue

        if previous_kind == "low" and kind == "high" and price >= previous_price:
            accepted.append((idx, kind))
        elif previous_kind == "high" and kind == "low" and price <= previous_price:
            accepted.append((idx, kind))

    current_price = float(after_high.iloc[-1]["close"])
    current_idx = n - 1
    if accepted:
        previous_idx, previous_kind = accepted[-1]
        previous_values = lows if previous_kind == "low" else highs
        previous_price = float(previous_values[previous_idx])
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

    segments: List[Dict[str, Any]] = []
    prev_price = year_high_price
    prev_date = year_high_date
    prev_is_high = True

    for idx, kind in accepted:
        values = lows if kind == "low" else highs
        curr_price = float(values[idx]) if idx != current_idx else current_price
        curr_date = after_high.iloc[idx]["date_dt"].strftime("%Y-%m-%d")
        change = (curr_price - prev_price) / prev_price * 100 if prev_price else 0.0
        if not math.isfinite(change):
            change = 0.0

        if kind == "low":
            change = min(change, 0.0)
            segment_type = "decline"
            from_peak = prev_is_high
        else:
            change = max(change, 0.0)
            segment_type = "rebound"
            from_peak = False

        segments.append({
            "type": segment_type,
            "from_date": prev_date,
            "to_date": curr_date,
            "start_price": round(prev_price, 4),
            "end_price": round(curr_price, 4),
            "change_pct": round(change, 2),
            "from_peak": from_peak,
        })
        prev_price = curr_price
        prev_date = curr_date
        prev_is_high = kind == "high"

    logger.info(
        "peak_trough %s/%s year_high=%.2f on %s, %d segments",
        stock_code, period, year_high_price, year_high_date, len(segments),
    )

    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "period": period,
        "year_high": {"date": year_high_date, "price": round(year_high_price, 4)},
        "year_start_date": this_year_df.iloc[0]["date_dt"].strftime("%Y-%m-%d"),
        "analysis_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "segments": segments,
        "summary": _summary(current_price, segments),
        "effective_window": effective_window,
    }


# ============================================================================
# 技术指标
# ============================================================================
def _to_list(df: pd.DataFrame, series: pd.Series, skip_nan: bool = False) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for (_, row), v in zip(df.iterrows(), series):
        if skip_nan and pd.isna(v):
            continue
        out.append({"time": row["date_dt"].strftime("%Y-%m-%d"), "value": round(float(v), 4)})
    return out


def compute_macd(data: List[Dict[str, Any]], fast: int = 12, slow: int = 26, signal: int = 9) -> Dict[str, Any]:
    df = _bars_to_df(data)
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2
    return {
        "indicator": "MACD",
        "params": {"fast": fast, "slow": slow, "signal": signal},
        "series": {
            "dif": _to_list(df, dif),
            "dea": _to_list(df, dea),
            "hist": _to_list(df, hist),
        },
    }


def compute_boll(data: List[Dict[str, Any]], period: int = 20, std_dev: float = 2.0) -> Dict[str, Any]:
    df = _bars_to_df(data)
    ma = df["close"].rolling(window=period).mean()
    std = df["close"].rolling(window=period).std(ddof=0)
    return {
        "indicator": "BOLL",
        "params": {"period": period, "std_dev": std_dev},
        "series": {
            "mid": _to_list(df, ma, skip_nan=True),
            "upper": _to_list(df, ma + std_dev * std, skip_nan=True),
            "lower": _to_list(df, ma - std_dev * std, skip_nan=True),
        },
    }


def compute_kdj(data: List[Dict[str, Any]], n: int = 9, m1: int = 3, m2: int = 3) -> Dict[str, Any]:
    df = _bars_to_df(data)
    low_n = df["low"].rolling(window=n, min_periods=1).min()
    high_n = df["high"].rolling(window=n, min_periods=1).max()
    spread = (high_n - low_n).clip(lower=KDJ_SPREAD_EPSILON)
    rsv = (df["close"] - low_n) / spread * 100
    rsv = rsv.fillna(50)

    k_vals: List[float] = []
    d_vals: List[float] = []
    prev_k, prev_d = 50.0, 50.0
    for r in rsv:
        k = (prev_k * (m1 - 1) + r) / m1
        d = (prev_d * (m2 - 1) + k) / m2
        k_vals.append(k)
        d_vals.append(d)
        prev_k, prev_d = k, d
    k_series = pd.Series(k_vals)
    d_series = pd.Series(d_vals)
    j_series = 3 * k_series - 2 * d_series
    return {
        "indicator": "KDJ",
        "params": {"n": n, "m1": m1, "m2": m2},
        "series": {
            "k": _to_list(df, k_series),
            "d": _to_list(df, d_series),
            "j": _to_list(df, j_series),
        },
    }


def compute_indicator(data: List[Dict[str, Any]], indicator: str) -> Dict[str, Any]:
    name = indicator.upper()
    if name == "MACD":
        return compute_macd(data)
    if name == "BOLL":
        return compute_boll(data)
    if name == "KDJ":
        return compute_kdj(data)
    raise ValueError(f"不支持的指标: {indicator} (可用: MACD, BOLL, KDJ)")
