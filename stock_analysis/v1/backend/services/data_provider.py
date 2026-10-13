"""数据源服务:akshare (A股/港股) + yfinance (美股/A股/港股) + mock 兜底。

参考 daily_stock_analysis_v2/data_provider 的路由思想, 简化为:
  - 6位数字开头 → A股 → akshare stock_zh_a_hist (失败时 mock 兜底)
  - 5位数字开头 → 港股 → akshare stock_hk_hist (失败时 mock 兜底)
  - 字母开头 → 美股 → yfinance (失败时 mock 兜底)

返回统一的 List[KLineData]。
"""
from __future__ import annotations

import logging
import math
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import pandas as pd

from schemas.stocks import KLineData, MarketType, PeriodType
from services.cache import kline_cache
from services.config import is_force_mock

logger = logging.getLogger(__name__)

# iter 84: yfinance `period` 参数的最小值常量 — 之前 max(days * 2, 60) 重复在 3 个调用点,
# 一旦调整需要同步改 3 处。统一到模块顶部。
YFINANCE_MIN_PERIOD_DAYS = 60
# iter 93: 拉取 A股 / 港股 时给 akshare 多要一些回看天数, 保证波峰/技术指标计算有足够上下文。
# 之前 +30 / +60 散落在两个函数体内, 调整时容易漏改一处。
AKSHARE_A_SHARE_BUFFER_DAYS = 30
AKSHARE_HK_BUFFER_DAYS = 60
# iter 93: 搜索接口的默认 limit, router 与服务层必须保持一致 (router 当前写死 20)。
SEARCH_STOCKS_LIMIT = 20


def _bars_for_days(period: PeriodType, days: int) -> int:
    """把日历天数换算成对应周期的 bar 数, 用于从全量历史数据中截取。

    iter 164: 之前直接用 ``data[-days:]``, 把 days (日历天数) 当 bar 数。对周/月线,
    选 "1年" (days=365) 会返回 365 根周K (7年) / 月K (30年), 而非 ~52 根 / 12 根。
    e.g. 美股 akshare 新浪 stock_us_daily 返回 40 年日线, resample 成周线后超量截取。
    """
    if period == "weekly":
        return max(1, days // 7)
    if period == "monthly":
        return max(1, days // 30)
    return days

# 数据源标签: 区分"用户主动 mock"与"上游失败 → mock 兜底" — 后续 UI 据此提示用户。
# iter 157: 之前所有失败都返回 "mock", 5min 缓存里塞假数据, 用户看到 AAPL 是
# 180 起步的随机价而真实价是 230+, 怀疑是 bug。其实是 Yahoo 429 → 静默 fallback。
# 新增 "mock_fallback" 让 frontend banner 能区分两种场景。
# iter 158: 新增 finnhub / alphavantage 标签 — 借鉴 v2 daily_stock_analysis_v2 的
# 第三方数据源作为 Yahoo 限流时的独立 fallback 路径。API key 缺失时 fetcher 自动跳过。
SOURCE_AKSHARE = "akshare"
SOURCE_YFINANCE = "yfinance"
SOURCE_TENCENT = "tencent"
SOURCE_FINNHUB = "finnhub"
SOURCE_ALPHAVANTAGE = "alphavantage"
SOURCE_MOCK = "mock"            # 显式 FORCE_MOCK=1 或本地无网络
SOURCE_MOCK_FALLBACK = "mock_fallback"  # 上游失败后兜底到 mock
ALL_SOURCES = (SOURCE_AKSHARE, SOURCE_YFINANCE, SOURCE_TENCENT, SOURCE_FINNHUB, SOURCE_ALPHAVANTAGE, SOURCE_MOCK, SOURCE_MOCK_FALLBACK)

# mock_fallback 路径的缓存 TTL: 30s 而不是默认 5min — Yahoo 限流是短窗口
# (实测 1-5min), 5min 缓存会让用户被旧 fallback 阻塞太久。30s 重新尝试更可能
# 命中真实数据。普通 mock / akshare / yfinance 仍走默认 5min。
FALLBACK_CACHE_TTL_SECONDS = 30.0

# 拉取上游失败时的重试: 借鉴 v2 daily_stock_analysis_v2 的 tenacity 指数退避。
# 3 attempts, base 2s, exponential 2x, capped at 10s — 足够应付瞬时限流又不让
# 用户等太久 (总等待 ~14s: 2+4+8)。Yahoo 限流窗口实测 1-5min, 14s 通常不够
# 跨窗口, 但 1 次重试 vs 3 次重试对边缘 case (限流刚解除) 差别巨大。
UPSTREAM_RETRY_MAX_ATTEMPTS = 3
UPSTREAM_RETRY_BASE_DELAY = 2.0
UPSTREAM_RETRY_MAX_DELAY = 10.0


def _is_transient_upstream_error(exc: BaseException) -> bool:
    """判断是否为瞬时可重试的上游错误 (yfinance / requests)。"""
    # 避免硬依赖 yfinance / requests 的具体类: 名字匹配优先, fallback 到 message。
    name = type(exc).__name__
    if name in {"YFRateLimitError", "HTTPError", "ConnectionError", "Timeout"}:
        return True
    # requests 体系 — yfinance 内部用 requests 时 wrap 在 utils.webkit_safe
    module = getattr(type(exc), "__module__", "")
    if module.startswith("requests.") or module.startswith("yfinance."):
        return True
    return False


def _with_upstream_retry(fn, *args, **kwargs):
    """对上游 fetch 函数做最多 ``UPSTREAM_RETRY_MAX_ATTEMPTS`` 次指数退避重试。

    只在瞬时错误 (限流 / 连接 / 超时) 上重试, 其它错误立即抛出。
    退避序列: 2s, 4s, 8s, ... 封顶 10s (UPSTREAM_RETRY_MAX_DELAY)。
    与 v2 yfinance_fetcher 的 tenacity 行为对齐 (stop=3, exponential 2-30s),
    但不引入 tenacity 依赖 — 现有 helper 用法更轻量。
    """
    import time as _time

    last_exc: BaseException | None = None
    delay = UPSTREAM_RETRY_BASE_DELAY
    for attempt in range(1, UPSTREAM_RETRY_MAX_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — 上游库异常类型多
            last_exc = exc
            if attempt >= UPSTREAM_RETRY_MAX_ATTEMPTS or not _is_transient_upstream_error(exc):
                raise
            logger.warning(
                "上游 fetch 瞬时失败 (attempt=%d/%d): %s — %.1fs 后重试",
                attempt, UPSTREAM_RETRY_MAX_ATTEMPTS, exc, delay,
            )
            _time.sleep(delay)
            delay = min(delay * 2, UPSTREAM_RETRY_MAX_DELAY)
    # 不可达, 但让 mypy 满意
    if last_exc:
        raise last_exc
    raise RuntimeError("_with_upstream_retry: unreachable")


# iter 165: singleflight 并发去重 — 同一 cache_key 的 fetch 只执行一次, 其余线程复用结果。
# 之前 8 个并发请求同一股票 (cache miss) 会同时调 8 次 akshare, 浪费 + 触发东财限流
# + 相互竞争 ("抢数据源")。现在第一个线程拉取, 其余等待其完成并复用, 缓存命中后
# kline_cache 兜底不再进 singleflight。
_FLIGHT_LOCK = threading.Lock()
_FLIGHT: dict[str, threading.Event] = {}  # cache_key -> 拉取进行中的信号
_FLIGHT_RESULT: dict[str, Tuple] = {}     # cache_key -> 完成后暂存的结果 (等待者读取)


def _singleflight_fetch(cache_key: str, fetcher) -> Tuple:
    """并发去重: 同一 cache_key 的 fetcher 只执行一次, 其余线程等待并复用结果。

    第一个线程执行 fetcher 并暂存结果; 等待者 event.wait 后读结果; 新请求 pop 结果。
    fetcher 抛异常时事件仍触发, 等待者读到无结果后自己重试。
    """
    while True:
        with _FLIGHT_LOCK:
            if cache_key in _FLIGHT_RESULT:
                # 已有完成结果 (上个 singleflight 遗留/并发完成) — 读即清
                return _FLIGHT_RESULT.pop(cache_key)
            event = _FLIGHT.get(cache_key)
            if event is None:
                event = threading.Event()
                _FLIGHT[cache_key] = event
                break
        # 有人正在拉取, 等它完成 (超时兜底防拉取线程异常卡死)
        wait_timeout = UPSTREAM_RETRY_MAX_ATTEMPTS * UPSTREAM_RETRY_MAX_DELAY + 15
        if not event.wait(timeout=wait_timeout):
            with _FLIGHT_LOCK:
                if _FLIGHT.get(cache_key) is event:
                    del _FLIGHT[cache_key]
            continue
        # 等待者: get 不 pop — 多个等待者都能读到同一结果 (pop 会让后续等待者拿 None
        # 而误以为自己要拉取, 导致重复调用 akshare)。结果由下一个"新请求"pop 清理。
        with _FLIGHT_LOCK:
            result = _FLIGHT_RESULT.get(cache_key)
        if result is not None:
            return result
        # 事件触发但结果未就绪 (拉取线程异常) — 重试自己拉
        continue

    try:
        result = fetcher()
    except BaseException:
        # 通知等待者: 失败, 它们会重新尝试拉取
        with _FLIGHT_LOCK:
            _FLIGHT.pop(cache_key, None)
            event.set()
        raise
    with _FLIGHT_LOCK:
        _FLIGHT_RESULT[cache_key] = result
        event.set()
        _FLIGHT.pop(cache_key, None)
    return result
# iter 131: _normalize_df 列名映射常量 — 支持 akshare 中文列名 + yfinance 英文列名。
# 之前 dict literal 嵌入函数体内, 每次 _normalize_df 调用都重建一次 (单次 /history 365 根柱 × ~200 字节)。
_COLUMN_RENAME_MAP: dict[str, str] = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "change_percent",
    "Date": "date",
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
    "Adj Close": "adj_close",
}

# 周期映射
PERIOD_MAP_AKSHARE = {
    "daily": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
}
PERIOD_MAP_YFINANCE = {
    "daily": "1d",
    "weekly": "1wk",
    "monthly": "1mo",
}
# iter 88: 周期对应步长 (mock 数据回看天数 → bar 数换算)。
# 提到模块级避免每次 _generate_mock_data 调用都 runtime dict 重建。
STEP_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}


def detect_market(code: str) -> MarketType:
    """根据代码识别市场。

    优先级:
      1. 有 HK/hk 前缀(无论长度)→ 港股
      2. 有 .HK / .SS / .SZ 后缀 → 对应市场
      3. 纯数字: 6位=A股, 5位=港股, 4位/3位补 0 后按港股处理
      4. 否则 → 美股
    """
    raw = code.strip().upper()
    # 显式市场标记优先于代码长度, 避免例如 ``123456.HK`` 被误判为 A股。
    # HK 前缀与 .HK 后缀都明确表示港股; .SS/.SZ 表示 A股。
    if raw.startswith("HK") or raw.endswith(".HK"):
        return "hk"
    if raw.endswith((".SS", ".SZ")):
        return "cn"

    code = raw
    # 后缀
    code = code.replace(".SS", "").replace(".SZ", "").replace(".HK", "")
    # 前缀
    while code.startswith(("HK", "hk")):
        code = code[2:]
    if code.isdigit():
        if len(code) == 6:
            return "cn"
        if len(code) == 5:
            return "hk"
        # iter 95: 6-7 位数字也视为港股 — 用户可能输入带前导 0 的 7 位港股代码
        # (e.g. "00700" for Tencent), 之前会回退到 "us" 然后 yfinance 找不到 ticker。
        # 注: STOCK_CODE_PATTERN 已限制总长度 ≤ 20, 这里 6-7 位数字 + HK 前缀仍 ≤ 20。
        if 1 <= len(code) <= 7:
            # 短数字 (1-4位) → 视为港股补 0 形式; 6 位优先判 cn 后剩余 7 位也归港股
            # 例: "700" -> "00700" 港股, "00700" -> hk
            return "hk"
    return "us"


def normalize_code(code: str, market: MarketType) -> str:
    """去掉前缀/后缀得到纯代码。"""
    code = code.strip().upper()
    code = code.replace(".SS", "").replace(".SZ", "").replace(".HK", "")
    if code.startswith(("HK", "hk")):
        code = code[2:]
    return code


def _normalize_df(df: pd.DataFrame) -> List[KLineData]:
    """将不同来源的 DataFrame 统一转为 KLineData 列表。"""
    df = df.copy()
    # 列名归一化
    # iter 131: rename_map 提升到模块常量 — 之前每次调用都重建 (~200 字节 dict × 365 根柱),
    # GC 压力 + 重复构造。in-place dict comprehension 不影响语义 (filter 仍按调用时列名判断)。
    df = df.rename(columns={
        k: v for k, v in _COLUMN_RENAME_MAP.items() if k in df.columns
    })

    required = ["date", "open", "high", "low", "close", "volume"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"数据源返回缺少必要列: {col}, 实际列: {df.columns.tolist()}")

    # 计算涨跌幅
    if "change_percent" not in df.columns:
        df["change_percent"] = df["close"].pct_change().fillna(0) * 100

    if "amount" not in df.columns:
        df["amount"] = 0.0

    # 日期转字符串
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

    result: List[KLineData] = []
    # iter 84: 改用 to_dict("records") 替代 iterrows() — iterrows 每次循环构造 pandas Series
    # 并做 dtype 转换, 对 365-bar daily 来说浪费明显; to_dict 只构造一次 dict 后纯 Python 遍历。
    records = df.to_dict("records")
    for r in records:
        result.append(
            KLineData(
                date=str(r["date"]),
                open=float(r["open"]) if pd.notna(r["open"]) else 0.0,
                high=float(r["high"]) if pd.notna(r["high"]) else 0.0,
                low=float(r["low"]) if pd.notna(r["low"]) else 0.0,
                close=float(r["close"]) if pd.notna(r["close"]) else 0.0,
                volume=float(r["volume"]) if pd.notna(r["volume"]) else 0.0,
                amount=float(r["amount"]) if pd.notna(r["amount"]) else 0.0,
                change_percent=float(r["change_percent"]) if pd.notna(r["change_percent"]) else 0.0,
            )
        )
    return result


def _generate_mock_data(
    code: str, period: PeriodType, days: int, base_price: Optional[float] = None
) -> Tuple[List[KLineData], str]:
    """生成模拟 K线数据(用于网络不可用时演示)。

    使用确定性算法(基于 code hash 作为随机种子), 让同一只股票每次生成相同走势。
    根据周期调整波动率, 并构造一波明显的"涨→跌→反弹→盘整"形态。
    """
    seed = sum(ord(c) for c in code)
    rng = random.Random(seed)

    # 周期对应步长
    step_days = STEP_DAYS[period]
    n_bars = max(20, days // step_days)

    # 起点价格: 根据股票代码 hash 到合理区间
    if base_price is None:
        base_price = round(20 + (seed % 200), 2)

    # 生成走势: 几个大波段拼接
    # [段1: 平稳上行 30%] -> [段2: 暴跌 25%] -> [段3: 强势反弹 40%] -> [段4: 高位震荡]
    waves = [
        (0.15, 0.002, 0.30),   # (年化趋势, 日波动率, 占比)
        (-0.25, 0.004, 0.25),
        (0.40, 0.003, 0.25),
        (0.0, 0.0025, 0.20),
    ]
    trend_strengths = []
    for _trend, _vol, _frac in waves:
        trend_strengths.extend([_trend] * max(1, int(n_bars * _frac)))

    # 截断/补齐
    if len(trend_strengths) > n_bars:
        trend_strengths = trend_strengths[:n_bars]
    while len(trend_strengths) < n_bars:
        trend_strengths.append(0.0)

    # 用几何布朗运动生成 close
    closes = [base_price]
    for daily_trend in trend_strengths:
        per_step_trend = daily_trend * step_days / 365
        per_step_vol = 0.025 * math.sqrt(step_days)
        prev = closes[-1]
        change = per_step_trend + per_step_vol * rng.gauss(0, 1)
        closes.append(prev * (1 + change))
    closes = closes[1:]

    # iter 86: 兜底 — 极小概率下 gauss 极端值会让 close 落到接近 0 或负数,
    # 触发 wave 累计后产生 < 0.01 的"废纸价", UI 显示成 NaN 或负百分比。
    # clip 到 [0.01, 1_000_000] 既保合理性, 又不影响正常波段的可见形态。
    MIN_MOCK_PRICE = 0.01
    MAX_MOCK_PRICE = 1_000_000.0
    closes = [max(MIN_MOCK_PRICE, min(c, MAX_MOCK_PRICE)) for c in closes]

    # OHLC: 基于 close 构造
    # iter 80: 用 UTC 一致性 — peak_trough.analysis_date 也是 UTC, mock 日期应对齐
    # 否则跨时区部署会让 mock 数据日期与分析日期不在同一天, 产生看似 "昨天" 的高峰/低谷。
    today = datetime.now(timezone.utc).date()
    # K线日期对齐: 日=自然日, 周=周五, 月=月末
    result: List[KLineData] = []
    for i, close in enumerate(closes):
        if period == "daily":
            bar_date = today - timedelta(days=(n_bars - i - 1))
        elif period == "weekly":
            bar_date = today - timedelta(weeks=(n_bars - i - 1))
        else:  # monthly
            # 每月最后一个交易日近似
            year = today.year
            month = today.month - (n_bars - i - 1)
            while month <= 0:
                month += 12
                year -= 1
            # 简化: 取当月 28 日, 但不能晚于今天, 否则会落在未来
            candidate = today.replace(year=year, month=month, day=28)
            if candidate > today:
                # 收尾月份的bar只能是已结束的月份, 退化到该月最后一个 <= today 的 28 日
                candidate = today.replace(day=min(today.day, 28))
            bar_date = candidate

        high_pct = 1 + abs(rng.gauss(0.005, 0.003))
        low_pct = 1 - abs(rng.gauss(0.005, 0.003))
        open_pct = 1 + rng.gauss(0, 0.005)
        high = round(close * high_pct, 4)
        low = round(close * low_pct, 4)
        open_ = round(close * open_pct, 4)
        volume = rng.randint(5_000_000, 30_000_000)

        prev_close = closes[i - 1] if i > 0 else close
        change_pct = (close - prev_close) / prev_close * 100

        result.append(
            KLineData(
                date=bar_date.strftime("%Y-%m-%d"),
                open=open_,
                high=max(open_, close, high),
                low=min(open_, close, low),
                close=round(close, 4),
                volume=float(volume),
                amount=0.0,
                change_percent=round(change_pct, 2),
            )
        )

    return result, "mock"


def _fetch_tencent(code: str, period: PeriodType, days: int, market: MarketType) -> Optional[List[KLineData]]:
    """用腾讯直连 API 拉取 A股/港股日/周/月 K 线。

    借鉴 v2 daily_stock_analysis_v2/data_provider/tencent_fetcher.py:
      端点: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
      param: {symbol},{freq},{start},{end},{count},qfq
      symbol: A股 sh600519 / sz000001; 港股 hk00700 (保留前导 0)
      freq: day / week / month
    响应: data[symbol]["qfq{freq}"] = [[date, open, close, high, low, volume], ...]
    (注意列序: open, close, high, low — 与常见 OHLC 顺序不同)

    iter 162: akshare 东财接口在部分网络间歇性 RemoteDisconnected (用户实测),
    直接 fallback mock 让用户看到假数据 + banner。腾讯直连 (gtimg.cn) 是独立
    服务器, 与东财不共享故障域, 作为 A股/港股第二 fallback:
      akshare → tencent → yfinance → mock_fallback

    Returns: List[KLineData] 或 None (失败时 None, 让调用方走下一节点)
    """
    freq = {"daily": "day", "weekly": "week", "monthly": "month"}.get(period)
    if not freq:
        return None
    clean = code.strip()
    # 构建腾讯 symbol
    if market == "hk":
        symbol = f"hk{clean.zfill(5)}"
    elif market == "cn":
        # 沪: 6xx/5xx/90x (上海 ETF / B股); 深: 其余 (00/30/20)
        symbol = f"sh{clean}" if clean.startswith(("6", "5", "90")) else f"sz{clean}"
    else:
        return None  # 美股不走腾讯

    try:
        import requests
    except ImportError:
        logger.debug("requests 模块不可用, 跳过腾讯 fallback")
        return None

    end = datetime.now().strftime("%Y-%m-%d")
    # 多留 90 天缓冲覆盖节假日/停牌; count 是 bar 数, 给足量再 slice
    start = (datetime.now() - timedelta(days=days + 90)).strftime("%Y-%m-%d")
    count = min(max(days, 60), 800)
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    try:
        resp = requests.get(
            url,
            params={"param": f"{symbol},{freq},{start},{end},{count},qfq"},
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.debug("tencent 请求失败 (%s %s): %s", code, period, exc)
        return None

    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    payload = payload.get(symbol) if isinstance(payload, dict) else None
    if not isinstance(payload, dict):
        return None
    rows = payload.get(f"qfq{freq}") or payload.get(freq) or []
    if not rows:
        return None

    result: List[KLineData] = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            # 列序: [date, open, close, high, low, volume]
            bar_date, open_, close, high, low, volume = row[0], row[1], row[2], row[3], row[4], row[5]
            result.append(
                KLineData(
                    date=str(bar_date)[:10],
                    open=float(open_),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume=float(volume),
                    amount=0.0,
                    change_percent=0.0,
                )
            )
        except (ValueError, TypeError):
            continue
    if not result:
        return None
    return result[-_bars_for_days(period, days):] if len(result) > _bars_for_days(period, days) else result


def _fetch_a_share(code: str, period: PeriodType, days: int) -> Tuple[List[KLineData], str]:
    """A股数据 - 优先 akshare, 兜底 yfinance, 最后 mock。"""
    # 跳过网络数据源直接走 mock(因为本地环境连不上东财/yahoo)
    if is_force_mock():
        mock_data, _ = _generate_mock_data(code, period, days, base_price=_cn_baseline_price(code))
        return mock_data, SOURCE_MOCK
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days + AKSHARE_A_SHARE_BUFFER_DAYS)).strftime("%Y%m%d")
    try:
        import akshare as ak

        df = _with_upstream_retry(
            ak.stock_zh_a_hist,
            symbol=code,
            period=PERIOD_MAP_AKSHARE[period],
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        )
        if df is None or df.empty:
            raise ValueError("akshare 返回空数据")
        data = _normalize_df(df)
        return data[-_bars_for_days(period, days):] if len(data) > _bars_for_days(period, days) else data, SOURCE_AKSHARE
    except Exception as exc:
        # iter 162: 腾讯直连 fallback — akshare 东财间歇性不可达时, 腾讯 (gtimg.cn)
        # 是独立服务器, 能继续返回真实数据, 避免直接 fallback mock。
        data = _fetch_tencent(code, period, days, "cn")
        if data:
            logger.info("tencent 兜底成功 (%s %s): %d bars", code, period, len(data))
            return data, SOURCE_TENCENT
        # 兜底 yfinance
        try:
            import yfinance as yf

            suffix = ".SS" if code.startswith("6") else ".SZ"
            ticker = f"{code}{suffix}"
            raw_df = _with_upstream_retry(
                yf.download,
                ticker,
                period=f"{max(days * 2, YFINANCE_MIN_PERIOD_DAYS)}d",
                interval=PERIOD_MAP_YFINANCE[period],
                progress=False,
                auto_adjust=True,
                # iter 74: 显式 timeout 防止 akshare/yfinance 上游卡死时阻塞 FastAPI worker
                timeout=10,
            )
            df = raw_df
            if df is None or df.empty:
                raise RuntimeError(f"yfinance 也未取到 {code} 数据: {exc}")
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            data = _normalize_df(df.reset_index())
            return data[-_bars_for_days(period, days):] if len(data) > _bars_for_days(period, days) else data, SOURCE_YFINANCE
        except Exception as inner_exc:
            # iter 68: 静默回退到 mock 之前先记 warning — 让运维能看到 akshare + yfinance 都失败的痕迹
            # iter 157: source 改 mock_fallback, 配合 frontend banner 区分 "用户主动 mock" 和 "上游失败 → mock"。
            logger.warning(
                "akshare+tencent+yfinance 全失败, fallback 到 mock (%s %s): %s / %s",
                code, period, exc, inner_exc,
            )
            mock_data, _ = _generate_mock_data(code, period, days, base_price=_cn_baseline_price(code))
            return mock_data, SOURCE_MOCK_FALLBACK


def _hk_yfinance_ticker(code: str) -> str:
    """生成 yfinance 港股 ticker。

    Yahoo 接受 5 位内部码形式 (0700.HK), 区别于去全部前导零的简化形式 (700.HK)。
    仅剥离单个前导零以保留必要位数: 00700 -> 0700.HK, 00005 -> 0005.HK。
    非 5 位或非全零开头的代码保持原样追加 .HK。
    """
    if len(code) == 5 and code.startswith("0"):
        return f"{code[1:]}.HK"
    return f"{code}.HK"


def _fetch_hk(code: str, period: PeriodType, days: int) -> Tuple[List[KLineData], str]:
    """港股数据 - 优先 akshare, 兜底 yfinance, 最后 mock。"""
    if is_force_mock():
        mock_data, _ = _generate_mock_data(code, period, days, base_price=_hk_baseline_price(code))
        return mock_data, SOURCE_MOCK
    raw_code = code
    if not code.isdigit() or len(code) != 5:
        # 7位如 00700, 取后5位
        digits = re.sub(r"\D", "", code)
        if len(digits) >= 5:
            code = digits[-5:].zfill(5)
        else:
            raise ValueError(f"无效港股代码: {raw_code}")
    code = code.zfill(5)

    end_date = datetime.now().strftime("%Y%m%d")
    start_date = (datetime.now() - timedelta(days=days + AKSHARE_HK_BUFFER_DAYS)).strftime("%Y%m%d")
    try:
        import akshare as ak

        df = _with_upstream_retry(
            ak.stock_hk_hist,
            symbol=code,
            period=PERIOD_MAP_AKSHARE[period],
            start_date=start_date,
            end_date=end_date,
            adjust="qfq",
        )
        if df is None or df.empty:
            raise ValueError("akshare 返回空数据")
        data = _normalize_df(df)
        return data[-_bars_for_days(period, days):] if len(data) > _bars_for_days(period, days) else data, SOURCE_AKSHARE
    except Exception as exc:
        # iter 162: 腾讯直连 fallback (港股) — 同 A股, 东财不可达时用腾讯兜底
        data = _fetch_tencent(code, period, days, "hk")
        if data:
            logger.info("tencent 港股兜底成功 (%s %s): %d bars", code, period, len(data))
            return data, SOURCE_TENCENT
        try:
            import yfinance as yf

            ticker = _hk_yfinance_ticker(code)
            raw_df = _with_upstream_retry(
                yf.download,
                ticker,
                period=f"{max(days * 2, YFINANCE_MIN_PERIOD_DAYS)}d",
                interval=PERIOD_MAP_YFINANCE[period],
                progress=False,
                auto_adjust=True,
                # iter 74: 同上, 港股路径 timeout
                timeout=10,
            )
            df = raw_df
            if df is None or df.empty:
                raise RuntimeError(f"yfinance 港股未取到 {raw_code} 数据: {exc}")
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            data = _normalize_df(df.reset_index())
            return data[-_bars_for_days(period, days):] if len(data) > _bars_for_days(period, days) else data, SOURCE_YFINANCE
        except Exception as inner_exc:
            # iter 69: 与 CN fetch 对齐 — 在静默 fallback 之前记 warning
            # iter 157: source 改 mock_fallback, 与 fetch_a_share 一致。
            logger.warning(
                "akshare+tencent+yfinance 港股全失败, fallback 到 mock (%s %s): %s / %s",
                code, period, exc, inner_exc,
            )
            mock_data, _ = _generate_mock_data(code, period, days, base_price=_hk_baseline_price(code))
            return mock_data, SOURCE_MOCK_FALLBACK


def _resample_us_ohlcv(df: pd.DataFrame, period: PeriodType) -> pd.DataFrame:
    """把新浪美股日 K (date/open/high/low/close/volume) 重采样为周/月 K。

    akshare ``stock_us_daily`` 只返回日线, 周/月需要本地 resample。
    用 ``W-FRI`` (周五收盘) / ``ME`` (月末) 对齐常见 K线习惯。
    最后一段 partial period (本周/本月未走完) 的日期标签 clamp 到最后一个实际
    交易日, 避免 UI 出现未来日期 — 与 iter 157 mock 数据的日期守卫一致。
    """
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    freq = "W-FRI" if period == "weekly" else "ME"
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    result = df.set_index("date").resample(freq).agg(agg).dropna().reset_index()
    if not result.empty:
        # 最后一个 bar 的标签 (本周五/本月末) 可能在今天之后 → clamp 到最后交易日
        last_real = df["date"].max()
        result["date"] = result["date"].where(result["date"] <= last_real, last_real)
    return result


def _fetch_us_akshare(code: str, period: PeriodType, days: int) -> Optional[List[KLineData]]:
    """用 akshare 拉美股日/周/月 K (新浪 stock_us_daily, 国内服务器不受 Yahoo 限流)。

    iter 160: 用户指出"参考项目能实时拉美股" — 实测 akshare 的新浪/东财接口在
    Yahoo 限流网络下依然返回真实数据 (AAPL/TSLA 等)。之前只测了 stock_us_hist
    一次碰壁就放弃, 没试 stock_us_daily。现在 akshare 作为美股首选, yfinance 兜底。

    返回 List[KLineData] 或 None (失败时 None, 让调用方走 yfinance)。
    - daily: 直接用 stock_us_daily
    - weekly/monthly: stock_us_daily 后本地 resample (W-FRI / ME)
    """
    try:
        import akshare as ak
    except ImportError:
        logger.debug("akshare 模块不可用, 跳过美股 akshare 路径")
        return None
    symbol = code.strip().upper()
    try:
        df = _with_upstream_retry(ak.stock_us_daily, symbol=symbol, adjust="qfq")
    except Exception as exc:
        logger.debug("akshare stock_us_daily 失败 (%s %s): %s", symbol, period, exc)
        return None
    if df is None or df.empty:
        return None
    if period != "daily":
        df = _resample_us_ohlcv(df, period)
    data = _normalize_df(df)
    return data[-_bars_for_days(period, days):] if len(data) > _bars_for_days(period, days) else data


def _fetch_us(code: str, period: PeriodType, days: int) -> Tuple[List[KLineData], str]:
    """美股数据 - akshare (首选) + yfinance + Finnhub/AlphaVantage, 失败时 mock。

    数据源优先级 (iter 158 + iter 160):
      1. akshare (新浪 stock_us_daily, 国内服务器) — 首选, 不受 Yahoo 限流
      2. yfinance (Ticker.history, v8/finance/chart) — 3 次指数退避重试
      3. Finnhub (仅日 K, 需 FINNHUB_API_KEY) — 60 calls/min 免费
      4. AlphaVantage (仅日 K, 需 ALPHAVANTAGE_API_KEY) — 25 calls/day 免费
      5. mock_fallback — 所有上游都失败时

    iter 160 变更: 用户指出 v1 应能像参考项目一样实时拉美股, 实测发现 akshare
    的新浪/东财美股接口在 Yahoo 限流下仍返回真实数据。把它放到 yfinance 之前
    作为首选 — 对 A股/港股本来就是 akshare 首选, 美股保持一致。个别 akshare
    覆盖不到的美股 ticker 会自动 fall through 到 yfinance。
    """
    if is_force_mock():
        mock_data, _ = _generate_mock_data(code, period, days, base_price=_us_baseline_price(code))
        return mock_data, SOURCE_MOCK

    # 路径 1: akshare (新浪 stock_us_daily)
    try:
        data = _fetch_us_akshare(code, period, days)
        if data:
            return data, SOURCE_AKSHARE
    except Exception as exc:
        logger.warning("akshare 美股失败, 尝试 yfinance (%s %s): %s", code, period, exc)

    # 路径 2: yfinance
    try:
        import yfinance as yf

        ticker_obj = yf.Ticker(code.upper())

        def _call() -> pd.DataFrame:
            return ticker_obj.history(
                period=f"{max(days * 2, YFINANCE_MIN_PERIOD_DAYS)}d",
                interval=PERIOD_MAP_YFINANCE[period],
                auto_adjust=True,
                timeout=10,
            )

        df = _with_upstream_retry(_call)
        if df is None or df.empty:
            raise RuntimeError(f"yfinance 未取到 {code} 数据")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        data = _normalize_df(df.reset_index())
        return data[-_bars_for_days(period, days):] if len(data) > _bars_for_days(period, days) else data, SOURCE_YFINANCE
    except Exception as exc:
        logger.warning(
            "yfinance 美股失败, 尝试 Finnhub/AlphaVantage (%s %s): %s",
            code, period, exc,
        )

    # 路径 3 + 4: Finnhub → AlphaVantage (仅日 K 周期, 其它周期跳过)
    if period == "daily":
        # 延迟 import: 这些模块在大多数调用路径根本用不到
        try:
            from services.us_alternate_fetchers import (
                fetch_us_finnhub,
                fetch_us_alphavantage,
            )
        except ImportError as exc:
            logger.debug("us_alternate_fetchers 不可用: %s", exc)
            fetch_us_finnhub = None  # type: ignore
            fetch_us_alphavantage = None  # type: ignore

        # 路径 3: Finnhub
        if fetch_us_finnhub is not None:
            data = fetch_us_finnhub(code, period, days)
            if data:
                logger.info("Finnhub 兜底成功 (%s %s): %d bars", code, period, len(data))
                return data, SOURCE_FINNHUB
        # 路径 4: AlphaVantage
        if fetch_us_alphavantage is not None:
            data = fetch_us_alphavantage(code, period, days)
            if data:
                logger.info("AlphaVantage 兜底成功 (%s %s): %d bars", code, period, len(data))
                return data, SOURCE_ALPHAVANTAGE

    # 路径 5: mock_fallback
    logger.warning("美股所有上游失败, fallback 到 mock (%s %s)", code, period)
    mock_data, _ = _generate_mock_data(code, period, days, base_price=_us_baseline_price(code))
    return mock_data, SOURCE_MOCK_FALLBACK


def _cn_baseline_price(code: str) -> float:
    """A股 mock 起点价: 基于 code hash 生成 20-300 元区间随机值 (确定性)。"""
    seed = sum(ord(c) for c in code)
    # A股价格区间 5-300
    return round(20 + (seed % 280), 2)


def _hk_baseline_price(code: str) -> float:
    """港股 mock 起点价: 基于 code hash 生成 50-500 元区间随机值 (确定性)。"""
    seed = sum(ord(c) for c in code)
    # 港股价格区间 5-500
    return round(50 + (seed % 450), 2)


def _us_baseline_price(code: str) -> float:
    """美股 mock 起点价: 基于 code hash 生成 40-700 元区间随机值 (确定性)。"""
    seed = sum(ord(c) for c in code)
    # 美股价格区间 20-700
    return round(40 + (seed % 660), 2)


# 简化的股票名映射 - 实际场景中应使用 akshare 的 stock_info_a_code_name 接口。
# 用于 search_stocks / guess_name / 前端展示，避免 API 调用延迟。
CN_NAME_HINTS = {
    # 白酒/消费
    "600519": "贵州茅台", "000858": "五粮液", "000568": "泸州老窖", "600809": "山西汾酒",
    "002304": "洋河股份", "600887": "伊利股份", "000895": "双汇发展",
    # 银行/金融
    "601398": "工商银行", "601988": "中国银行", "601288": "农业银行", "601939": "建设银行",
    "600036": "招商银行", "601166": "兴业银行", "000001": "平安银行", "600000": "浦发银行",
    "601318": "中国平安", "601628": "中国人寿", "601336": "新华保险",
    # 地产/基建
    "000002": "万科A", "600048": "保利发展", "001979": "招商蛇口", "600340": "华夏幸福",
    "601668": "中国建筑", "601390": "中国中铁", "601800": "中国交建",
    # 科技/互联网
    "300750": "宁德时代", "002594": "比亚迪", "300059": "东方财富", "002415": "海康威视",
    "600276": "恒瑞医药", "000063": "中兴通讯", "002475": "立讯精密", "300760": "迈瑞医疗",
    "300015": "爱尔眼科", "600436": "片仔癀", "300122": "智飞生物",
    # 新能源/汽车
    "601012": "隆基绿能", "002460": "赣锋锂业", "300014": "亿纬锂能", "002129": "TCL中环",
    "300124": "汇川技术", "002241": "歌尔股份", "603799": "华友钴业",
    # 工业/制造
    "000333": "美的集团", "000651": "格力电器", "600690": "海尔智家", "601877": "正泰电器",
    "600585": "海螺水泥", "601088": "中国神华", "601225": "陕西煤业", "600028": "中国石化",
    # 医药/健康
    "000538": "云南白药", "600196": "复星医药", "300003": "乐普医疗", "002007": "华兰生物",
    # 传媒/娱乐
    "300413": "芒果超媒", "002027": "分众传媒", "600977": "中国电影",
    # 券商
    "600030": "中信证券", "601688": "华泰证券", "000776": "广发证券", "600837": "海通证券",
    "601066": "中信建投", "002736": "国信证券",
    # 军工/航天
    "600893": "航发动力", "000768": "中航西飞", "600316": "洪都航空",
    # 食品/农业
    "603288": "海天味业", "600298": "安琪酵母", "000876": "新希望",
}
HK_NAME_HINTS = {
    "00700": "腾讯控股", "09988": "阿里巴巴-W", "03690": "美团-W", "01810": "小米集团-W",
    "00388": "香港交易所", "00939": "建设银行", "00941": "中国移动", "00998": "中信银行",
    "01211": "比亚迪股份", "02318": "中国平安", "02628": "中国人寿", "03988": "中国银行",
    "00005": "汇丰控股", "00001": "长和", "00016": "新鸿基地产", "00019": "太古股份",
    "02020": "安踏体育", "01024": "快手-W", "09618": "京东集团-SW", "09888": "百度集团-SW",
    "09999": "网易-S", "01088": "中国神华", "00883": "中国海洋石油", "00386": "中国石油化工",
    "02269": "药明生物", "02359": "药明康德", "06160": "百济神州",
}
US_NAME_HINTS = {
    # 科技巨头
    "AAPL": "Apple Inc.", "MSFT": "Microsoft", "GOOGL": "Alphabet (Class A)",
    "GOOG": "Alphabet (Class C)", "AMZN": "Amazon", "META": "Meta Platforms",
    "NVDA": "NVIDIA", "TSLA": "Tesla", "NFLX": "Netflix", "ORCL": "Oracle",
    "CRM": "Salesforce", "ADBE": "Adobe", "INTC": "Intel", "AMD": "AMD",
    "CSCO": "Cisco", "IBM": "IBM",
    # 消费/零售
    "WMT": "Walmart", "COST": "Costco", "HD": "Home Depot", "MCD": "McDonald's",
    "NKE": "Nike", "SBUX": "Starbucks", "TGT": "Target", "LOW": "Lowe's",
    # 金融
    "JPM": "JPMorgan Chase", "BAC": "Bank of America", "WFC": "Wells Fargo",
    "GS": "Goldman Sachs", "MS": "Morgan Stanley", "V": "Visa", "MA": "Mastercard",
    "BRK-B": "Berkshire Hathaway", "BLK": "BlackRock",
    # 医药/健康
    "JNJ": "Johnson & Johnson", "PFE": "Pfizer", "MRNA": "Moderna", "LLY": "Eli Lilly",
    "UNH": "UnitedHealth", "ABBV": "AbbVie", "TMO": "Thermo Fisher",
    # 中概股
    "BABA": "Alibaba", "PDD": "PDD Holdings", "JD": "JD.com", "BIDU": "Baidu",
    "NIO": "NIO Inc.", "XPEV": "XPeng", "LI": "Li Auto", "BILI": "Bilibili",
    "TME": "Tencent Music", "VIPS": "Vipshop",
    # 能源
    "XOM": "ExxonMobil", "CVX": "Chevron",
    # 通信/媒体
    "DIS": "Disney", "T": "AT&T", "VZ": "Verizon", "CMCSA": "Comcast",
}

# iter 163: 全量 A股列表 — 内置 CN_NAME_HINTS 只有 ~100 条, 大量股票 (昊华科技 600378 /
# 天齐锂业 002466 等) 搜不到。用 akshare stock_info_a_code_name 拉全量 (~5500 只)
# 合并内置, 进程内缓存 24h。akshare 接口本身 @lru_cache, 但 tqdm 进度条需屏蔽,
# 且网络不可达时需降级内置。
_CN_MAP_CACHE: Optional[dict[str, str]] = None
_CN_MAP_LOADED_AT: float = 0.0
_CN_MAP_TTL_SECONDS = 24 * 3600.0  # 24h — A股代码/名称低频变化
_CN_MAP_LOCK = threading.Lock()


def _get_cn_name_map() -> dict[str, str]:
    """A股全量 {code: name} 映射 (内置 CN_NAME_HINTS + akshare 全量合并)。

    优先级: 内置优先 (人工维护的名称更友好), akshare 缺失的 code 补全。
    akshare 拉取失败 (东财不可达) 时降级为仅内置 — 搜索仍可用但不完整。
    线程安全 + 24h TTL 缓存, 避免每次搜索重复拉网络。
    """
    global _CN_MAP_CACHE, _CN_MAP_LOADED_AT
    now = time.monotonic()
    if _CN_MAP_CACHE is not None and (now - _CN_MAP_LOADED_AT) < _CN_MAP_TTL_SECONDS:
        return _CN_MAP_CACHE
    with _CN_MAP_LOCK:
        # 二次检查: 持锁后可能已被其它线程加载
        if _CN_MAP_CACHE is not None and (time.monotonic() - _CN_MAP_LOADED_AT) < _CN_MAP_TTL_SECONDS:
            return _CN_MAP_CACHE
        merged: dict[str, str] = dict(CN_NAME_HINTS)
        if not is_force_mock():
            try:
                import akshare as ak

                # 屏蔽 akshare 内部的 tqdm 进度条 (拉取时打印 ~17 行进度, 污染日志)
                from tqdm import tqdm

                _orig_init = tqdm.__init__

                def _silent(self, *a, **kw):
                    kw["disable"] = True
                    return _orig_init(self, *a, **kw)

                tqdm.__init__ = _silent
                try:
                    df = ak.stock_info_a_code_name()
                finally:
                    tqdm.__init__ = _orig_init
                for row in df.itertuples(index=False):
                    code_s = str(getattr(row, "code", "")).zfill(6)
                    name = str(getattr(row, "name", "")).strip()
                    if code_s and name:
                        merged.setdefault(code_s, name)
                logger.info("全量 A股列表加载成功: %d 只 (内置 %d + akshare 补全)",
                            len(merged), len(CN_NAME_HINTS))
            except Exception as exc:
                # 东财不可达时降级内置 — 搜索仍可用 (已覆盖常见股票)
                logger.warning("akshare 全量 A股列表拉取失败, 降级内置 (%d 只): %s",
                               len(merged), exc)
        _CN_MAP_CACHE = merged
        _CN_MAP_LOADED_AT = time.monotonic()
        return _CN_MAP_CACHE


def guess_name(code: str, market: MarketType) -> str:
    code = normalize_code(code, market).upper()
    if market == "cn":
        name_map = _get_cn_name_map()
        if code in name_map:
            return name_map[code]
    if market == "hk":
        # 港股代码 keys 存为 5 位(00005), 输入可能是 5 位或 7 位
        padded = code.zfill(5)
        if padded in HK_NAME_HINTS:
            return HK_NAME_HINTS[padded]
        # 也支持去掉前导 0 的简短形式
        stripped = padded.lstrip("0")
        if stripped in HK_NAME_HINTS:
            return HK_NAME_HINTS[stripped]
    if market == "us" and code in US_NAME_HINTS:
        return US_NAME_HINTS[code]
    # 未知股票: 显式标记, 让前端知道这不是真名
    return f"{code} (未匹配)"


def fetch_history(code: str, period: PeriodType, days: int = 365) -> Tuple[List[KLineData], str, MarketType, str]:
    """主入口: 获取 K线数据。

    Returns: (data, source, market, stock_name)

    带 TTL 缓存: 同一只股票/周期/天数 5 分钟内不重复拉取
    """
    market = detect_market(code)
    clean_code = normalize_code(code, market)
    cache_key = f"{clean_code}|{period}|days={days}"  # v2: 加 days= 前缀, 避免旧 3字段值被新代码误命中
    cached = kline_cache.get(cache_key)
    if cached is not None:
        # iter 64: 防御性 shape 校验 — 旧版本缓存的 tuple 长度不对时, 删 cache 重新拉取
        # (升级时改 fetch_history 返回签名不会导致 5 分钟内所有请求反复失败)
        if not (isinstance(cached, tuple) and len(cached) == 4):
            logger.warning(
                "kline_cache poison: 缓存值不是 4-tuple (key=%s, type=%s), 删除重拉",
                cache_key, type(cached).__name__,
            )
            kline_cache.delete(cache_key)
        else:
            data, source, market_cached, name = cached
            logger.info("fetch_history cache hit %s (%d bars)", cache_key, len(data))
            return data, source, market_cached, name

    # iter 165: singleflight 并发去重 — 同一股票/周期/天数并发 cache miss 只拉一次,
    # 避免抢 akshare 数据源 (实测 8 并发 → 8 次重复拉取)。首个线程拉取, 其余复用。
    def _do_fetch():
        if market == "cn":
            d, s = _fetch_a_share(clean_code, period, days)
        elif market == "hk":
            d, s = _fetch_hk(clean_code, period, days)
        else:
            d, s = _fetch_us(clean_code, period, days)
        n = guess_name(clean_code, market)
        return (d, s, market, n)

    result = _singleflight_fetch(cache_key, _do_fetch)
    data, source, market_r, name = result
    # iter 157: mock_fallback 用 30s TTL — Yahoo 限流是短窗口, 默认 5min 会让用户
    # 被旧 fallback 阻塞太久。普通 mock / akshare / yfinance 仍走默认 5min。
    cache_ttl = FALLBACK_CACHE_TTL_SECONDS if source == SOURCE_MOCK_FALLBACK else None
    kline_cache.set(cache_key, result, ttl=cache_ttl)
    logger.info(
        "fetch_history %s/%s/%s -> %d bars from %s (ttl=%s)",
        clean_code, market, period, len(data), source,
        "30s" if cache_ttl else "5min",
    )
    return result


def search_stocks(q: str, limit: int = SEARCH_STOCKS_LIMIT) -> List[dict]:
    """股票搜索筛选。

    支持:
      - 代码前缀/包含匹配 (e.g. "6005", "AAPL")
      - 中文名包含匹配 (e.g. "茅台", "银行")
      - 大小写不敏感
    """
    q = q.strip()
    if not q:
        return []

    q_lower = q.lower()
    q_upper = q.upper()
    results: List[dict] = []

    # A股 (全量: 内置 CN_NAME_HINTS + akshare stock_info_a_code_name 补全 ~5500 只)
    # iter 163: 之前只有内置 ~100 条, 昊华科技/天齐锂业等大量股票搜不到。
    for code, name in _get_cn_name_map().items():
        if q in code or q in name:
            results.append({"code": code, "name": name, "market": "cn"})

    # 港股(注意: 港股代码 keys 是 5 位, 输入可能是 5/7 位, 大小写)
    for code, name in HK_NAME_HINTS.items():
        if q in code or q in name or q_upper in code or q_lower in name.lower():
            results.append({"code": code, "name": name, "market": "hk"})

    # 美股
    for code, name in US_NAME_HINTS.items():
        if q_upper in code or q_lower in name.lower() or q in name:
            results.append({"code": code, "name": name, "market": "us"})

    # 按市场分组排序(优先显示与查询精确匹配的)
    def sort_key(item: dict) -> tuple:
        starts_with_code = item["code"].lower().startswith(q_lower)
        starts_with_name = item["name"].lower().startswith(q_lower)
        return (not starts_with_code, not starts_with_name, item["code"])

    results.sort(key=sort_key)
    return results[:limit]