# -*- coding: utf-8 -*-
"""
大盘复盘服务 —— update_version 轻量自建版本

目标：在不依赖根项目 notifier/analyzer 的前提下生成可持久化的复盘报告。
- 复用 akshare 抓实时数据（指数 + 全市场宽度 + 行业板块 + 概念板块）
- 不调用 LLM，纯结构化汇总（确定性规则，结果可重现）
- 返回字段全部可 JSON 化；前端可直接展示

数据源(2026-07-28 实测)：
  指数  : ak.stock_zh_index_spot_sina()        (562 行, 1.4s)
  全市场: ak.stock_zh_a_spot()                  (5532 行, 14.9s, 带缓存)
  行业  : ak.stock_sector_spot(indicator="行业")  (84 行, 0.24s)
  概念  : ak.stock_sector_spot(indicator="概念")  (175 行, 0.26s)
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# 国内主要关注指数（名称匹配 stock_zh_index_spot_sina）
_DEFAULT_INDICES: List[Tuple[str, str]] = [
    ("上证指数", "000001"),
    ("深证成指", "399001"),
    ("创业板指", "399006"),
    ("科创50", "000688"),
    ("上证50", "000016"),
    ("沪深300", "000300"),
]


# ============================== 工具函数 ==============================

def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """安全转 float；失败回退默认或 None。"""
    if value is None or value == "" or value == "-":
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _safe_pct(value: Any) -> Optional[float]:
    """把带有 % 或纯数字的涨跌幅统一成 float 百分点。"""
    if value is None or value == "" or value == "-":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().rstrip("%")
    try:
        return float(s)
    except ValueError:
        return None


def _find_col(df: Any, *candidates: str) -> Optional[str]:
    """在 DataFrame 列名中按候选关键字查找（中文优先 + 英文小写兜底）。"""
    if df is None or not hasattr(df, "columns"):
        return None
    cols = list(df.columns)
    for cand in candidates:
        for c in cols:
            if cand in str(c):
                return c
    return None


# ============================== 简单内存缓存 ==============================

class _TtlCache:
    """线程安全的 TTL 缓存（仅用于 akshare 抓取数据的新鲜度控制）。"""

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._data: Optional[Any] = None
        self._expires_at: float = 0.0

    def get(self) -> Optional[Any]:
        with self._lock:
            if self._data is None or time.time() > self._expires_at:
                return None
            return self._data

    def set(self, value: Any) -> None:
        with self._lock:
            self._data = value
            self._expires_at = time.time() + self._ttl


# akshare 抓取开销大（15s+），60s 内复用同一份全市场快照
_FULL_MARKET_CACHE = _TtlCache(ttl_seconds=60.0)


# ============================== 数据抓取 ==============================

def _fetch_indices() -> Tuple[Dict[str, Any], Optional[str]]:
    """
    拉取国内主要指数当日行情。

    Returns:
        (indices_dict, error_message)
        - indices_dict: {指数名: {code, name, price, change_pct, ...}}
        - error_message: 失败原因（None 表示成功）
        失败时 indices_dict 为空，但不抛错以免阻断整个复盘流程。
    """
    out: Dict[str, Any] = {}
    try:
        import akshare as ak
        # 1.18.63: stock_zh_index_spot_sina() 不再支持 symbol 参数
        df = ak.stock_zh_index_spot_sina()
    except Exception as exc:
        logger.warning("[market_review] 抓指数行情失败: %s", exc)
        return out, f"stock_zh_index_spot_sina: {exc}"

    if df is None or (hasattr(df, "empty") and df.empty):
        return out, "stock_zh_index_spot_sina returned empty DataFrame"

    col_code = _find_col(df, "代码", "code")
    col_name = _find_col(df, "名称", "name")
    col_price = _find_col(df, "最新价", "current", "price")
    col_change_pct = _find_col(df, "涨跌幅", "percent", "change_pct")
    col_open = _find_col(df, "今开", "open")
    col_high = _find_col(df, "最高", "high")
    col_low = _find_col(df, "最低", "low")
    col_volume = _find_col(df, "成交量", "volume")
    col_amount = _find_col(df, "成交额", "amount", "turnover")

    target_names = {name for name, _ in _DEFAULT_INDICES}
    for _, row in df.iterrows():
        try:
            name = str(row.get(col_name, "")) if col_name else ""
            if name not in target_names:
                continue
            out[name] = {
                "code": str(row.get(col_code, "")) if col_code else "",
                "name": name,
                "price": _safe_float(row.get(col_price)) if col_price else None,
                "change_pct": _safe_pct(row.get(col_change_pct)) if col_change_pct else None,
                "open": _safe_float(row.get(col_open)) if col_open else None,
                "high": _safe_float(row.get(col_high)) if col_high else None,
                "low": _safe_float(row.get(col_low)) if col_low else None,
                "volume": _safe_float(row.get(col_volume)) if col_volume else None,
                "turnover": _safe_float(row.get(col_amount)) if col_amount else None,
            }
        except Exception:
            continue

    if not out:
        return out, "no target indices found in sina spot result"
    return out, None


def _fetch_full_market_snapshot() -> Tuple[List[Dict[str, Any]], Optional[str], float]:
    """
    拉取全市场 A 股快照（用于宽度统计）。

    Returns:
        (stocks_list, error_message, duration) - 失败时 stocks_list 为空
    """
    cached = _FULL_MARKET_CACHE.get()
    if cached is not None:
        return cached["stocks"], None, cached["duration"]

    started = time.time()
    try:
        import akshare as ak
        # 1.18.63: stock_zh_a_spot_em() 远端断开; stock_zh_a_spot() 仍可用
        df = ak.stock_zh_a_spot()
    except Exception as exc:
        logger.warning("[market_review] 抓全市场行情失败: %s", exc)
        return [], f"stock_zh_a_spot: {exc}", time.time() - started

    if df is None or (hasattr(df, "empty") and df.empty):
        return [], "stock_zh_a_spot returned empty DataFrame", time.time() - started

    col_code = _find_col(df, "代码", "code")
    col_name = _find_col(df, "名称", "name")
    col_change_pct = _find_col(df, "涨跌幅", "percent", "change_pct")
    col_amount = _find_col(df, "成交额", "amount", "turnover")

    results: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        try:
            cp = _safe_pct(row.get(col_change_pct)) if col_change_pct else None
            results.append({
                "code": str(row.get(col_code, "")) if col_code else "",
                "name": str(row.get(col_name, "")) if col_name else "",
                "change_pct": cp,
                "turnover": _safe_float(row.get(col_amount)) if col_amount else None,
            })
        except Exception:
            continue

    duration = time.time() - started
    _FULL_MARKET_CACHE.set({"stocks": results, "duration": duration})
    return results, None, duration


def _fetch_sector_board(indicator: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """
    拉取行业/概念板块实时排行。

    Args:
        indicator: "行业" 或 "概念"

    Returns:
        (sectors_list, error_message)
        sectors_list: 每项 {name, change_pct, company_count, total_turnover}
    """
    try:
        import akshare as ak
        df = ak.stock_sector_spot(indicator=indicator)
    except Exception as exc:
        logger.warning("[market_review] 抓%s板块失败: %s", indicator, exc)
        return [], f"stock_sector_spot(indicator={indicator!r}): {exc}"

    if df is None or (hasattr(df, "empty") and df.empty):
        return [], f"stock_sector_spot({indicator!r}) returned empty DataFrame"

    col_name = _find_col(df, "板块", "name")
    col_change_pct = _find_col(df, "涨跌幅", "percent", "change_pct")
    col_count = _find_col(df, "公司家数", "count")
    col_amount = _find_col(df, "总成交额", "amount", "turnover")

    out: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        try:
            cp = _safe_pct(row.get(col_change_pct)) if col_change_pct else None
            if cp is None:
                continue
            out.append({
                "name": str(row.get(col_name, "")) if col_name else "",
                "change_pct": cp,
                "company_count": _safe_float(row.get(col_count)) if col_count else None,
                "total_turnover": _safe_float(row.get(col_amount)) if col_amount else None,
            })
        except Exception:
            continue
    return out, None


# ============================== 信号计算 ==============================

def _compute_breadth(stocks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从全市场快照统计宽度（上涨/下跌/平盘 + 涨停/跌停 + 总成交额）。"""
    up = down = flat = limit_up = limit_down = 0
    total_turnover = 0.0
    for s in stocks:
        cp = s.get("change_pct")
        if cp is None:
            flat += 1
            continue
        if cp > 0:
            up += 1
        elif cp < 0:
            down += 1
        else:
            flat += 1
        # 沪深主板 ±10% 涨停/跌停；创业板/科创板 ±20%
        if cp >= 9.95:
            limit_up += 1
        if cp <= -9.95:
            limit_down += 1
        turnover = s.get("turnover")
        if turnover:
            total_turnover += turnover
    total = up + down + flat
    return {
        "advance": up,
        "decline": down,
        "flat": flat,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "total": total,
        "advance_ratio": round(up / total * 100, 2) if total else 0.0,
        "total_turnover_yi": round(total_turnover / 1e8, 2),  # 亿元
        "limit_diff": limit_up - limit_down,
    }


def _rank_top_n(rows: List[Dict[str, Any]], n: int = 5) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把板块行情按涨跌幅排序，返回 top_gainers / top_losers 各 n 条。"""
    valid = [r for r in rows if r.get("change_pct") is not None]
    valid.sort(key=lambda x: x["change_pct"], reverse=True)
    return valid[:n], valid[-n:][::-1]


def _compute_sectors() -> Dict[str, Any]:
    """
    拉取行业 + 概念板块并按涨跌幅排序。

    Returns:
        {
            "industries": {"top_gainers": [...], "top_losers": [...], "count": N},
            "concepts":   {"top_gainers": [...], "top_losers": [...], "count": N},
        }
    """
    industries_rows, industries_err = _fetch_sector_board("行业")
    concepts_rows, concepts_err = _fetch_sector_board("概念")

    def _pack(rows: List[Dict[str, Any]], err: Optional[str]) -> Dict[str, Any]:
        if not rows:
            return {"top_gainers": [], "top_losers": [], "count": 0, "error": err or ""}
        top, bottom = _rank_top_n(rows, 5)
        return {
            "top_gainers": [
                {"name": r["name"], "change_pct": r["change_pct"],
                 "company_count": r.get("company_count")}
                for r in top
            ],
            "top_losers": [
                {"name": r["name"], "change_pct": r["change_pct"],
                 "company_count": r.get("company_count")}
                for r in bottom
            ],
            "count": len(rows),
            "error": err or "",
        }

    return {
        "industries": _pack(industries_rows, industries_err),
        "concepts": _pack(concepts_rows, concepts_err),
    }


def _compute_signals(breadth: Dict[str, Any], indices: Dict[str, Any]) -> Dict[str, Any]:
    """综合宽度 + 指数，给出盘面信号 + 操作建议（确定性规则，0 LLM）。"""
    score = 50.0
    adv = breadth.get("advance_ratio", 0) or 0
    if adv >= 70:
        score += 25
    elif adv >= 50:
        score += 10
    elif adv >= 30:
        score -= 5
    else:
        score -= 20

    limit_diff = breadth.get("limit_diff", 0) or 0
    if limit_diff >= 100:
        score += 15
    elif limit_diff >= 30:
        score += 5
    elif limit_diff <= -30:
        score -= 15

    # 主要指数加权
    for idx_name in ("上证指数", "深证成指", "创业板指"):
        if idx_name in indices and indices[idx_name].get("change_pct") is not None:
            cp = indices[idx_name]["change_pct"]
            score += cp * 3

    score = max(0, min(100, score))
    if score >= 70:
        tag, advice = "偏暖", "可进攻，关注主线"
    elif score >= 55:
        tag, advice = "温和", "持仓为主，注意分歧"
    elif score >= 40:
        tag, advice = "偏弱", "控制仓位，等待放量"
    else:
        tag, advice = "弱势", "防守为主，降低风险敞口"
    return {
        "score": round(score, 1),
        "tag": tag,
        "advice": advice,
        "reason": f"上涨占比 {adv}%, 涨跌停差 {limit_diff}",
    }


# ============================== Markdown 渲染 ==============================

def _fmt_pct(p: Optional[float]) -> str:
    if p is None:
        return "-"
    return f"{p:+.2f}%"


def _render_markdown(payload: Dict[str, Any]) -> str:
    """把复盘 payload 渲染为可读 markdown。"""
    lines: List[str] = []
    review_date = payload.get("review_date", "")
    lines.append(f"# 🎯 大盘复盘 {review_date}")
    lines.append("")
    signals = payload.get("signals", {})
    lines.append(f"**盘面信号**：{signals.get('score', 0)}/100（{signals.get('tag', '-')}）")
    lines.append(f"**操作建议**：{signals.get('advice', '-')}")
    lines.append("")

    breadth = payload.get("breadth", {})
    if breadth:
        lines.append("## 一、盘面总览")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("|------|------|")
        lines.append(f"| 上涨 / 下跌 / 平盘 | {breadth.get('advance', 0)} / {breadth.get('decline', 0)} / {breadth.get('flat', 0)} |")
        lines.append(f"| 上涨占比 | {breadth.get('advance_ratio', 0)}% |")
        lines.append(f"| 涨停 / 跌停 | {breadth.get('limit_up', 0)} / {breadth.get('limit_down', 0)} (差 {breadth.get('limit_diff', 0)}) |")
        lines.append(f"| 两市成交额 | {breadth.get('total_turnover_yi', 0)} 亿 |")
        lines.append("")

    indices = payload.get("indices", {})
    if indices:
        lines.append("## 二、指数结构")
        lines.append("")
        lines.append("| 指数 | 最新 | 涨跌幅 | 开盘 | 最高 | 最低 | 成交额(亿) |")
        lines.append("|------|------|--------|------|------|------|----------|")
        for name, _ in _DEFAULT_INDICES:
            if name not in indices:
                continue
            row = indices[name]
            price = row.get("price")
            price_str = f"{price:.2f}" if price is not None else "-"
            turnover = row.get("turnover")
            turnover_str = f"{turnover / 1e8:.0f}" if turnover else "-"
            lines.append(
                f"| {name} | {price_str} | {_fmt_pct(row.get('change_pct'))} | "
                f"{row.get('open', '-')} | {row.get('high', '-')} | {row.get('low', '-')} | {turnover_str} |"
            )
        lines.append("")

    sectors = payload.get("sectors", {})
    industries = sectors.get("industries", {}) if sectors else {}
    if industries.get("top_gainers") or industries.get("top_losers"):
        lines.append("## 三、行业板块")
        lines.append("")
        lines.append("### 领涨 Top 5")
        lines.append("| 行业 | 平均涨跌幅 | 成分股数 |")
        lines.append("|------|----------|---------|")
        for s in industries.get("top_gainers", []):
            tag = f"+{s['change_pct']}%" if s.get("change_pct", 0) >= 0 else f"{s['change_pct']}%"
            lines.append(f"| {s['name']} | {tag} | {s.get('company_count', '-')} |")
        lines.append("")
        lines.append("### 领跌 Top 5")
        lines.append("| 行业 | 平均涨跌幅 | 成分股数 |")
        lines.append("|------|----------|---------|")
        for s in industries.get("top_losers", []):
            lines.append(f"| {s['name']} | {s['change_pct']}% | {s.get('company_count', '-')} |")
        lines.append("")

    concepts = sectors.get("concepts", {}) if sectors else {}
    if concepts.get("top_gainers") or concepts.get("top_losers"):
        lines.append("## 四、概念板块")
        lines.append("")
        lines.append("### 领涨 Top 5")
        lines.append("| 概念 | 涨跌幅 |")
        lines.append("|------|--------|")
        for s in concepts.get("top_gainers", []):
            tag = f"+{s['change_pct']}%" if s.get("change_pct", 0) >= 0 else f"{s['change_pct']}%"
            lines.append(f"| {s['name']} | {tag} |")
        lines.append("")
        lines.append("### 领跌 Top 5")
        lines.append("| 概念 | 涨跌幅 |")
        lines.append("|------|--------|")
        for s in concepts.get("top_losers", []):
            lines.append(f"| {s['name']} | {s['change_pct']}% |")
        lines.append("")

    lines.append("## 五、操作要点")
    lines.append("")
    lines.append(f"- 信号依据：{signals.get('reason', '-')}")
    lines.append(f"- 建议：{signals.get('advice', '-')}")
    lines.append("")
    return "\n".join(lines)


# ============================== 顶层入口 ==============================

def _build_payload(review_date: str, region: str = "cn") -> Tuple[Dict[str, Any], List[str]]:
    """
    同步构建一份复盘 payload。

    Returns:
        (payload, warnings)
        - payload 包含 status / indices / breadth / sectors / signals / report_markdown / summary
        - warnings 列出每个失败的数据源（前端不阻塞但要可见）
    """
    started = time.time()
    warnings: List[str] = []

    indices, indices_err = _fetch_indices()
    if indices_err:
        warnings.append(f"indices: {indices_err}")

    stocks, stocks_err, stocks_duration = _fetch_full_market_snapshot()
    if stocks_err:
        warnings.append(f"market_snapshot: {stocks_err}")

    if stocks:
        breadth = _compute_breadth(stocks)
        sections = _compute_sectors()
    else:
        breadth = {
            "advance": 0, "decline": 0, "flat": 0,
            "limit_up": 0, "limit_down": 0,
            "total": 0, "advance_ratio": 0.0,
            "total_turnover_yi": 0.0, "limit_diff": 0,
        }
        sections = {
            "industries": {"top_gainers": [], "top_losers": [], "count": 0, "error": "skipped: no stocks"},
            "concepts": {"top_gainers": [], "top_losers": [], "count": 0, "error": "skipped: no stocks"},
        }

    # 板块级 error 透传
    if sections["industries"].get("error"):
        warnings.append(f"industries: {sections['industries']['error']}")
    if sections["concepts"].get("error"):
        warnings.append(f"concepts: {sections['concepts']['error']}")

    signals = _compute_signals(breadth, indices)

    # 状态判定：indices 与 stocks 任一成功就算 "ok"，否则 degraded
    has_real_data = bool(indices) or bool(stocks)
    status = "ok" if has_real_data else "degraded"

    payload: Dict[str, Any] = {
        "review_date": review_date,
        "region": region,
        "status": status,
        "indices": indices,
        "breadth": breadth,
        "sectors": sections,
        "signals": signals,
        "warnings": warnings,
        "stocks_fetch_seconds": round(stocks_duration, 3),
    }
    payload["report_markdown"] = _render_markdown(payload)
    payload["summary"] = (
        f"{signals.get('tag', '-')}｜{signals.get('score', 0)}/100｜"
        f"上涨 {breadth.get('advance', 0)} / 下跌 {breadth.get('decline', 0)}"
    )
    payload["duration_seconds"] = round(time.time() - started, 3)
    return payload, warnings


def generate_market_review(
    *,
    review_date: Optional[str] = None,
    region: str = "cn",
    persist: bool = True,
) -> Dict[str, Any]:
    """
    同步生成一份大盘复盘。

    Args:
        review_date: 日期字符串（YYYY-MM-DD）；默认今天
        region: 市场区域（目前仅支持 cn）
        persist: 是否持久化到 DB

    Returns:
        完整复盘 payload（含 status / warnings / report_markdown / summary / duration）
    """
    review_date = review_date or datetime.now().strftime("%Y-%m-%d")
    error = ""
    try:
        payload, warnings = _build_payload(review_date, region)
    except Exception as exc:
        logger.exception("[market_review] 生成复盘失败")
        error = str(exc)
        payload = {
            "review_date": review_date,
            "region": region,
            "status": "failed",
            "indices": {},
            "breadth": {},
            "sectors": {
                "industries": {"top_gainers": [], "top_losers": [], "count": 0, "error": ""},
                "concepts": {"top_gainers": [], "top_losers": [], "count": 0, "error": ""},
            },
            "signals": {},
            "warnings": [f"build_payload: {error}"],
            "report_markdown": f"# 大盘复盘生成失败\n\n{error}",
            "summary": "复盘失败",
            "duration_seconds": 0.0,
        }
        warnings = payload["warnings"]

    if persist:
        # ★ Round 10 修复：用 importlib 按路径加载 v2 自己的 db.history
        # （不能用 `from db.history` 因为 backend/db 包抢占，且 v2 没装成 update_version 包）
        # 路径：services 在 update_version_v2/services/，所以 db 在 ../db/history.py
        try:
            from pathlib import Path as _Path
            import importlib.util as _ilu
            import sys as _sys
            _svc_path = _Path(__file__).resolve()
            _v2_root = _svc_path.parent.parent  # update_version_v2/
            _db_path = _v2_root / "db" / "history.py"
            if not _db_path.exists():
                raise ImportError(f"db.history not found at {_db_path}")
            if "v2_root_db_history" not in _sys.modules:
                _spec = _ilu.spec_from_file_location("v2_root_db_history", str(_db_path))
                _mod = _ilu.module_from_spec(_spec)
                _sys.modules["v2_root_db_history"] = _mod
                _spec.loader.exec_module(_mod)
            save_market_review = _sys.modules["v2_root_db_history"].save_market_review
        except Exception as _ie:
            logger.warning("[market_review] 路径加载 v2 db.history 失败: %s", _ie)
            try:
                from update_version.db.history import save_market_review  # 旧项目兼容
            except ImportError:
                raise
        try:
            save_market_review(
                review_date=payload["review_date"],
                region=payload["region"],
                status=payload["status"],
                summary=payload["summary"],
                breadth=payload["breadth"],
                indices=payload["indices"],
                sectors=payload["sectors"],
                signals=payload["signals"],
                report_markdown=payload["report_markdown"],
                duration_seconds=payload["duration_seconds"],
                error=", ".join(warnings) if warnings else error,
            )
        except Exception as exc:
            logger.warning("[market_review] 持久化失败（仍返回 payload）: %s", exc)
            payload.setdefault("warnings", []).append(f"persist_failed: {exc}")

    return payload


def reset_cache() -> None:
    """清空内存缓存（测试用）。"""
    with _FULL_MARKET_CACHE._lock:  # type: ignore[attr-defined]
        _FULL_MARKET_CACHE._data = None
        _FULL_MARKET_CACHE._expires_at = 0.0


# ============================================================
# round 6 新功能：报告导出 + 对比 + 批量生成 + 换手率维度
# ============================================================

def export_review_as_markdown(review: Dict[str, Any]) -> str:
    """
    把一次大盘复盘记录导出为独立 markdown 报告（含元信息头部）。
    与 payload["report_markdown"] 区别：这里加 review_date / status / duration 等头部
    适合作为下载文件或邮件发送。
    """
    lines: List[str] = []
    lines.append(f"# 大盘复盘报告 - {review.get('review_date', '?')}")
    lines.append("")
    lines.append(f"> 生成时间：{review.get('created_at', '-')}")
    lines.append(f"> 状态：`{review.get('status', '-')}`")
    lines.append(f"> 耗时：{review.get('duration_seconds', 0)}s")
    lines.append(f"> 区域：`{review.get('region', 'cn')}`")
    lines.append("")
    if review.get("warnings"):
        lines.append("## ⚠️ 数据源警告")
        for w in review["warnings"]:
            lines.append(f"- {w}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(review.get("report_markdown", ""))
    return "\n".join(lines)


def _diff_dict(d1: Any, d2: Any, path: str = "") -> List[Dict[str, Any]]:
    """
    递归对比两个值，输出形如
      {"path": "breadth.advance_ratio", "from": 47.04, "to": 61.99, "delta": 14.95}
    的 diff 列表。dict / list 递归；标量直接比较。
    """
    diffs: List[Dict[str, Any]] = []
    if type(d1) != type(d2):
        diffs.append({"path": path, "from": d1, "to": d2, "delta": None, "type": "type_change"})
        return diffs
    if isinstance(d1, dict):
        keys = set(d1.keys()) | set(d2.keys())
        for k in sorted(keys):
            sub = _diff_dict(d1.get(k), d2.get(k), f"{path}.{k}" if path else k)
            diffs.extend(sub)
    elif isinstance(d1, list):
        if len(d1) != len(d2):
            diffs.append({
                "path": path, "from": f"len={len(d1)}",
                "to": f"len={len(d2)}", "delta": len(d2) - len(d1), "type": "list_len"
            })
        for i, (a, b) in enumerate(zip(d1, d2)):
            sub = _diff_dict(a, b, f"{path}[{i}]")
            diffs.extend(sub)
    else:
        if d1 != d2:
            if isinstance(d1, (int, float)) and isinstance(d2, (int, float)):
                diffs.append({
                    "path": path, "from": d1, "to": d2,
                    "delta": round(d2 - d1, 4), "type": "numeric",
                })
            else:
                diffs.append({"path": path, "from": d1, "to": d2, "delta": None, "type": "value"})
    return diffs


def compare_two_reviews(
    review_a: Dict[str, Any],
    review_b: Dict[str, Any],
) -> Dict[str, Any]:
    """
    对比两次大盘复盘，输出结构化 diff + 摘要。

    Args:
        review_a / review_b: DB 返回的 dict（含 review_date / breadth / indices / sectors / signals）

    Returns:
        {
          "date_a": "2026-07-28",
          "date_b": "2026-07-29",
          "diff": [...],          # 字段级 diff
          "summary": {
            "signal_score": {"from": 0, "to": 55.7, "delta": 55.7},
            "advance_ratio": {"from": 47.04, "to": 61.99, "delta": 14.95},
            "limit_diff": {"from": -52, "to": -2, "delta": 50},
            "tag_change": {"from": "弱势", "to": "温和"},
            ...
          }
        }
    """
    diff = _diff_dict(review_a, review_b)
    # 提取关键指标 diff
    a_breadth = review_a.get("breadth", {}) or {}
    b_breadth = review_b.get("breadth", {}) or {}
    a_signals = review_a.get("signals", {}) or {}
    b_signals = review_b.get("signals", {}) or {}
    summary: Dict[str, Dict[str, Any]] = {}
    for key in ("advance", "decline", "limit_up", "limit_down", "advance_ratio",
                "total_turnover_yi", "limit_diff"):
        av = a_breadth.get(key)
        bv = b_breadth.get(key)
        if av is None or bv is None:
            continue
        entry: Dict[str, Any] = {"from": av, "to": bv}
        if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
            entry["delta"] = round(bv - av, 4)
        summary[f"breadth.{key}"] = entry
    for key in ("score", "tag", "advice"):
        av = a_signals.get(key)
        bv = b_signals.get(key)
        if av is None or bv is None:
            continue
        entry = {"from": av, "to": bv}
        if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
            entry["delta"] = round(bv - av, 4)
        summary[f"signals.{key}"] = entry
    if "tag" in summary:
        summary["tag_change"] = {"from": a_signals.get("tag"), "to": b_signals.get("tag")}
    return {
        "date_a": review_a.get("review_date"),
        "date_b": review_b.get("review_date"),
        "diff": diff,
        "summary": summary,
    }


def batch_generate_recent_reviews(
    *,
    days: int = 5,
    region: str = "cn",
    persist: bool = True,
) -> List[Dict[str, Any]]:
    """
    批量生成最近 N 天（不含今天）的大盘复盘，跳过已有记录（按 review_date）。
    注意：akshare 的全市场数据是"当日实时"，无法拿历史快照，所以历史日期会
    拿到"今天的快照"——这是一个已知限制；这个批量主要适用于"补齐今天之前
    数据库为空" 的场景。

    Returns:
        list[dict]: 每个 dict 含 review_date + status + summary
    """
    from datetime import timedelta
    results: List[Dict[str, Any]] = []
    today = datetime.now().strftime("%Y-%m-%d")
    # 1) 跳过今天（避免重复），从昨天开始回溯
    base_date = datetime.now() - timedelta(days=1)
    # 2) 查 DB：哪些日期已有，跳过
    existing: set = set()
    try:
        from update_version.db.history import get_market_review_list
        items, _ = get_market_review_list(page=1, page_size=365, region=region)
        existing = {it["review_date"] for it in items}
    except Exception:
        pass
    for i in range(days):
        target = (base_date - timedelta(days=i)).strftime("%Y-%m-%d")
        if target in existing:
            results.append({
                "review_date": target, "status": "skipped",
                "summary": "已有记录，跳过",
            })
            continue
        try:
            payload = generate_market_review(
                review_date=target, region=region, persist=persist,
            )
            results.append({
                "review_date": target, "status": payload.get("status", "ok"),
                "summary": payload.get("summary", ""),
            })
        except Exception as exc:  # noqa: BLE001
            results.append({
                "review_date": target, "status": "failed",
                "summary": f"生成失败: {exc}",
            })
    return results
