"""美股 (US) 备选数据源:Finnhub + AlphaVantage + Longbridge。

借鉴 v2 daily_stock_analysis_v2/data_provider/{finnhub,alphavantage,longbridge}_fetcher.py
的设计: 当 yfinance 被限流且无解时, 这些 API 提供了独立于 Yahoo 的访问路径。

设计原则:
  - 全部读 env / .env 配置 API key, 缺失时 fetcher 自动跳过 (不抛)
  - 仅作为美股 fallback; A股/港股路径不变
  - 不引入第三方 SDK (finnhub-python 等), 直接用 requests 调 REST API
    减少依赖, 与 v1 极简风格一致

启用方式 (env):
  FINNHUB_API_KEY=...        # https://finnhub.io/register  (free 60 calls/min)
  ALPHAVANTAGE_API_KEY=...   # https://www.alphavantage.co/support/#api-key  (free 25/day, 5/min)
  LONGBRIDGE_APP_KEY=...     # https://open.longbridge.com/  (需 OAuth, 不在本模块)
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import List, Optional

import pandas as pd

from schemas.stocks import KLineData

logger = logging.getLogger(__name__)

# Finnhub
_FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
_FINNHUB_TIMEOUT = 15
# Finnhub 沙箱规则: 0.3-0.8s 随机 sleep 防触发 60/min 限流
_FINNHUB_SLEEP_RANGE = (0.3, 0.8)

# AlphaVantage
_ALPHAVANTAGE_BASE_URL = "https://www.alphavantage.co/query"
_ALPHAVANTAGE_TIMEOUT = 30
# AlphaVantage 沙箱规则: 0.5-1.5s 随机 sleep 防触发 5/min 限流
_ALPHAVANTAGE_SLEEP_RANGE = (0.5, 1.5)


def _read_env(name: str) -> Optional[str]:
    """读 env var, strip 空白, 空字符串返回 None。"""
    val = os.environ.get(name, "").strip()
    return val or None


def _finnhub_api_key() -> Optional[str]:
    return _read_env("FINNHUB_API_KEY")


def _alphavantage_api_key() -> Optional[str]:
    return _read_env("ALPHAVANTAGE_API_KEY")


def _normalize_us_df(
    df: pd.DataFrame,
    source: str,
    expected_cols: List[str],
) -> List[KLineData]:
    """将第三方 API 返回的 DataFrame 归一化为 KLineData 列表。

    expected_cols: 来源列名 (英文), 应至少包含 open/high/low/close/volume + 时间列。
    """
    if df is None or df.empty:
        return []
    df = df.copy().reset_index(drop=True)
    if "date" not in df.columns:
        # 某些 API 把日期放在 index
        if df.index.name and "date" in str(df.index.name).lower():
            df["date"] = df.index.astype(str)
        else:
            raise ValueError(f"{source} 数据缺少 date 列: {list(df.columns)}")
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            raise ValueError(f"{source} 数据缺少 {col} 列: {list(df.columns)}")
    records = df.to_dict("records")
    result: List[KLineData] = []
    for r in records:
        try:
            result.append(
                KLineData(
                    date=str(r["date"])[:10],  # YYYY-MM-DD
                    open=float(r["open"]) if pd.notna(r["open"]) else 0.0,
                    high=float(r["high"]) if pd.notna(r["high"]) else 0.0,
                    low=float(r["low"]) if pd.notna(r["low"]) else 0.0,
                    close=float(r["close"]) if pd.notna(r["close"]) else 0.0,
                    volume=float(r["volume"]) if pd.notna(r["volume"]) else 0.0,
                    amount=0.0,
                    change_percent=0.0,
                )
            )
        except (ValueError, TypeError) as exc:
            logger.debug("%s 单行解析失败跳过: %s", source, exc)
            continue
    return result


def fetch_us_finnhub(
    code: str, period: str, days: int
) -> Optional[List[KLineData]]:
    """从 Finnhub 拉取美股日 K 线 (resolution='D')。

    失败 / 无 API key / 非日 K 周期 / 非美股 → 返回 None, 让调用方继续走下家。
    """
    api_key = _finnhub_api_key()
    if not api_key:
        logger.debug("Finnhub API key 未配置, 跳过")
        return None
    if period != "daily":
        # Finnhub 的 resolution 支持 D/W/M 但本接口只暴露日 K, 周/月仍走 yfinance
        return None

    try:
        import requests
    except ImportError:
        logger.warning("requests 模块未安装, Finnhub fetcher 不可用")
        return None

    symbol = code.strip().upper()
    end_ts = int(time.time())
    start_ts = end_ts - days * 86400
    url = f"{_FINNHUB_BASE_URL}/stock/candle"
    params = {
        "symbol": symbol,
        "resolution": "D",
        "from": start_ts,
        "to": end_ts,
        "token": api_key,
    }
    try:
        time.sleep(0.3)  # 简化的 rate-limit 防护
        resp = requests.get(url, params=params, timeout=_FINNHUB_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Finnhub 请求失败 (%s %s): %s", code, period, exc)
        return None

    if data.get("s") != "ok" or not data.get("c"):
        logger.info("Finnhub 无数据 (%s %s): s=%s", code, period, data.get("s"))
        return None

    df = pd.DataFrame({
        "date": [datetime.fromtimestamp(t).strftime("%Y-%m-%d") for t in data["t"]],
        "open": data["o"],
        "high": data["h"],
        "low": data["l"],
        "close": data["c"],
        "volume": data["v"],
    })
    return _normalize_us_df(df, "finnhub", ["open", "high", "low", "close", "volume"])


def fetch_us_alphavantage(
    code: str, period: str, days: int
) -> Optional[List[KLineData]]:
    """从 AlphaVantage 拉取美股日 K 线 (TIME_SERIES_DAILY)。

    免费 tier 25 calls/day, 5 calls/min — 仅作为最后兜底, 正常使用不应触达。
    """
    api_key = _alphavantage_api_key()
    if not api_key:
        logger.debug("AlphaVantage API key 未配置, 跳过")
        return None
    if period != "daily":
        return None

    try:
        import requests
    except ImportError:
        logger.warning("requests 模块未安装, AlphaVantage fetcher 不可用")
        return None

    symbol = code.strip().upper()
    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": symbol,
        "outputsize": "compact",  # compact = 最近 100 个交易日
        "apikey": api_key,
    }
    try:
        time.sleep(0.5)
        resp = requests.get(
            _ALPHAVANTAGE_BASE_URL, params=params, timeout=_ALPHAVANTAGE_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("AlphaVantage 请求失败 (%s %s): %s", code, period, exc)
        return None

    if "Note" in data:
        # API 限流提示: "Thank you for using Alpha Vantage! ... call frequency will be..."
        logger.info("AlphaVantage 限流: %s", data["Note"][:200])
        return None
    if "Error Message" in data:
        logger.info("AlphaVantage API 错误 (%s): %s", symbol, data["Error Message"])
        return None

    ts_key = "Time Series (Daily)"
    if ts_key not in data or not data[ts_key]:
        return None

    from datetime import timedelta
    cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = []
    for date_str, values in data[ts_key].items():
        # AlphaVantage 返回 100 天, days>100 时裁到 days 天
        if date_str < cutoff_date:
            continue
        rows.append({
            "date": date_str,
            "open": float(values.get("1. open", 0)),
            "high": float(values.get("2. high", 0)),
            "low": float(values.get("3. low", 0)),
            "close": float(values.get("4. close", 0)),
            "volume": float(values.get("5. volume", 0)),
        })
    if not rows:
        return None
    # AlphaVantage 默认 newest-first, 升序排后取末尾 days 天
    rows.sort(key=lambda r: r["date"])
    rows = rows[-days:]

    df = pd.DataFrame(rows)
    return _normalize_us_df(df, "alphavantage", ["open", "high", "low", "close", "volume"])
