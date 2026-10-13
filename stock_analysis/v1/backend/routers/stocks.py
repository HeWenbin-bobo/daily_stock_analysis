"""Stocks API router。

端点:
  GET /api/v1/stocks/search?q=600519
  GET /api/v1/stocks/{code}/quote
  GET /api/v1/stocks/{code}/history?period=daily|weekly|monthly&days=365
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, Response
import pandas as pd
# NOTE: yfinance kept at module level for test monkeypatch compatibility
# (tests/test_quote.py uses `monkeypatch.setattr(stocks_router.yf, "download", ...)`).
# Lazy import inside get_quote() would force every test to redo the import via mock
# and breaks the existing test contract. ~10MB startup cost accepted.
import yfinance as yf

from schemas.stocks import (
    IndicatorType,
    PeriodType,
    StockHistoryResponse,
    StockQuote,
    StockSearchResponse,
)
from services.cache import quantize_days
from services.config import is_force_mock
from services.data_provider import (
    _hk_yfinance_ticker,
    detect_market,
    fetch_history,
    guess_name,
    normalize_code,
    SEARCH_STOCKS_LIMIT,
    search_stocks,
)
from services.indicators import compute_indicator
from middlewares.rate_limit import rate_limiter
from security import (
    SEARCH_Q_MAX_LENGTH,
    normalize_and_validate_stock_code,
    validate_search_q,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/stocks", tags=["stocks"])


def _pad_hk_code(code: str) -> str:
    """港股代码保留前导 0: "700" -> "00700", "12345" -> "12345"。"""
    if code.isdigit():
        return code.zfill(5)
    return code


# iter 111: /indicators 端点限流 — 比 LLM 宽松 (LLM 5/分钟因有外部费用),
# 但仍阻止单 IP 刷量耗尽 CPU (numpy/pandas 计算比纯查询贵)。
INDICATOR_RATE_LIMIT_MAX_CALLS = 30
INDICATOR_RATE_LIMIT_WINDOW_SECONDS = 60.0
# iter 135: /search 限流 — 之前未限流, 攻击者可枚举整个静态 hints dict (476 条)
# 拖走 cn/hk/us 三市场所有股票代码 + 名称。60 次/分钟/IP 已远超正常使用 (搜索框防抖
# 300ms, 用户每次输入平均只触发 1-3 次请求), 又能阻止脚本化字典攻击。
SEARCH_RATE_LIMIT_MAX_CALLS = 60
SEARCH_RATE_LIMIT_WINDOW_SECONDS = 60.0

# iter 155: days 量化桶 + quantize_days() 已迁移到 services/cache.py — 那是它们
# 真正的归属 (cache key 稳定性), 不是 router 职责。跨 router 共享 utility 应当走
# services 层, 避免 routers/analysis.py 之前那种 from routers.stocks import _private 的耦合。


def _normalize_quote_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """校验并归一化 yfinance 行情表, 拒绝不安全的列和索引结构。"""
    normalized = df.copy()
    if isinstance(normalized.columns, pd.MultiIndex):
        normalized.columns = [column[0] for column in normalized.columns]
    normalized.columns = [str(column).lower() for column in normalized.columns]

    required_columns = {"open", "high", "low", "close", "volume"}
    if normalized.columns.has_duplicates or not required_columns.issubset(
        normalized.columns
    ):
        raise ValueError("yfinance 行情列结构无效")
    if not isinstance(normalized.index, pd.DatetimeIndex):
        raise ValueError("yfinance 行情索引必须为 DatetimeIndex")
    return normalized


@router.get("/search", response_model=StockSearchResponse)
async def search(
    request: Request,
    q: str = Query("", max_length=SEARCH_Q_MAX_LENGTH, description="搜索关键字(代码/名称)"),
) -> dict:
    """股票搜索筛选。"""
    # iter 135: 限流先于参数校验 — 防止攻击者用超大 q 探测也能消耗配额。
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = rate_limiter.is_allowed(
        f"search:{client_ip}",
        max_calls=SEARCH_RATE_LIMIT_MAX_CALLS,
        window_seconds=SEARCH_RATE_LIMIT_WINDOW_SECONDS,
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"搜索请求过于频繁, 请 {retry_after:.0f} 秒后重试 (上限 {SEARCH_RATE_LIMIT_MAX_CALLS} 次/{SEARCH_RATE_LIMIT_WINDOW_SECONDS:.0f}秒/IP)",
        )
    q = validate_search_q(q)
    # iter 163: to_thread — 首次搜索会触发全量 A股列表 (akshare, ~5500 只) 懒加载,
    # 约 3-5s。在 async handler 里直接调用会冻结事件循环, 阻塞其它请求。推到线程池。
    items = await asyncio.to_thread(search_stocks, q, SEARCH_STOCKS_LIMIT)
    return {"items": items}


@router.get("/{stock_code}/indicators/{indicator}")
async def get_indicators(
    request: Request,
    response: Response,
    stock_code: str,
    # iter 98: 提前用 Enum 类做路径参数验证 — 之前是 str 让 compute_indicator 内部
    # ValueError 才拒绝, 既浪费 fetch_history + 缓存写, 又走完整 500 路径。
    # FastAPI 会自动把不匹配的 indicator 转换为 422 (validation error)。
    indicator: IndicatorType,
    period: PeriodType = Query("daily"),
    days: int = Query(365, ge=30, le=2000),
) -> dict:
    """计算技术指标 (MACD / BOLL / KDJ)。

    利用 K线缓存, 同一股票+周期+天数 的指标无需重复拉取数据。

    iter 111: 加 rate limit — 之前只有 /llm-insight 有限流, /indicators 计算指标
    (numpy/pandas) 比纯查询贵, 暴露给无限流攻击。30 次/分钟/IP — 比 LLM 宽松
    (LLM 5/分钟因有外部费用), 但仍阻止刷量。
    """
    # 限流先于参数校验, 避免攻击者用无效 indicator 探测 422 也消耗配额
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = rate_limiter.is_allowed(
        f"indicator:{client_ip}",
        max_calls=INDICATOR_RATE_LIMIT_MAX_CALLS,
        window_seconds=INDICATOR_RATE_LIMIT_WINDOW_SECONDS,
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=f"指标请求过于频繁, 请 {retry_after:.0f} 秒后重试 (上限 {INDICATOR_RATE_LIMIT_MAX_CALLS} 次/{INDICATOR_RATE_LIMIT_WINDOW_SECONDS:.0f}秒/IP)",
            # iter 115: 用 ceil 替代 int — 之前 int(retry_after) + 1 在 retry_after < 1
            # (e.g. 0.001s floor) 时退化成 Retry-After: 1, 客户端立刻再试触发 429 风暴。
            # ceil(0.001) = 1, ceil(0.4) = 1, ceil(1.2) = 2 — 始终反映真实等待。
            headers={"Retry-After": str(math.ceil(retry_after))},
        )

    stock_code = normalize_and_validate_stock_code(stock_code)
    days = quantize_days(days)
    try:
        data, source, market, name = fetch_history(stock_code, period, days)
        if not data:
            raise HTTPException(status_code=404, detail=f"未取到 {stock_code} 数据")
        result = compute_indicator(data, indicator)
        # 浏览器/CDN 缓存提示: 与 server-side kline_cache TTL 对齐 (iter 63 perf)
        response.headers["Cache-Control"] = "private, max-age=300"
        return result
    except ValueError as exc:
        # 不向响应泄露内部异常文本 (可能含数据源列名); 完整堆栈写日志
        rid = getattr(request.state, "request_id", "-")
        logger.warning("指标计算 ValueError rid=%s for %s: %s", rid, stock_code, exc)
        raise HTTPException(status_code=400, detail="请求参数无效")
    except HTTPException:
        raise
    except Exception:
        # 不向响应泄露内部异常文本; 完整堆栈写日志
        rid = getattr(request.state, "request_id", "-")
        logger.exception("指标计算失败 rid=%s for %s", rid, stock_code)
        raise HTTPException(status_code=500, detail="指标计算失败")


@router.get("/{stock_code}/quote", response_model=StockQuote)
async def get_quote(stock_code: str, request: Request) -> dict:
    """获取实时行情(基于最近一根 K线 + 前一根计算涨跌)。

    FORCE_MOCK=1 (默认) 时走 fetch_history 的 mock 路径, 不调用 yfinance,
    避免本地开发触发外部请求 / rate limit。

    注 (iter 64): 当前前端从 history 派生 quote-like 字段, 本端点暂无 consumer。
    已通过 20 个测试覆盖; 后续若前端需要可零成本接入。
    """
    stock_code = normalize_and_validate_stock_code(stock_code)
    market = detect_market(stock_code)
    clean = normalize_code(stock_code, market)
    # 港股代码保留前导 0 (例: "700" -> "00700"), 响应字段与 yfinance ticker 不冲突
    if market == "hk":
        clean = _pad_hk_code(clean)

    # ---- FORCE_MOCK 路径: 复用 fetch_history 走 mock, 不调用 yfinance ----
    if is_force_mock():
        try:
            data, _source, _market, name = fetch_history(stock_code, "daily", 30)
            if not data:
                raise HTTPException(
                    status_code=404,
                    detail=f"未取到 {stock_code} 行情",
                )
            last = data[-1]
            prev = data[-2] if len(data) >= 2 else last
            current = float(last.close)
            prev_close = float(prev.close)
            change = current - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0.0
            try:
                update_time = datetime.fromisoformat(last.date)
            except ValueError:
                # iter 67: datetime.utcnow() 在 Python 3.12+ deprecated; 用 timezone-aware UTC 然后去 tz
                update_time = datetime.now(timezone.utc).replace(tzinfo=None)
            return {
                "stock_code": clean,
                "stock_name": name,
                "market": market,
                "current_price": round(current, 4),
                "change": round(change, 4),
                "change_percent": round(change_pct, 2),
                "open": round(float(last.open), 4),
                "high": round(float(last.high), 4),
                "low": round(float(last.low), 4),
                "prev_close": round(prev_close, 4),
                "volume": float(last.volume) if pd.notna(last.volume) else 0.0,
                "amount": 0.0,
                "update_time": update_time,
            }
        except HTTPException:
            raise
        except Exception:
            # 不向响应泄露内部异常文本; 完整堆栈写日志
            # iter 132: 加 rid= — 与同文件 /indicators /history rid= 模式一致, 跨 middleware 日志可串联。
            rid = getattr(request.state, "request_id", "-")
            logger.exception("/quote (mock) failed rid=%s for %s", rid, stock_code)
            raise HTTPException(status_code=500, detail="获取行情失败")

    # ---- 真实行情路径: 调用 yfinance ----
    try:
        if market == "us":
            ticker = clean.upper()
        elif market == "hk":
            ticker = _hk_yfinance_ticker(clean)
        else:
            suffix = ".SS" if clean.startswith("6") else ".SZ"
            ticker = f"{clean}{suffix}"

        # iter 82: timeout=10 + asyncio.to_thread — yfinance 是阻塞调用, 在 async handler 里直接
        # 调用会冻结 FastAPI worker 的事件循环, 导致该 worker 其它所有请求排队等待。
        # to_thread 把阻塞调用推到线程池, 主事件循环可继续派发其它请求。
        df = await asyncio.to_thread(
            yf.download,
            ticker,
            period="5d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            timeout=10,
        )
        # iter 72: 类型检查必须先于 .empty (dict 没有 .empty 属性, 会抛 AttributeError)
        if df is None:
            raise HTTPException(status_code=404, detail=f"未取到 {stock_code} 行情")
        if not isinstance(df, pd.DataFrame):
            # iter 134: 加 rid= — 与同文件 mock 路径 (line 226) + /indicators /history 模式一致
            rid = getattr(request.state, "request_id", "-")
            logger.warning("Invalid yfinance quote response rid=%s for %s", rid, stock_code)
            raise HTTPException(status_code=502, detail="行情数据格式异常")
        if df.empty:
            raise HTTPException(status_code=404, detail=f"未取到 {stock_code} 行情")
        try:
            df = _normalize_quote_dataframe(df)
        except ValueError:
            # iter 134: 加 rid=
            rid = getattr(request.state, "request_id", "-")
            logger.warning("Invalid yfinance quote data rid=%s for %s", rid, stock_code)
            raise HTTPException(status_code=502, detail="行情数据格式异常")

        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else last
        name = guess_name(clean, market)

        current = float(last["close"])
        prev_close = float(prev["close"])
        change = current - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0.0

        return {
            "stock_code": clean,
            "stock_name": name,
            "market": market,
            "current_price": round(current, 4),
            "change": round(change, 4),
            "change_percent": round(change_pct, 2),
            "open": round(float(last["open"]), 4),
            "high": round(float(last["high"]), 4),
            "low": round(float(last["low"]), 4),
            "prev_close": round(prev_close, 4),
            "volume": float(last["volume"]) if pd.notna(last["volume"]) else 0.0,
            "amount": 0.0,
            # iter 72: pandas>=2.0 保留 tz 后 to_pydatetime() 返回 aware datetime,
            # StockQuote.update_time 字段是 naive, Pydantic 会抛 ValidationError.
            "update_time": df.index[-1].to_pydatetime().replace(tzinfo=None),
        }
    except HTTPException:
        raise
    except Exception:
        # 不向响应泄露内部异常文本; 完整堆栈写日志
        # iter 134: 加 rid= — 与同文件 mock 路径 (line 226) 模式一致, 完成 /quote 全路径 rid 串联
        rid = getattr(request.state, "request_id", "-")
        logger.exception("/quote (yfinance) failed rid=%s for %s", rid, stock_code)
        raise HTTPException(status_code=500, detail="获取行情失败")


@router.get("/{stock_code}/history", response_model=StockHistoryResponse)
async def get_history(
    request: Request,
    response: Response,
    stock_code: str,
    period: PeriodType = Query("daily", description="周期: daily/weekly/monthly"),
    days: int = Query(365, ge=10, le=2000, description="回看天数"),
) -> dict:
    """获取 K 线历史数据。"""
    stock_code = normalize_and_validate_stock_code(stock_code)
    days = quantize_days(days)
    try:
        data, source, market, name = fetch_history(stock_code, period, days)
        clean = normalize_code(stock_code, market)
        # 浏览器/CDN 缓存提示: 与 server-side kline_cache TTL 对齐 (iter 63 perf)
        response.headers["Cache-Control"] = "private, max-age=300"
        return {
            "stock_code": clean,
            "stock_name": name,
            "market": market,
            "period": period,
            "data": [d.model_dump() for d in data],
            "source": source,
        }
    except ValueError as exc:
        # 不向响应泄露内部异常文本 (可能含数据源列名); 完整堆栈写日志
        # iter 119: 加 rid= — 与 /peak-trough / /indicators / /llm-insight / /llm-status
        # 对齐。iter 115 漏了 /history, 让运维拼该 endpoint 的日志要靠时间戳匹配。
        rid = getattr(request.state, "request_id", "-")
        logger.warning("获取历史数据 ValueError rid=%s for %s: %s", rid, stock_code, exc)
        raise HTTPException(status_code=400, detail="请求参数无效")
    except Exception:
        # 不向响应泄露内部异常文本; 完整堆栈写日志
        # iter 119: 同样加 rid= — 与 iter 115 模式一致。
        rid = getattr(request.state, "request_id", "-")
        logger.exception("获取历史数据失败 rid=%s for %s", rid, stock_code)
        raise HTTPException(status_code=500, detail="获取历史数据失败")