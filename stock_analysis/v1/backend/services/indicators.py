"""技术指标计算: MACD, BOLL, KDJ。

所有指标基于已获取的 K线序列计算(避免重复请求数据源)。
返回格式与 lightweight-charts 兼容(time + value 数组)。
"""
from __future__ import annotations

from typing import List

import pandas as pd

from schemas.stocks import KLineData

# iter 86: KDJ RSV divisor lower-bound — 防止 high_n == low_n 时除以 0 产生极端值
KDJ_SPREAD_EPSILON = 1e-10


def _bars_to_df(data: List[KLineData]) -> pd.DataFrame:
    if not data:
        raise ValueError("指标计算需要至少 1 个数据点")
    df = pd.DataFrame([d.model_dump() for d in data])
    df["date_dt"] = pd.to_datetime(df["date"])
    return df.sort_values("date_dt").reset_index(drop=True)


def compute_macd(
    data: List[KLineData],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict:
    """MACD: 12-26-EMA 差值 (DIF), DEA (DIF 的 9-EMA), HIST (DIF - DEA)。"""
    df = _bars_to_df(data)
    ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = (dif - dea) * 2

    def to_list(series: pd.Series) -> List[dict]:
        return [
            {"time": d.date_dt.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
            for d, v in zip(df.itertuples(), series)
        ]

    return {
        "indicator": "MACD",
        "params": {"fast": fast, "slow": slow, "signal": signal},
        "series": {
            "dif": to_list(dif),
            "dea": to_list(dea),
            "hist": to_list(hist),
        },
    }


def compute_boll(
    data: List[KLineData],
    period: int = 20,
    std_dev: float = 2.0,
) -> dict:
    """布林带: 中轨 = N日 MA, 上下轨 = 中轨 ± N_std。"""
    df = _bars_to_df(data)
    ma = df["close"].rolling(window=period).mean()
    std = df["close"].rolling(window=period).std(ddof=0)

    def to_list(series: pd.Series) -> List[dict]:
        out = []
        for d, v in zip(df.itertuples(), series):
            if pd.isna(v):
                continue
            out.append({"time": d.date_dt.strftime("%Y-%m-%d"), "value": round(float(v), 4)})
        return out

    return {
        "indicator": "BOLL",
        "params": {"period": period, "std_dev": std_dev},
        "series": {
            "mid": to_list(ma),
            "upper": to_list(ma + std_dev * std),
            "lower": to_list(ma - std_dev * std),
        },
    }


def compute_kdj(
    data: List[KLineData],
    n: int = 9,
    m1: int = 3,
    m2: int = 3,
) -> dict:
    """KDJ: RSV = (C - L_n) / (H_n - L_n) * 100, K = SMA(RSV, m1), D = SMA(K, m2), J = 3K - 2D。"""
    df = _bars_to_df(data)
    low_n = df["low"].rolling(window=n, min_periods=1).min()
    high_n = df["high"].rolling(window=n, min_periods=1).max()
    # iter 69: 用 eps 兜底而非 `.replace(0, 1)` — 后者只替精确 0, 浮点极小值 (e.g. 1e-15)
    # 仍会产生极端 RSV (除以接近 0 的数 → 巨大值, 平滑后污染 K/D/J)。
    # iter 86: 提取为命名常量 KDJ_SPREAD_EPSILON (避免 1e-10 散落在多处的 magic number)。
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

    def to_list(series: pd.Series) -> List[dict]:
        return [
            {"time": d.date_dt.strftime("%Y-%m-%d"), "value": round(float(v), 4)}
            for d, v in zip(df.itertuples(), series)
        ]

    return {
        "indicator": "KDJ",
        "params": {"n": n, "m1": m1, "m2": m2},
        "series": {
            "k": to_list(k_series),
            "d": to_list(d_series),
            "j": to_list(j_series),
        },
    }


def compute_indicator(
    data: List[KLineData],
    indicator: str,
) -> dict:
    """统一入口。"""
    if indicator.upper() == "MACD":
        return compute_macd(data)
    if indicator.upper() == "BOLL":
        return compute_boll(data)
    if indicator.upper() == "KDJ":
        return compute_kdj(data)
    raise ValueError(f"不支持的指标: {indicator} (可用: MACD, BOLL, KDJ)")
