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
import threading
import os
from collections import OrderedDict
from typing import Any, Callable, Optional, Dict, List, Tuple
from threading import Lock, RLock

from db.migrate import get_conn

logger = logging.getLogger(__name__)


def _f(value: Any, default: float = 0.0) -> float:
    """安全转 float —— 复用主项目 data_provider.realtime_types.safe_float。

    ★ 探索期修复：akshare 在停牌/无报价行返回 "-"（字符串），旧代码
      `float(r.get("最新价", 0) or 0)` 对 "-" 抛 ValueError（`"-" or 0` 仍是 "-"），
      被外层 `except` 捕获后整只股票行情失败并 `record_failure` ——
      一只停牌股就能把主源健康度打成不可用，批量行情里还会拖垮整批。
      safe_float 已处理 None / "" / "-" / "--"，这里再兜住 NaN（NaN 会污染
      后续的均值/回测计算，且 `nan != nan` 使比较判断全部失效）。
    """
    try:
        from data_provider.realtime_types import safe_float
        parsed = safe_float(value, default=None)
    except Exception:  # noqa: BLE001 - 主项目模块不可用时退回本地实现
        try:
            if value is None:
                return default
            if isinstance(value, str) and value.strip() in ("", "-", "--"):
                return default
            parsed = float(value)
        except (TypeError, ValueError):
            return default
    if parsed is None:
        return default
    result = float(parsed)
    if result != result:  # NaN
        return default
    return result

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
                "price": _f(r.get("最新价")),
                "change_pct": _f(r.get("涨跌幅")),
                "volume": _f(r.get("成交量")),
                "amount": _f(r.get("成交额")),
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
                    "open": _f(row["开盘"]),
                    "high": _f(row["最高"]),
                    "low": _f(row["最低"]),
                    "close": _f(row["收盘"]),
                    "volume": _f(row["成交量"]),
                    "amount": _f(row.get("成交额")),
                })
            self.record_success()
            return result
        except Exception as e:
            self.record_failure(str(e)[:200])
            return []


# ============================================================================
# Baostock 适配器（备用，免费）
# ============================================================================
def _to_baostock_code(code: str) -> str:
    """把 600519 转成 baostock 的 sh.600519（9 位带点）；已带前缀则原样返回。

    ★ 修复(H8)：北交所（4/8 开头，如 430047/830799）baostock 不支持，
    旧实现映射成 sz.* 只会拿到空数据。改为返回 None 让调用方明确降级。
    """
    if code.startswith(("sh", "sz")):
        return code if "." in code else f"{code[:2]}.{code[2:]}"
    # 北交所：baostock 不提供 → 返回 None（调用方应跳过此源）
    if code.startswith(("4", "8")):
        return None
    prefix = "sh" if code.startswith("6") else "sz"
    return f"{prefix}.{code}"


def _aggregate_kline_bars(bars: List[Dict[str, Any]], group_size: int) -> List[Dict[str, Any]]:
    """把（日/月）K 线按 group_size 聚合（oldest-first）：每组的 open=首根 open、
    high=max、low=min、close=末根 close、volume/amount 求和，date=首根日期。

    ★ Round 24：baostock 原生只支持 d/w/m，quarterly/yearly 用月线聚合得到。"""
    out: List[Dict[str, Any]] = []
    for i in range(0, len(bars), group_size):
        group = bars[i:i + group_size]
        if not group:
            break
        out.append({
            "date": group[0]["date"],
            "open": group[0]["open"],
            "high": max(b["high"] for b in group),
            "low": min(b["low"] for b in group),
            "close": group[-1]["close"],
            "volume": sum(b["volume"] for b in group),
            "amount": sum(b["amount"] for b in group),
        })
    return out


class BaostockAdapter(DataSourceAdapter):
    def __init__(self):
        super().__init__("baostock")
        self._bs = None
        # ★ 修复(迭代1-并发损坏)：baostock 是单 TCP 连接、非线程安全 ——
        #   apscheduler 定时任务（每5分钟异动扫描）与用户请求并发调用时，
        #   多线程同时读同一 socket → gzip 解压损坏（Error -5/-3 while decompressing）
        #   → 数据源全部失败回退 mock。加 RLock 串行化所有 baostock 访问。
        self._lock = RLock()

    def _ensure_bs(self):
        with self._lock:
            if self._bs is None:
                import baostock as bs
                bs.login()
                self._bs = bs
            return self._bs

    def _is_connection_error(self, e: Exception) -> bool:
        """判断是否为 baostock 连接类错误（连接被服务端关闭/重置）。

        ★ 修复(迭代2-连接失效)：baostock 单连接在空闲一段时间后会被服务端关闭，
        缓存的 _bs 连接失效 → 后续查询抛 WinError 10053/10054（WSAECONNABORTED/
        WSAECONNRESET）→ 数据源全部失败回退 mock。识别这类错误以便重置连接重连。
        """
        import socket
        name = type(e).__name__
        if isinstance(e, (ConnectionResetError, ConnectionAbortedError, ConnectionError, socket.error)):
            return True
        # WinError 10053/10054 等 socket 级错误（Windows 下 OSError 包装）
        if isinstance(e, OSError):
            if getattr(e, "winerror", None) in (10053, 10054, 10058, 10060):
                return True
            if getattr(e, "errno", None) in (104, 110, 32):
                return True
        # 文本匹配兜底（baostock 内部可能包装成通用异常）
        msg = str(e).lower()
        for kw in ("10053", "10054", "reset", "aborted", "connection", "broken pipe", "decompress"):
            if kw in msg:
                return True
        return False

    def _reset_connection(self):
        """连接失效时重置：尝试 logout + 置 None，下次调用重新 login。"""
        try:
            if self._bs is not None:
                try:
                    self._bs.logout()
                except Exception:
                    pass
        finally:
            self._bs = None

    def fetch_quote(self, code: str) -> Optional[Dict[str, Any]]:
        """★ Round 24 修复(H5)：Baostock 此前未实现 fetch_quote（继承 NotImplementedError 被吞，
           永远不是真实行情源，只会落到 mock）。用 baostock 最近日线做 best-effort 行情回退。
           baostock 无实时快照，这里用最新交易日 K 线收盘价近似现价。"""
        # ★ 修复(迭代1-并发损坏)：整个查询+遍历包在锁内 —— baostock 单连接非线程安全，
        #   线程 A 遍历 rs 时线程 B 发新查询会污染同一 socket 的流。
        with self._lock:
            try:
                bs = self._ensure_bs()
                bs_code = _to_baostock_code(code)
                # ★ 修复(H8)：北交所不支持 → 直接返回 None 让调用方明确降级
                if bs_code is None:
                    return None
                today = time.strftime("%Y-%m-%d")
                start = time.strftime("%Y-%m-%d", time.localtime(time.time() - 5 * 86400))
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,volume,amount",
                    start_date=start, end_date=today,
                    frequency="d", adjustflag="2",
                )
                # ★ Round 50 修复(CRITICAL)：baostock 连接失效时不抛异常，而是返回
                #   error_code != "0"（如 10002007 网络接收错误 / 10001001 未登录）。
                #   旧代码只 while error_code=="0" 取行 → 空结果当"无数据"静默返回 None，
                #   不重置连接 → 缓存失效连接永久抛错 → 全链回退 mock 假数据（用户无感知）。
                #   这里显式检查 error_code：非 0 视为连接/会话级错误 → 重置连接待重连，
                #   并返回 None 让 fallback 继续（而非静默吞掉）。
                if rs.error_code != "0":
                    logger.warning(
                        "baostock quote error_code=%s error_msg=%s for %s, resetting connection",
                        rs.error_code, rs.error_msg, code,
                    )
                    self._reset_connection()
                    return None
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
                if not rows:
                    return None
                last = rows[-1]
                price = float(last[4] or 0)
                if price <= 0:
                    return None
                prev_close = float(rows[-2][4]) if len(rows) >= 2 and rows[-2][4] else price
                change_pct = (price - prev_close) / prev_close * 100 if prev_close else 0.0
                self.record_success()
                return {
                    "code": code, "name": code,
                    "price": price, "change_pct": round(change_pct, 2),
                    "open": float(last[1] or 0), "high": float(last[2] or 0), "low": float(last[3] or 0),
                    "volume": float(last[5] or 0), "amount": float(last[6] or 0),
                    "source": "baostock",
                }
            except Exception as e:
                self.record_failure(str(e)[:200])
                # ★ 修复(迭代2-连接失效)：连接类错误 → 重置连接，下次调用自动重连，
                #   否则缓存的失效连接会一直抛错 → 永久回退 mock。
                if self._is_connection_error(e):
                    logger.warning("baostock 连接失效(%s)，重置连接待重连", type(e).__name__)
                    self._reset_connection()
                return None

    def fetch_kline(self, code: str, period: str = "daily", days: int = 120) -> List[Dict[str, Any]]:
        # ★ Round 24 修复：baostock 的 query_history_k_data_plus 支持 frequency d/w/m，
        #   旧代码硬编码 "d" → weekly/monthly 也返回日线（period 被忽略）。
        #   quarterly/yearly 用月线聚合。
        # ★ 修复(迭代1-并发损坏)：整个查询+遍历包在锁内（同 fetch_quote）。
        with self._lock:
            try:
                bs = self._ensure_bs()
                end = time.strftime("%Y-%m-%d")
                start = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
                bs_code = _to_baostock_code(code)
                # ★ 修复(H8)：北交所不支持 → 返回空列表让调用方明确降级
                if bs_code is None:
                    return []
                freq = {"daily": "d", "weekly": "w", "monthly": "m"}.get(period, "m")
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,volume,amount",
                    start_date=start, end_date=end,
                    frequency=freq, adjustflag="2",
                )
                # ★ Round 50 修复(CRITICAL)：同 fetch_quote —— baostock 连接失效时返回
                #   error_code != "0"（不抛异常），旧代码把空结果当"无数据"静默返回 [],
                #   不重置连接 → 永久回退 mock 假数据。显式检查并重置连接。
                if rs.error_code != "0":
                    logger.warning(
                        "baostock kline error_code=%s error_msg=%s for %s, resetting connection",
                        rs.error_code, rs.error_msg, code,
                    )
                    self._reset_connection()
                    return []
                result = []
                while rs.error_code == "0" and rs.next():
                    row = rs.get_row_data()
                    result.append({
                        "date": row[0],
                        "open": _f(row[1]),
                        "high": _f(row[2]),
                        "low": _f(row[3]),
                        "close": _f(row[4]),
                        "volume": _f(row[5]),
                        "amount": _f(row[6]),
                    })
                if period == "quarterly":
                    result = _aggregate_kline_bars(result, group_size=3)
                elif period == "yearly":
                    result = _aggregate_kline_bars(result, group_size=12)
                self.record_success()
                return result
            except Exception as e:
                self.record_failure(str(e)[:200])
                # ★ 修复(迭代2-连接失效)：同 fetch_quote —— 连接类错误重置连接待重连
                if self._is_connection_error(e):
                    logger.warning("baostock kline 连接失效(%s)，重置连接待重连", type(e).__name__)
                    self._reset_connection()
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
    """累加数据源健康度计数。

    ★ 探索期修复：旧实现是「SELECT 判断存在 → INSERT 或 UPDATE」的两步序列。
      `fetch_quotes_bulk` 会开 8 个并发 worker，首次遇到某个数据源时多个线程
      同时 SELECT 到空 → 同时 INSERT → `UNIQUE constraint failed:
      data_source_health.source_name`（实测 12 并发下 11 次失败）。
      异常被外层 except 吞成一条 warning，计数直接丢失，`get_health` 的熔断
      判断因此失真（源已恢复却仍被跳过）。
      改成 SQLite 的原子 UPSERT（3.24+，本项目 schema 已在用 ON CONFLICT）。
    """
    try:
        conn = get_conn()
        now = time.time()
        conn.execute(
            """INSERT INTO data_source_health
               (source_name, success_count, failure_count,
                last_success_at, last_failure_at, last_error, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_name) DO UPDATE SET
                 success_count = success_count + excluded.success_count,
                 failure_count = failure_count + excluded.failure_count,
                 last_success_at = COALESCE(excluded.last_success_at, last_success_at),
                 last_failure_at = COALESCE(excluded.last_failure_at, last_failure_at),
                 last_error      = COALESCE(excluded.last_error, last_error),
                 updated_at      = excluded.updated_at""",
            (source_name,
             1 if success else 0,
             0 if success else 1,
             now if success else None,
             now if not success else None,
             None if success else error,
             now),
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
        try:
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
            # ★ 修复(M9-2)：主源永久熔断 —— 只要 last_success_at=None 就恒返回 0.3，
            #   与失败发生多久无关，导致主源首次启动失败后永远被跳过（即使已恢复）。
            #   现在：最后一次失败若已超出 window_seconds，视为熔断已冷却，恢复可用。
            if last_s is None:
                if last_f < now - window_seconds:
                    # 失败已过期（窗口外）→ 允许重试主源；历史成功率低于 0.5 时给中等分
                    return 0.5 if total > 0 and (s_cnt or 0) / total < 0.5 else 0.7
                return 0.3
            if last_s >= last_f:
                return 0.7 if last_event_in_window else 0.9
            # 最后一次失败更近：若失败也在窗口外，说明已稳定一段时间，给中间分
            if last_f < now - window_seconds:
                return 0.6
            return 0.3 if last_event_in_window else 0.5
        finally:
            # ★ 探索期修复(fd 泄漏)：sqlite3.Connection 被 GC 跟踪，引用计数归零
            #   也不会立刻释放 fd，必须显式 close。get_health 被 is_available()
            #   高频调用（每次 fetch 前都会问一遍）。
            conn.close()
    except Exception:
        return 1.0


# ============================================================================
# 带 fallback 的高层 API
# ============================================================================
# ★ Round 19 修复(M9)：下方 441 行有唯一的 fetch_quote_with_fallback 实现（需用到 _mock_quote），
#   这里原先存在一份完全相同的死代码定义（被后定义覆盖），已删除。
# ★ Round 5：fallback 触发次数（当主源失败切到备用源）
_data_source_fallback_total = 0
# ★ 本轮修复(M-mock)：mock 回退触发次数（所有真实源都失败时的合成数据次数），
#   与 fallback_total 区分：fallback 是真实源之间切换，mock 是所有真实源都不可用。
_data_source_mock_total = 0


def get_data_source_metrics() -> Dict[str, int]:
    """暴露 data_source fallback 计数"""
    return {
        "data_source_fallback_total": _data_source_fallback_total,
        "data_source_mock_total": _data_source_mock_total,
    }


# ============================================================================
# Mock 标记识别（供业务侧统一判断"拿到的是不是假数据"）
# ============================================================================
# mock 标识：
#   - dict 类型（行情/某条K线）：source == "mock"
#   - list 类型（K 线数组）：list 实例上挂 is_mock=True 属性
# 这样调用方一行 is_mock_payload(x) 就能识别，避免各处硬编码字符串。
_MOCK_SOURCE_TAG: str = "mock"


def is_mock_payload(payload: Any) -> bool:
    """判断从 data_source 拿到的是不是 mock 数据。

    ★ 本轮修复(M-mock)：让 mock 对调用方可见。业务侧（模拟盘下单、异动告警）
    必须基于 is_mock_payload(x) 拒绝把假数据用于成交/触发决策，
    否则 sandbox/网络受限时会把"按 code 字符串 hash 编造的行情"当真数据成交/告警。

    适用类型：
      - dict（_mock_quote 产物，含 source=="mock"）
      - list（_mock_kline 产物，list 实例带 is_mock=True 属性）
      - None：返回 False（None 已经被上游拦截）
    """
    if payload is None:
        return False
    if isinstance(payload, dict):
        return payload.get("source") == _MOCK_SOURCE_TAG
    if isinstance(payload, list):
        # K 线 mock 产物：list 实例上挂了 is_mock=True 属性
        return bool(getattr(payload, "is_mock", False))
    return False


# ★ E2E 修复：单数据源行情硬超时（秒）——防止无内部超时的接口把整条 fallback 链拖死
_QUOTE_SOURCE_TIMEOUT = 15.0
# ★ 探索期修复(异动监控停摆)：K 线此前**完全没有**硬超时（只有行情有），
#   baostock 的 socket read 无内部超时时会永久阻塞。实测 2026-10-20 12:55 的
#   异动扫描卡在 fetch_kline 里超过 3 小时，APScheduler max_instances=1 于是把
#   之后每一轮都判为 "maximum number of running instances reached" 跳过 ——
#   异动告警从此完全静默，且日志里只有 WARNING，看不出根因。
_KLINE_SOURCE_TIMEOUT = 15.0


def fetch_quotes_bulk(
    codes: List[str],
    parallel: bool = True,
    budget_seconds: Optional[float] = None,
) -> Dict[str, Optional[Dict[str, Any]]]:
    """批量拉取行情，避免 N+1

    - codes: 股票代码列表
    - parallel: True 时用线程池并发拉取（推荐）；False 时顺序拉取
    - budget_seconds: 整批的总时间预算。到期后未完成的 code 直接记为 None 并返回，
      不再等待（上游数据源挂起时，单个 code 最坏要跑满整条 fallback 链，
      30 只叠加足以占满 Web 线程池）。None 表示沿用旧行为、不设总预算。
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
        from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
        max_workers = min(8, len(unique_codes))
        deadline = (time.monotonic() + budget_seconds) if budget_seconds else None
        # 不用 with：块退出会 join 所有 worker，超时形同虚设。改为手动 shutdown
        # 且不等待未完成任务，让挂起的上游调用留在后台线程里自生自灭。
        ex = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {ex.submit(fetch_quote_with_fallback, c): c for c in unique_codes}
            pending = set(futures)
            while pending:
                # 必须用 wait(timeout=) 主动限时：as_completed 只在有 future 完成时
                # 才返回，整批都卡在上游时它会一直阻塞到最慢的那个跑完。
                remaining = None
                if deadline is not None:
                    remaining = max(0.0, deadline - time.monotonic())
                done, pending = wait(pending, timeout=remaining, return_when=FIRST_COMPLETED)
                for fut in done:
                    c = futures[fut]
                    try:
                        result[c] = fut.result(timeout=0)
                    except Exception as e:
                        logger.warning("bulk fetch %s failed: %s", c, e)
                        result[c] = None
                if deadline is not None and time.monotonic() >= deadline and pending:
                    logger.warning(
                        "bulk fetch budget %.1fs exhausted; %d/%d codes unresolved "
                        "(returning partial quotes)",
                        budget_seconds, len(pending), len(unique_codes),
                    )
                    break
        finally:
            ex.shutdown(wait=False)
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
    # ★ 迭代2 修复(M4 一致性)：明显无效代码返回空（不再生成合成 K 线），与 fetch_quote 行为一致
    if not _is_plausible_ashare_code(code):
        logger.info("fetch_kline invalid code, returning empty: %r", code)
        return []
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    for src in _FALLBACK_ORDER:
        adapter = _get_adapter(src)
        if adapter is None:
            continue
        try:
            # ★ 探索期修复(异动监控停摆)：与 fetch_quote_with_fallback 相同的
            #   daemon 线程 + join(timeout) 硬超时。baostock / akshare 的 K 线
            #   接口都可能无内部超时地永久阻塞，此前一条卡死的连接会让整个
            #   扫描任务挂住（实测 >3h），拖死后续所有定时轮次。
            _box: list = []
            def _fetch():
                try:
                    _box.append(("ok", adapter.fetch_kline(code, period=period, days=days)))
                except BaseException as _e:  # noqa: BLE001
                    _box.append(("err", _e))
            _t = threading.Thread(target=_fetch, daemon=True)
            _t.start()
            _t.join(_KLINE_SOURCE_TIMEOUT)
            if _t.is_alive():
                raise TimeoutError(f"{src} kline timed out after {_KLINE_SOURCE_TIMEOUT}s")
            _state, _val = _box[0]
            if _state == "err":
                raise _val
            result = _val
        except Exception as e:
            logger.warning("fetch_kline via %s failed: %s", src, e)
            # ★ 探索期修复(熔断对超时失效)：同 fetch_quote —— 超时在调用方
            #   线程抛出，适配器内部的 record_failure 永远执行不到，健康面板
            #   因此把一直超时的源显示成 100% 健康。只在超时路径补记。
            if isinstance(e, TimeoutError):
                adapter.record_failure(str(e)[:200])
            continue
        if result:
            _cache_set(cache_key, result, cache_ttl)
            return result
    # ★ Round 7：mock fallback — sandbox / 网络受限环境保证可视化不空白
    # ★ 本轮修复(M-mock)：递增 _data_source_mock_total 计数器，
    #   让 /metrics 或 /health 能观察到"系统正在用假数据"。
    global _data_source_mock_total
    _data_source_mock_total += 1
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
    # ★ 修复(M9)：n_bars 下限从 10 提到 30 —— 旧实现 weekly/monthly 周期下
    #   （如 days=60, monthly → max(10, 2)=10 根）数据点过少，技术分析
    #   （BOLL period=20 / 波峰波谷 window=3 需 2w+1=7 点）窗口自动收缩、结果失真。
    #   保证至少 30 根，指标计算有足够数据。
    n_bars = max(30, days // days_per_bar)
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
    # ★ 本轮修复(M-mock)：用 list 子类挂 is_mock 标记 —— CPython 原生 list 不支持
    #   自定义属性（无 __dict__），setattr(rows, ...) 会抛 AttributeError，故不能直接
    #   rows.is_mock = True。改用 MockKlineList 子类实例，业务侧仍 isinstance(x, list) 兼容。
    try:
        marked_rows: List[Dict[str, Any]] = _MockKlineList(rows)
        setattr(marked_rows, "is_mock", True)
        return marked_rows
    except Exception:
        # 极端环境下若子类/属性赋值仍失败，回退到 list，避免调用方崩溃
        return rows


class _MockKlineList(list):
    """仅用于挂载 is_mock 标记的 list 子类。

    ★ 本轮修复(M-mock)：让 mock K 线对调用方可见，同时保持 list 接口不破坏现有消费者。
    之所以需要子类而非直接 rows.is_mock = True：
      CPython 原生 list 没有 __dict__，对其实例赋自定义属性会抛 AttributeError。
    """
    pass


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


def _is_plausible_ashare_code(code: str) -> bool:
    """判断是否像合法 A 股代码（6 位数字 + 合法前缀，或 sh./sz. 前缀）。

    ★ 迭代5：拦截明显无效代码（999999 / abc / 空串等）。
    A 股代码前缀：0(深主板) 3(创业板) 4/8(北交所) 6(沪主板/科创板688)。
    1/5=基金，2=深B，7/9=非股票 → 直接拒绝。
    """
    c = (code or "").strip()
    if not c:
        return False
    # sh.600519 / sz.000858
    if c.lower().startswith(("sh.", "sz.")):
        c = c[3:]
    if not (c.isdigit() and len(c) == 6):
        return False
    return c[0] in "03468"


def fetch_quote_with_fallback(code: str) -> Optional[Dict[str, Any]]:
    """依次尝试 fallback 链中的数据源

    ★ Round 7 升级：所有数据源失败时返回 mock 行情（sandbox / 离线仍可用）
    ★ Round 19 修复(M9)：备用源成功时递增 _data_source_fallback_total（原为死计数器恒 0）
    """
    global _data_source_fallback_total
    # ★ 迭代5 修复(M4)：明显无效代码直接返回 None（"无法识别代码"），
    #   不再为 999999/abc 等生成合成 mock 价（否则用户可对假代码下单成交）
    if not _is_plausible_ashare_code(code):
        logger.info("fetch_quote invalid code, returning None: %r", code)
        return None
    for src in _FALLBACK_ORDER:
        adapter = _get_adapter(src)
        if adapter is None:
            continue
        if not adapter.is_available() and src == _FALLBACK_ORDER[0]:
            logger.info("Skipping unhealthy primary source %s", src)
            continue
        try:
            # ★ E2E 修复：akshare 等 quote 接口可能无内部超时地挂起（实测热力图
            #   请求 197s），fallback 链会倍增卡顿。daemon 线程 + join(timeout)
            #   实现真硬超时（ThreadPoolExecutor 的 with 退出会 join 挂起线程，
            #   超时形同虚设）；超时后线程自然泄漏为 daemon，按失败走下一源。
            _box: list = []
            def _fetch():
                try:
                    _box.append(("ok", adapter.fetch_quote(code)))
                except BaseException as _e:  # noqa: BLE001
                    _box.append(("err", _e))
            _t = threading.Thread(target=_fetch, daemon=True)
            _t.start()
            _t.join(_QUOTE_SOURCE_TIMEOUT)
            if _t.is_alive():
                raise TimeoutError(f"{src} quote timed out after {_QUOTE_SOURCE_TIMEOUT}s")
            _state, _val = _box[0]
            if _state == "err":
                raise _val
            result = _val
        except Exception as e:
            logger.warning("fetch_quote via %s failed: %s", src, e)
            # ★ 探索期修复(熔断对超时失效)：适配器自己在 try/except 里调
            #   record_failure，但超时是**调用方线程**抛的 —— 那个被抛弃的
            #   daemon 线程永远走不到 record_failure。实测 baostock 在日志里
            #   超时 58 次，health 表里却仍是 failure_count=0 / success 9079，
            #   于是 /ready 与 /data/health 宣称它 100% 健康，熔断判定形同虚设。
            #   只在超时这一条路径补记，避免与非超时错误重复计数
            #   （后者已由适配器内部记录）。
            if isinstance(e, TimeoutError):
                adapter.record_failure(str(e)[:200])
            continue
        if result is not None:
            if src != _FALLBACK_ORDER[0]:
                _data_source_fallback_total += 1
            return result
    # ★ 本轮修复(M-mock)：递增 _data_source_mock_total 计数器，
    #   让 /metrics 或 /health 能观察到"系统正在用假数据"。
    global _data_source_mock_total
    _data_source_mock_total += 1
    logger.info("All data sources failed for %s, returning mock quote", code)
    return _mock_quote(code)


# ============================================================================
# 内存缓存 (TTL + LRU 上限)
# ============================================================================
# ★ Round 4 修复：OrderedDict 实现 LRU+TTL，避免冷 key 累积撑爆内存
# ★ 修复(M1)：条目从 (ts, value) 改为 (ts, value, ttl) —— 旧实现 _cache_get 硬编码
#   60s TTL，fetch_kline_with_fallback(cache_ttl=300) 传参被忽略。
_cache: "OrderedDict[str, Tuple[float, Any, int]]" = OrderedDict()
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
        ts, val, ttl = item
        if time.time() - ts > ttl:
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
        _cache[key] = (time.time(), value, ttl)
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
    """所有数据源的健康度摘要

    ★ 探索期修复(fd 泄漏)：连接必须显式 close（sqlite3.Connection 被 GC 跟踪，
      引用计数归零不立刻释放 fd）。这是 /ready 的核心路径。
    """
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM data_source_health")
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return {
        "sources": rows,
        "primary": _FALLBACK_ORDER[0],
        "fallback_chain": _FALLBACK_ORDER,
        "cache": cache_stats(),
    }