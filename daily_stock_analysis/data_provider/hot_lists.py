# -*- coding: utf-8 -*-
"""
===================================
三榜独立抓取与交集计算
===================================

职责：
1. 独立抓取东方财富 / 雪球 / 同花顺 三个平台的实时热门榜单
2. 标准化股票代码（去前缀、补 0）
3. 计算三榜交集，剔除科创板（688*）等非主 A 股
4. 提供进程内 TTL 缓存（避免短时间内重复抓取）

设计说明：
- 不与 `AkshareFetcher.get_hot_stocks()` 的 fallback 链耦合
  - 现有 fetcher 只返回单一来源，本模块需要 3 个**独立**的列表
  - 即便其中一个失败也要继续抓取其余
- 同花顺通过 akshare 的 `stock_hot_ths_*d` 系列接口按时间维度降级尝试
- akshare 的 `stock_hot_rank_em()` / `stock_hot_follow_xq()` 实际最多返回约 100 条
  - 任务要求"前 1000 名"在工程上等同于"全榜"（已记录此假设）
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 进程内缓存（5 分钟 TTL，避免同一次分析中重复抓取）
# ---------------------------------------------------------------------------
_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SECONDS = 300


def _cache_get(key: str) -> Optional[List[Dict[str, Any]]]:
    with _CACHE_LOCK:
        item = _CACHE.get(key)
        if not item:
            return None
        ts, data = item
        if time.time() - ts > _CACHE_TTL_SECONDS:
            return None
        # 拷贝一份以防外部修改
        return list(data)


def _cache_set(key: str, data: List[Dict[str, Any]]) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), list(data))


def clear_cache() -> None:
    """清空缓存（测试 / 调试用）"""
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# 标准化与过滤
# ---------------------------------------------------------------------------
def normalize_code(code: Any) -> str:
    """
    标准化股票代码到 6 位字符串。

    - 去 SH / SZ / BJ / sh / sz / bj 前缀
    - 数字补 0 到 6 位
    - 去除空白
    """
    if code is None:
        return ""
    s = str(code).strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if s.startswith(prefix):
            s = s[2:]
            break
    # 仅保留数字
    s = "".join(ch for ch in s if ch.isdigit())
    if not s:
        return ""
    return s.zfill(6)


def is_a_share_main(code: str) -> bool:
    """
    判断是否为主板 / 中小板 / 创业板 A 股。

    规则（与任务要求一致）：
    - 剔除 688* 科创板
    - 仅保留 60*（沪市主板）、00*（深市主板/中小板）、30*（创业板）
    - 北交所（8*/4*）和 B 股不纳入
    """
    if not code or len(code) != 6:
        return False
    if not code.isdigit():
        return False
    if code.startswith("688"):
        return False
    return code.startswith(("60", "00", "30"))


# ---------------------------------------------------------------------------
# 抓取各榜单
# ---------------------------------------------------------------------------
def _try_akshare_call(func, *, name: str, retries: int = 2) -> Optional[Any]:
    """
    调用 akshare 函数，带简单重试与异常抑制。
    返回 DataFrame 或 None。
    """
    last_err = ""
    for i in range(retries):
        try:
            df = func()
            if df is not None and not df.empty:
                return df
            last_err = "empty dataframe"
        except Exception as e:
            last_err = str(e)
            logger.debug("[hot_lists] %s 第 %d 次失败: %s", name, i + 1, e)
            time.sleep(0.5 * (i + 1))
    if last_err:
        logger.warning("[hot_lists] %s 抓取失败: %s", name, last_err)
    return None


def fetch_eastmoney_hot(n: int = 200) -> List[Dict[str, Any]]:
    """
    东方财富人气榜（ak.stock_hot_rank_em）。

    返回字段：code / name / rank（可能缺失）/ source
    """
    cache_key = "eastmoney"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[:n]

    try:
        import akshare as ak
    except Exception as e:
        logger.error("[hot_lists] 缺少 akshare 依赖: %s", e)
        return []

    df = _try_akshare_call(ak.stock_hot_rank_em, name="东方财富人气榜")
    rows: List[Dict[str, Any]] = []
    if df is not None and not df.empty:
        for idx, (_, row) in enumerate(df.iterrows(), 1):
            code = normalize_code(row.get("代码"))
            if not code:
                continue
            rows.append({
                "code": code,
                "name": str(row.get("股票名称", "")).strip(),
                "rank": int(row.get("当前排名", idx) or idx),
                "source": "东方财富人气榜",
            })

    logger.info("[hot_lists] 东方财富人气榜: 抓取到 %d 只", len(rows))
    _cache_set(cache_key, rows)
    return rows[:n]


def fetch_xueqiu_hot(n: int = 200) -> List[Dict[str, Any]]:
    """
    雪球关注榜（ak.stock_hot_follow_xq symbol='最热门'）。
    """
    cache_key = "xueqiu"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[:n]

    try:
        import akshare as ak
    except Exception as e:
        logger.error("[hot_lists] 缺少 akshare 依赖: %s", e)
        return []

    df = _try_akshare_call(
        lambda: ak.stock_hot_follow_xq(symbol="最热门"),
        name="雪球关注榜",
    )
    rows: List[Dict[str, Any]] = []
    if df is not None and not df.empty:
        for idx, (_, row) in enumerate(df.iterrows(), 1):
            code = normalize_code(row.get("股票代码"))
            if not code:
                continue
            rows.append({
                "code": code,
                "name": str(row.get("股票简称", "")).strip(),
                "rank": idx,
                "source": "雪球关注榜",
            })

    logger.info("[hot_lists] 雪球关注榜: 抓取到 %d 只", len(rows))
    _cache_set(cache_key, rows)
    return rows[:n]


# ---------------------------------------------------------------------------
# 方案 A：抓取"全市场活跃"榜（akshare stock_zh_a_spot 排序）
# 这是真正的"第三榜"，对现有 2 榜（东财 + 雪球）是独立维度
# ---------------------------------------------------------------------------
def fetch_full_market_hot(n: int = 200) -> List[Dict[str, Any]]:
    """
    全市场 A 股活跃榜 - 用 akshare.stock_zh_a_spot() 拉全市场实时行情（5526+ 只）
    按"换手率"降序排序，取前 N 只作为"资金最活跃"的代理指标。
    """
    cache_key = "full_market"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[:n]

    try:
        import akshare as ak
    except Exception as e:
        logger.error("[hot_lists] 缺少 akshare: %s", e)
        return []

    rows: List[Dict[str, Any]] = []
    sort_col_used = ""
    try:
        df = ak.stock_zh_a_spot()
        if df is None or df.empty:
            logger.warning("[hot_lists] stock_zh_a_spot() 返回空")
        else:
            # 不同 akshare 版本列名略不同，尝试多个候选
            sort_col = None
            for cand in ["换手率", "turnover", "换手", "turnover_rate"]:
                if cand in df.columns:
                    sort_col = cand
                    break
            if sort_col is None:
                for cand in ["涨跌幅", "change_pct", "change"]:
                    if cand in df.columns:
                        sort_col = cand
                        break
            if sort_col is None:
                logger.warning("[hot_lists] stock_zh_a_spot 列名未知: %s", list(df.columns)[:6])
            else:
                # 排序前先去掉科创板（即便用户没剔除，前置过滤不影响后续 filter_a_share）
                if "代码" in df.columns:
                    df = df[~df["代码"].astype(str).str.startswith("688")]
                df_top = df.sort_values(sort_col, ascending=False).head(n)
                sort_col_used = sort_col
                for idx, (_, row) in enumerate(df_top.iterrows(), 1):
                    code = normalize_code(row.get("代码"))
                    if not code:
                        continue
                    rows.append({
                        "code": code,
                        "name": str(row.get("名称", "")).strip(),
                        "rank": idx,
                        "source": f"全市场活跃榜(底层=stock_zh_a_spot, 排序={sort_col})",
                    })
    except Exception as e:
        logger.warning("[hot_lists] 全市场活跃榜失败: %s", e)

    if rows:
        logger.info("[hot_lists] 全市场活跃榜: %d 只 (排序=%s, 底层=stock_zh_a_spot)", len(rows), sort_col_used)
    _cache_set(cache_key, rows)
    return rows[:n]


# ---------------------------------------------------------------------------
# 方案 B：fetch_eastmoney_hot 加 fallback - 主榜失败时降级到"全市场活跃"
# ---------------------------------------------------------------------------
def fetch_eastmoney_hot(n: int = 200) -> List[Dict[str, Any]]:
    """
    东方财富人气榜。主榜失败时降级到 fetch_full_market_hot（"持续放量"代理）。
    """
    cache_key = "eastmoney"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[:n]

    try:
        import akshare as ak
    except Exception as e:
        logger.error("[hot_lists] 缺少 akshare 依赖: %s", e)
        return []

    # 主榜尝试
    rows = []
    try:
        df = ak.stock_hot_rank_em()
        if df is not None and not df.empty:
            for idx, (_, row) in enumerate(df.head(n).iterrows(), 1):
                code = normalize_code(row.get("代码"))
                if not code:
                    continue
                rows.append({
                    "code": code,
                    "name": str(row.get("股票名称", "")).strip(),
                    "rank": idx,
                    "source": "东方财富人气榜",
                })
    except Exception as e:
        logger.debug("[hot_lists] 东方财富主榜失败: %s", e)

    # Fallback: 全市场活跃榜（按换手率排序）
    if not rows:
        logger.info("[hot_lists] 东方财富主榜失败，降级到全市场活跃榜")
        fb_rows = fetch_full_market_hot(n)
        for r in fb_rows:
            # 标记 source 为降级路径
            r2 = dict(r)
            r2["source"] = "东方财富人气榜(fallback=全市场活跃)"
            rows.append(r2)

    if rows:
        logger.info("[hot_lists] 东方财富人气榜: %d 只", len(rows))
    _cache_set(cache_key, rows)
    return rows[:n]


# ---------------------------------------------------------------------------
# 抓取"同花顺"：使用 Playwright + 持久化浏览器上下文拉 THS 排行榜 SPA
# 替代 akshare 已下线的 stock_hot_ths_* 接口
# ---------------------------------------------------------------------------
def _persist_dir() -> str:
    """持久化浏览器上下文目录（用于缓存 cookies / 减少反爬）"""
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache", "ths_pw")
    os.makedirs(d, exist_ok=True)
    return d


def _scrape_ths_rank_page(page, url: str, label: str, n: int) -> List[Dict[str, Any]]:
    """
    给一个已加载好的 Playwright page 和 url，从渲染完的 DOM 里提取股票代码/名称。
    """
    import re as _re
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(5000)
    except Exception as e:
        logger.debug("[hot_lists] THS page goto %s err: %s", url, e)
        return []
    try:
        html = page.content()
    except Exception:
        return []

    raw_codes = _re.findall(r"(?:sh|sz)?((?:60|00|30)\d{4})", html)
    raw_codes = [c for c in raw_codes if not c.startswith("688")]
    seen = set()
    codes = []
    for c in raw_codes:
        if c not in seen:
            seen.add(c)
            codes.append(c)

    name_map = {}
    try:
        anchors = page.query_selector_all('a[href*="code"], a[href*="quotes"]')
        for a in anchors[:100]:
            try:
                txt = (a.inner_text() or "").strip()
                href = a.get_attribute("href") or ""
                m = _re.search(r"(?:code|stock[_-]?code)=([0-9]{6})", href)
                if not m:
                    m = _re.search(r"/(sh|sz)?0*([0-9]{6})", href)
                if m:
                    code = m.group(2) if m.lastindex >= 2 else m.group(1)
                    if code and txt and len(txt) < 20:
                        name_map.setdefault(code, txt)
            except Exception:
                continue
    except Exception:
        pass

    rows = []
    for idx, code in enumerate(codes[:n], 1):
        rows.append({
            "code": code,
            "name": name_map.get(code, ""),
            "rank": idx,
            "source": f"同花顺热度榜({label})",
        })
    return rows


def fetch_ths_hot_playwright(n: int = 200) -> List[Dict[str, Any]]:
    """
    同花顺热榜 - Playwright 渲染版（替代 akshare 的 stock_hot_ths_*）

    拉取策略：按以下顺序尝试，找到第一个能用的榜单
      1. data.10jqka.com.cn/rank/cxfl/  - 持续放量（46 只，资金最活跃）
      2. data.10jqka.com.cn/rank/cxg/   - 持续高（50 只）
      3. data.10jqka.com.cn/rank/xstp/  - 新上榜（37 只）

    使用进程内 5 分钟缓存，无需持久化浏览器（避免首次启动慢）。

    Returns:
        List[{code, name, rank, source}]，抓不到则返回 [].
    """
    cache_key = "ths_pw"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[:n]

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        logger.error("[hot_lists] playwright 未安装: %s", e)
        return []

    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    rank_pages = [
        ("持续放量", "http://data.10jqka.com.cn/rank/cxfl/"),
        ("持续高", "http://data.10jqka.com.cn/rank/cxg/"),
        ("新上榜", "http://data.10jqka.com.cn/rank/xstp/"),
    ]

    rows: List[Dict[str, Any]] = []
    seen_codes = set()
    used_label = ""

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            )
            try:
                context = browser.new_context(
                    user_agent=user_agent,
                    viewport={"width": 1920, "height": 1080},
                    locale="zh-CN",
                )
                page = context.new_page()

                for label, url in rank_pages:
                    if len(rows) >= n:
                        break
                    page_rows = _scrape_ths_rank_page(page, url, label, n - len(rows))
                    for r in page_rows:
                        if r["code"] not in seen_codes:
                            seen_codes.add(r["code"])
                            rows.append(r)
                    if page_rows and not used_label:
                        used_label = label

                context.close()
            finally:
                browser.close()
    except Exception as e:
        logger.warning("[hot_lists] Playwright 抓同花顺整体失败: %s", e)

    if rows:
        logger.info(
            "[hot_lists] 同花顺热度榜 (Playwright): 主榜=%s, 共 %d 只 (含去重合并)",
            used_label, len(rows),
        )
    else:
        logger.warning("[hot_lists] 同花顺热度榜 (Playwright): 三个 rank 页都失败")

    _cache_set(cache_key, rows)
    return rows[:n]


def fetch_ths_hot(n: int = 200) -> List[Dict[str, Any]]:
    """
    "同花顺"热度榜（外部 label 保留）。

    当前实现：用 Playwright 渲染 data.10jqka.com.cn/rank/cxfl/ 等真实同花顺榜单。
    - 主榜（持续放量，~46 只）→ 降级到 持续高 / 新上榜
    - 历史 fallback：akshare stock_hot_ths_*d 系列（已下线，几乎不可用）

    历史问题：
        akshare 旧版提供的 stock_hot_ths_20d / 10d / 5d 系列接口
        （THS 官方热度榜）在新版 akshare 中已全部下线（AttributeError），
        导致 fetch_ths_hot 一直返回 []。

    当前实现：fetch_ths_hot_playwright（headless Chromium 渲染 data.10jqka.com.cn）。
    失败时回退到 ak.stock_hot_deal_xq（雪球 deals）。

    候选接口优先级：
        1. Playwright data.10jqka.com.cn/rank/cxfl/ (持续放量，最准)
        2. Playwright data.10jqka.com.cn/rank/cxg/  (持续高)
        3. Playwright data.10jqka.com.cn/rank/xstp/ (新上榜)
        4. ak.stock_hot_ths_*d (历史，已下线)
        5. ak.stock_hot_deal_xq (雪球 deals, 兜底)

    Returns:
        List[{code, name, rank, source}]，抓不到则返回 [].
    """
    cache_key = "ths"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached[:n]

    # 1. 优先 Playwright（真实 THS 数据）
    pw_rows = fetch_ths_hot_playwright(n)
    if pw_rows:
        return pw_rows

    logger.debug("[hot_lists] Playwright 抓同花顺失败，回退到 akshare 历史接口")

    try:
        import akshare as ak
    except Exception as e:
        logger.error("[hot_lists] 缺少 akshare 依赖: %s", e)
        return []

    # 候选接口（按优先级）
    candidates: List[Tuple[str, Any]] = [
        # 1. 历史 THS 接口（已下线，通常 AttributeError，作为 fallback 保留）
        ("stock_hot_ths_20d", lambda: ak.stock_hot_ths_20d()),
        ("stock_hot_ths_10d", lambda: ak.stock_hot_ths_10d()),
        ("stock_hot_ths_5d", lambda: ak.stock_hot_ths_5d()),
        ("stock_hot_ths_30d", lambda: ak.stock_hot_ths_30d()),
        # 2. 历史替代（通常也不存在）
        ("stock_hot_rank_ths",   lambda: getattr(ak, "stock_hot_rank_ths",   lambda: None)()),
        ("stock_hot_follow_ths", lambda: getattr(ak, "stock_hot_follow_ths", lambda: None)()),
        ("stock_hot_ths_index",  lambda: getattr(ak, "stock_hot_ths_index",  lambda: None)()),
        # 3. 实际工作的替代：雪球 deals（与关注榜正交）
        ("stock_hot_deal_xq", lambda: ak.stock_hot_deal_xq()),
    ]

    rows: List[Dict[str, Any]] = []
    used_source = ""
    for cand_name, cand_fn in candidates:
        df = _try_akshare_call(cand_fn, name=f"同花顺替代({cand_name})")
        if df is None or df.empty:
            continue

        for idx, (_, row) in enumerate(df.iterrows(), 1):
            # 列名适配：THS 系列用 "代码"/"股票代码"，雪球 deals 用 "股票代码"
            code = normalize_code(
                row.get("代码")
                or row.get("股票代码")
                or row.get("symbol")
            )
            if not code:
                continue
            name = (
                str(row.get("股票简称", "")).strip()
                or str(row.get("股票名称", "")).strip()
                or str(row.get("名称", "")).strip()
            )
            rows.append({
                "code": code,
                "name": name,
                "rank": idx,
                "source": f"同花顺热度榜(底层={cand_name})",
            })
        if rows:
            used_source = cand_name
            break

    if rows:
        logger.info(
            "[hot_lists] 同花顺热度榜: 使用=%s, 抓到=%d 只",
            used_source, len(rows),
        )
        if used_source != "stock_hot_deal_xq":
            # 如果未来 THS 原生接口复活，记 INFO 让运维知道
            logger.info(
                "[hot_lists] (注: 当前非兜底实现, 真实 THS 接口已可用: %s)",
                used_source,
            )
    else:
        logger.warning("[hot_lists] 同花顺热度榜: 全部候选接口失败或返回空")

    _cache_set(cache_key, rows)
    return rows[:n]


# ---------------------------------------------------------------------------
# 抓取全部三榜 + 交集计算
# ---------------------------------------------------------------------------
def fetch_all_four(n: int = 200) -> Dict[str, List[Dict[str, Any]]]:
    """
    抓取 4 个榜单（独立，失败不互相影响）。

    Returns:
        {
            "东方财富": [...],   # ak.stock_hot_rank_em (fallback: 全市场活跃)
            "雪球":     [...],   # ak.stock_hot_follow_xq
            "同花顺":   [...],   # Playwright data.10jqka.com.cn (fallback: 雪球 deals)
            "全市场活跃": [...],  # ak.stock_zh_a_spot 排序
        }

    阈值（endpoint 检查）：至少 2/4 成功即可分析
    """
    out: Dict[str, List[Dict[str, Any]]] = {}
    for label, fetcher in (
        ("东方财富", fetch_eastmoney_hot),
        ("雪球", fetch_xueqiu_hot),
        ("同花顺", fetch_ths_hot),
        ("全市场活跃", fetch_full_market_hot),
    ):
        try:
            out[label] = fetcher(n)
        except Exception as e:
            logger.exception("[hot_lists] 抓取 %s 异常: %s", label, e)
            out[label] = []
    return out


# 向后兼容：保留 fetch_all_three 作为别名（只取前 3 个）
def fetch_all_three(n: int = 200) -> Dict[str, List[Dict[str, Any]]]:
    """向后兼容别名：调用 fetch_all_four 并只返回前 3 个。"""
    all_four = fetch_all_four(n)
    return {k: all_four.get(k, []) for k in ("东方财富", "雪球", "同花顺")}


def compute_intersection(
    lists: Dict[str, List[Dict[str, Any]]],
    min_lists: int = 2,
) -> List[Dict[str, Any]]:
    """
    计算多个榜单的"至少 N 个榜出现"（不再是严格 AND）。

    设计变更：
        - 原版用 set.intersection(*sets) — 所有榜单都有才算热门
        - 现在改为"出现在至少 N 个榜单"（默认 N=2）— 更宽松，更贴合实战

    步骤：
    1. 标准化每只股票代码
    2. 累计每只股票出现过的 source 数
    3. 保留：出现在 count >= min_lists 个榜单的股票
    4. 排序：按出现次数 desc + code asc

    Returns:
        [
            {"code": "600519", "name": "贵州茅台",
             "in_sources": ["东方财富", "雪球", "同花顺"],
             "count": 3, "ranks": {...}},
            ...
        ]
    """
    if not lists:
        return []

    code_meta: Dict[str, Dict[str, Any]] = {}

    for source, items in lists.items():
        for item in items or []:
            code = normalize_code(item.get("code"))
            if not code:
                continue
            meta = code_meta.setdefault(
                code,
                {
                    "code": code,
                    "name": "",
                    "in_sources": [],
                    "ranks": {},
                    "count": 0,
                },
            )
            if not meta["name"]:
                meta["name"] = str(item.get("name") or "").strip()
            if source not in meta["in_sources"]:
                meta["in_sources"].append(source)
                meta["count"] = len(meta["in_sources"])
            rank = item.get("rank")
            if isinstance(rank, int):
                meta["ranks"][source] = rank

    result = []
    for code, meta in code_meta.items():
        if meta["count"] >= min_lists:
            result.append({
                "code": code,
                "name": meta["name"],
                "in_sources": list(meta["in_sources"]),
                "ranks": meta["ranks"],
                "count": meta["count"],
            })

    result.sort(key=lambda x: (-x["count"], x["code"]))
    return result


def filter_a_share(
    stocks: List[Dict[str, Any]],
    *,
    exclude_kechuang: bool = True,
) -> List[Dict[str, Any]]:
    """
    过滤 A 股：剔除科创板（688*）等。
    """
    out = []
    for s in stocks:
        code = s.get("code", "")
        if exclude_kechuang and code.startswith("688"):
            continue
        if not is_a_share_main(code):
            continue
        out.append(s)
    return out
