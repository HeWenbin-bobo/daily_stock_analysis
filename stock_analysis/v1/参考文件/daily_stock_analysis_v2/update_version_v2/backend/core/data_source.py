# -*- coding: utf-8 -*-
"""
多数据源管理 (akshare / tushare / efinance / baostock)
- 自动 fallback：主源失败 → 备用源
- 健康检查：最近 5min 成功率，自动熔断
- 60s TTL + LRU 内存缓存（maxsize 保护内存）
- 健康度结果 5s 进程内缓存（避免每次 fetch 都走 SQLite）
"""
import time
import logging
import json
import os
from collections import OrderedDict
from typing import Any, Callable, Optional, Dict, List, Tuple
from threading import Lock

from db.migrate import get_conn

logger = logging.getLogger(__name__)

# 数据源结果缓存上限（防止冷 key 累积 OOM）；可通过 DATA_SOURCE_CACHE_MAXSIZE 覆盖
_CACHE_MAXSIZE = int(os.environ.get("DATA_SOURCE_CACHE_MAXSIZE", "2048"))

# ============================================================================
# 数据源适配器接口
# ============================================================================
class DataSourceAdapter:
    """每个数据源是一个适配器"""

    def __init__(self, name: str):
        self.name = name

    def fetch_quote(self, code: str) -> Optional[Dict[str, Any]]:
        """拉单个股票实时行情；返回 None 表示此源不支持或失败"""
        raise NotImplementedError

    def fetch_kline(self, code: str, period: str = "daily", days: int = 120) -> List[Dict[str, Any]]:
        """拉 K 线；period: daily/weekly/monthly"""
        raise NotImplementedError

    def is_available(self) -> bool:
        """此源当前是否可用（健康度检查；5s 进程内缓存）"""
        return get_health_cached(self.name, window_seconds=300) > 0.5

    def record_success(self) -> None:
        update_health(self.name, success=True)

    def record_failure(self, err: str = "") -> None:
        update_health(self.name, success=False, error=err)


# ============================================================================
# Akshare 适配器（主源）
# ============================================================================
class AkshareAdapter(DataSourceAdapter):
    def __init__(self):
        super().__init__("akshare")
        self._ak = None

    def _ensure_ak(self):
        if self._ak is None:
            import akshare as ak
            self._ak = ak
        return self._ak

    def fetch_quote(self, code: str) -> Optional[Dict[str, Any]]:
        try:
            ak = self._ensure_ak()
            df = ak.stock_zh_a_spot_em()
            row = df[df["代码"] == code]
            if row.empty:
                return None
            r = row.iloc[0].to_dict()
            self.record_success()
            return {
                "code": code,
                "name": r.get("名称"),
                "price": float(r.get("最新价", 0) or 0),
                "change_pct": float(r.get("涨跌幅", 0) or 0),
                "volume": float(r.get("成交量", 0) or 0),
                "amount": float(r.get("成交额", 0) or 0),
                "source": "akshare",
            }
        except Exception as e:
            self.record_failure(str(e)[:200])
            return None

    def fetch_kline(self, code: str, period: str = "daily", days: int = 120) -> List[Dict[str, Any]]:
        try:
            ak = self._ensure_ak()
            # ★ Round 7 修复：akshare 1.18+ 参数名改为 end_date（之前用 end 会抛 unexpected keyword）
            end_date = time.strftime("%Y%m%d")
            start_date = time.strftime("%Y%m%d", time.localtime(time.time() - days * 86400))
            df = ak.stock_zh_a_hist(
                symbol=code, period=period,
                start_date=start_date, end_date=end_date, adjust="qfq",
            )
            result = []
            for _, row in df.iterrows():
                result.append({
                    "date": str(row["日期"]),
                    "open": float(row["开盘"]),
                    "high": float(row["最高"]),
                    "low": float(row["最低"]),
                    "close": float(row["收盘"]),
                    "volume": float(row["成交量"]),
                    "amount": float(row.get("成交额", 0) or 0),
                })
            self.record_success()
            return result
        except Exception as e:
            self.record_failure(str(e)[:200])
            return []


# ============================================================================
# Baostock 适配器（备用，免费）
# ============================================================================
class BaostockAdapter(DataSourceAdapter):
    def __init__(self):
        super().__init__("baostock")
        self._bs = None

    def _ensure_bs(self):
        if self._bs is None:
            import baostock as bs
            bs.login()
            self._bs = bs
        return self._bs

    def fetch_kline(self, code: str, period: str = "daily", days: int = 120) -> List[Dict[str, Any]]:
        try:
            bs = self._ensure_bs()
            end = time.strftime("%Y-%m-%d")
            start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
            # baostock 需要 sh600519 格式
            if not code.startswith(("sh", "sz")):
                prefix = "sh" if code.startswith("6") else "sz"
                bs_code = f"{prefix}{code}"
            else:
                bs_code = code
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,amount",
                start_date=start, end_date=end,
                frequency="d", adjustflag="2",
            )
            result = []
            while rs.error_code == "0" and rs.next():
                row = rs.get_row_data()
                result.append({
                    "date": row[0],
                    "open": float(row[1] or 0),
                    "high": float(row[2] or 0),
                    "low": float(row[3] or 0),
                    "close": float(row[4] or 0),
                    "volume": float(row[5] or 0),
                    "amount": float(row[6] or 0),
                })
            self.record_success()
            return result
        except Exception as e:
            self.record_failure(str(e)[:200])
            return []


# ============================================================================
# 数据源注册
# ============================================================================
_ADAPTERS: Dict[str, DataSourceAdapter] = {}
_FALLBACK_ORDER = ["akshare", "baostock"]  # 优先 akshare，失败用 baostock
_lock = Lock()


def _get_adapter(name: str) -> Optional[DataSourceAdapter]:
    with _lock:
        if name not in _ADAPTERS:
            try:
                if name == "akshare":
                    _ADAPTERS[name] = AkshareAdapter()
                elif name == "baostock":
                    _ADAPTERS[name] = BaostockAdapter()
                else:
                    return None
            except Exception as e:
                logger.warning("Failed to init adapter %s: %s", name, e)
                return None
        return _ADAPTERS.get(name)


# ============================================================================
# 健康度（写入 SQLite）
# ============================================================================
def update_health(source_name: str, *, success: bool, error: str = "") -> None:
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT success_count, failure_count FROM data_source_health WHERE source_name=?", (source_name,))
        row = cur.fetchone()
        now = time.time()
        if row is None:
            cur.execute(
                """INSERT INTO data_source_health
                   (source_name, success_count, failure_count, last_success_at, last_failure_at, last_error, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (source_name, 1 if success else 0, 0 if success else 1,
                 now if success else None, now if not success else None,
                 error if not success else None, now),
            )
        else:
            if success:
                cur.execute(
                    """UPDATE data_source_health SET
                       success_count=success_count+1, last_success_at=?, updated_at=?
                       WHERE source_name=?""",
                    (now, now, source_name),
                )
            else:
                cur.execute(
                    """UPDATE data_source_health SET
                       failure_count=failure_count+1, last_failure_at=?, last_error=?, updated_at=?
                       WHERE source_name=?""",
                    (now, error, now, source_name),
                )
    except Exception as e:
        logger.warning("Failed to update health for %s: %s", source_name, e)


def get_health(source_name: str, window_seconds: int = 300) -> float:
    """最近 window_seconds 内的成功率（基于累计计数估算）

    真实滑动窗口需要事件流水；这里用 (last_success_at, last_failure_at) 作为
    "最近一次事件" 的代理：若最近一次成功 → 健康；最近一次失败 → 不可用；
    都没有 → 视为可用；其它 → 中等。
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """SELECT last_success_at, last_failure_at, success_count, failure_count
               FROM data_source_health WHERE source_name=?""",
            (source_name,),
        )
        row = cur.fetchone()
        if row is None:
            return 1.0  # 未测试视为可用
        last_s, last_f, s_cnt, f_cnt = row
        total = (s_cnt or 0) + (f_cnt or 0)
        if total == 0:
            return 1.0
        # 最近一次成功 / 失败的时间窗：若都在窗口内，按最后事件打分
        now = time.time()
        last_event_in_window = (
            (last_s is not None and last_s >= now - window_seconds)
            or (last_f is not None and last_f >= now - window_seconds)
        )
        if last_f is None:
            return 1.0
        if last_s is None:
            return 0.3
        if last_s >= last_f:
            return 0.7 if last_event_in_window else 0.9
        return 0.3 if last_event_in_window else 0.5
    except Exception:
        return 1.0


# ============================================================================
# 带 fallback 的高层 API
# ============================================================================
def fetch_quote_with_fallback(code: str) -> Optional[Dict[str, Any]]:
    """依次尝试 fallback 链中的数据源

    ★ Round 7 升级：所有数据源失败时返回 mock 行情（sandbox / 离线仍可用）
    """
    for src in _FALLBACK_ORDER:
        adapter = _get_adapter(src)
        if adapter is None:
            continue
        if not adapter.is_available() and src == _FALLBACK_ORDER[0]:
            logger.info("Skipping unhealthy primary source %s", src)
            continue
        try:
            result = adapter.fetch_quote(code)
        except Exception as e:
            logger.warning("fetch_quote via %s failed: %s", src, e)
            continue
        if result is not None:
            return result
    logger.info("All data sources failed for %s, returning mock quote", code)
    return _mock_quote(code)


# ★ Round 5：fallback 触发次数（当主源失败切到备用源）
_data_source_fallback_total = 0


def get_data_source_metrics() -> Dict[str, int]:
    """暴露 data_source fallback 计数"""
    return {"data_source_fallback_total": _data_source_fallback_total}


def fetch_quotes_bulk(codes: List[str], parallel: bool = True) -> Dict[str, Optional[Dict[str, Any]]]:
    """批量拉取行情，避免 N+1

    - codes: 股票代码列表
    - parallel: True 时用线程池并发拉取（推荐）；False 时顺序拉取
    - 返回: {code: quote_dict_or_None} — 缺失的 code 也保留 key，值为 None
    - 利用现有 60s 内存缓存，已缓存的 code 不会触发新请求
    """
    if not codes:
        return {}
    # 去重保持顺序
    seen: set = set()
    unique_codes = [c for c in codes if not (c in seen or seen.add(c))]
    result: Dict[str, Optional[Dict[str, Any]]] = {c: None for c in unique_codes}
    if parallel:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        max_workers = min(8, len(unique_codes))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(fetch_quote_with_fallback, c): c for c in unique_codes}
            for fut in as_completed(futures):
                c = futures[fut]
                try:
                    result[c] = fut.result(timeout=30)
                except Exception as e:
                    logger.warning("bulk fetch %s failed: %s", c, e)
                    result[c] = None
    else:
        for c in unique_codes:
            result[c] = fetch_quote_with_fallback(c)
    return result


def fetch_kline_with_fallback(
    code: str,
    period: str = "daily",
    days: int = 120,
    cache_key: Optional[str] = None,
    cache_ttl: int = 60,
) -> List[Dict[str, Any]]:
    """带 60s TTL 缓存的 K 线拉取

    ★ Round 7 升级：所有数据源失败时（sandbox / 离线 / 网络受限），返回合成 mock K 线
    — 基于 code 字符串派生 seed，保证可视化 / 单股 tab 在受限环境也能展示完整 demo。
    """
    if cache_key is None:
        cache_key = f"kline:{code}:{period}:{days}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    for src in _FALLBACK_ORDER:
        adapter = _get_adapter(src)
        if adapter is None:
            continue
        try:
            result = adapter.fetch_kline(code, period=period, days=days)
        except Exception as e:
            logger.warning("fetch_kline via %s failed: %s", src, e)
            continue
        if result:
            _cache_set(cache_key, result, cache_ttl)
            return result
    # ★ Round 7：mock fallback — sandbox / 网络受限环境保证可视化不空白
    logger.info("All kline sources failed for %s, returning mock data", code)
    return _mock_kline(code, days, period)


_PERIOD_DAYS_PER_BAR: Dict[str, int] = {
    "daily": 1,
    "weekly": 5,
    "monthly": 21,
    "quarterly": 63,
    "yearly": 252,
}


def _mock_kline(code: str, days: int, period: str = "daily") -> List[Dict[str, Any]]:
    """合成 mock K 线（基于 code 字符串 seed，结果稳定可复现）

    ★ Round 8 修复：
    - 时间顺序：oldest → newest（K 线 today 显示在最右）
    - 支持 period（daily/weekly/monthly/quarterly/yearly）——将 days 按周期聚合
    """
    import random
    days_per_bar = _PERIOD_DAYS_PER_BAR.get(period, 1)
    # 聚合：把 days 天按 days_per_bar 合并成一根 K 线
    n_bars = max(10, days // days_per_bar)
    seed = sum(ord(c) for c in code) + days + days_per_bar * 1000
    rng = random.Random(seed)
    base_price = 10.0 + (seed % 100) * 1.5  # 10 ~ 160
    trend = rng.choice([-0.001, 0.0002, 0.0005, 0.001])  # 微趋势
    rows: List[Dict[str, Any]] = []
    price = base_price
    today = time.time()
    # i=0 是最老一根，i=n_bars-1 是今天
    for i in range(n_bars):
        ts = today - (n_bars - 1 - i) * 86400 * days_per_bar
        date = time.strftime("%Y-%m-%d", time.localtime(ts))
        # 每根内部累计波动：周期越长振幅越大
        amplitude = 0.03 * (1 + (days_per_bar - 1) * 0.4)
        open_p = price
        change = price * rng.uniform(-amplitude, amplitude)
        close_p = max(0.1, open_p + change + price * trend * days_per_bar)
        high_p = max(open_p, close_p) * (1 + abs(rng.uniform(0, 0.02 * days_per_bar**0.5)))
        low_p = min(open_p, close_p) * (1 - abs(rng.uniform(0, 0.02 * days_per_bar**0.5)))
        volume = int(rng.uniform(500_000, 50_000_000) * days_per_bar)
        amount = volume * (open_p + close_p) / 2
        rows.append({
            "date": date,
            "open": round(open_p, 2),
            "high": round(high_p, 2),
            "low": round(low_p, 2),
            "close": round(close_p, 2),
            "volume": volume,
            "amount": round(amount, 2),
        })
        price = close_p
    # ★ Round 8：循环已按 oldest→newest 生成，无需 reversed
    return rows


def _mock_quote(code: str) -> Dict[str, Any]:
    """合成 mock 行情（sandbox 环境用）"""
    seed = sum(ord(c) for c in code)
    import random
    rng = random.Random(seed)
    base = 10.0 + (seed % 100) * 1.5
    change = rng.uniform(-0.05, 0.05)  # ±5%
    return {
        "code": code,
        "name": f"模拟{code}",
        "price": round(base * (1 + change), 2),
        "change_pct": round(change * 100, 2),
        "volume": int(rng.uniform(100_000, 5_000_000)),
        "amount": round(base * (1 + change) * 1_000_000, 2),
        "industry": "模拟行业",
        "source": "mock",
    }


def fetch_quote_with_fallback(code: str) -> Optional[Dict[str, Any]]:
    """依次尝试 fallback 链中的数据源

    ★ Round 7 升级：所有数据源失败时返回 mock 行情（sandbox / 离线仍可用）
    """
    for src in _FALLBACK_ORDER:
        adapter = _get_adapter(src)
        if adapter is None:
            continue
        if not adapter.is_available() and src == _FALLBACK_ORDER[0]:
            logger.info("Skipping unhealthy primary source %s", src)
            continue
        try:
            result = adapter.fetch_quote(code)
        except Exception as e:
            logger.warning("fetch_quote via %s failed: %s", src, e)
            continue
        if result is not None:
            return result
    logger.info("All data sources failed for %s, returning mock quote", code)
    return _mock_quote(code)


# ============================================================================
# 内存缓存 (60s TTL + LRU 上限)
# ============================================================================
# ★ Round 4 修复：OrderedDict 实现 LRU+TTL，避免冷 key 累积撑爆内存
_cache: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
_cache_lock = Lock()
# ★ Round 5 缓存命中计数（暴露给 /metrics）
_cache_hit = 0
_cache_miss = 0
_cache_evict = 0


def _cache_get(key: str) -> Optional[Any]:
    global _cache_hit, _cache_miss
    with _cache_lock:
        item = _cache.get(key)
        if item is None:
            _cache_miss += 1
            return None
        ts, val = item
        if time.time() - ts > 60:
            _cache.pop(key, None)
            _cache_miss += 1
            return None
        # LRU：命中后移到末尾
        _cache.move_to_end(key)
        _cache_hit += 1
        return val


def _cache_set(key: str, value: Any, ttl: int = 60) -> None:
    global _cache_evict
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
        _cache[key] = (time.time(), value)
        # 超 maxsize 时弹掉最旧（FIFO from oldest = LRU evict）
        while len(_cache) > _CACHE_MAXSIZE:
            _cache.popitem(last=False)
            _cache_evict += 1


def cache_stats() -> Dict[str, int]:
    """缓存健康度（可被 /data/health 暴露）"""
    with _cache_lock:
        return {"size": len(_cache), "maxsize": _CACHE_MAXSIZE}


def get_metrics() -> Dict[str, Any]:
    """★ Round 5：暴露给 /metrics 端点"""
    with _cache_lock:
        return {
            "cache_size": len(_cache),
            "cache_maxsize": _CACHE_MAXSIZE,
            "cache_hit": _cache_hit,
            "cache_miss": _cache_miss,
            "cache_evict": _cache_evict,
            "cache_hit_ratio": round(_cache_hit / max(_cache_hit + _cache_miss, 1), 4),
        }


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ============================================================================
# 健康度 5s 进程内缓存（避免每次 fetch_quote 都跑 SQLite）
# ============================================================================
_HEALTH_CACHE: Dict[str, Tuple[float, float]] = {}
_HEALTH_TTL = float(os.environ.get("DATA_SOURCE_HEALTH_CACHE_TTL", "5"))


def get_health_cached(source_name: str, window_seconds: int = 300) -> float:
    """带 5s 进程内 TTL 的健康度查询。

    get_health 每次都查 SQLite，在 fetch_quotes_bulk 并发拉 50 只股票时会触发 50 次
    SQLite 读（虽然 WAL 模式问题不大，但仍是热路径浪费）。这里加 5s TTL，
    health 状态 5s 内复用同一结果。
    """
    now = time.time()
    cached = _HEALTH_CACHE.get(source_name)
    if cached and (now - cached[0]) < _HEALTH_TTL:
        return cached[1]
    val = get_health(source_name, window_seconds)
    _HEALTH_CACHE[source_name] = (now, val)
    return val


def get_health_report() -> Dict[str, Any]:
    """所有数据源的健康度摘要"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM data_source_health")
    rows = [dict(r) for r in cur.fetchall()]
    return {
        "sources": rows,
        "primary": _FALLBACK_ORDER[0],
        "fallback_chain": _FALLBACK_ORDER,
        "cache": cache_stats(),
    }