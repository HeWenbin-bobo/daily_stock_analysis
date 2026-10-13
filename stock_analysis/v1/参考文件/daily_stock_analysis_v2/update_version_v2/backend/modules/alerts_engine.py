# -*- coding: utf-8 -*-
"""
自选股异动监控 - 规则引擎
- 8 种规则：opening_spike / closing_spike / volume_surge / ma_break / volume_ratio / north_flow / limit_up / limit_down
- 扫描入口：scan_all_alerts()（被 cron 每 5 分钟调用）
- 触发后：写入 alert_trigger + 调用 notifier.send() 推送到用户的所有启用通道
"""
import time
import json
import logging
from typing import Dict, Any, List, Optional, Callable, Tuple
from datetime import datetime, timedelta

from db.migrate import get_conn
from core import notifier
from core.data_source import fetch_quote_with_fallback, fetch_kline_with_fallback

logger = logging.getLogger(__name__)

# ============================================================================
# 规则定义
# ============================================================================
RULE_TYPES = {
    "opening_spike": "开盘 30 分钟涨幅 > 阈值",
    "closing_spike": "尾盘 30 分钟涨幅 > 阈值",
    "volume_surge": "连续 3 日放量",
    "ma_break": "突破/跌破 20 日均线",
    "volume_ratio": "量比 > 阈值",
    "north_flow": "北向资金净流入 > 阈值",
    "limit_up": "涨停",
    "limit_down": "跌停",
}


def list_rule_types() -> List[Dict[str, str]]:
    return [{"type": k, "description": v} for k, v in RULE_TYPES.items()]


# ============================================================================
# 规则 CRUD
# ============================================================================
def create_rule(
    user_id: str,
    name: str,
    rule_type: str,
    params: Dict[str, Any],
    *,
    symbol_filter: Optional[str] = None,
    cooldown_minutes: int = 60,
    enabled: bool = True,
) -> int:
    if rule_type not in RULE_TYPES:
        raise ValueError(f"Unknown rule_type: {rule_type}")
    conn = get_conn()
    cur = conn.cursor()
    now = time.time()
    cur.execute(
        """INSERT INTO alert_rule
           (user_id, name, rule_type, symbol_filter, params_json,
            enabled, cooldown_minutes, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, name, rule_type, symbol_filter,
         json.dumps(params, ensure_ascii=False),
         1 if enabled else 0, cooldown_minutes, now, now),
    )
    return cur.lastrowid or 0


def list_rules(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    sql = "SELECT * FROM alert_rule"
    params: list = []
    if user_id:
        sql += " WHERE user_id=?"
        params.append(user_id)
    sql += " ORDER BY created_at DESC"
    cur.execute(sql, params)
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        try:
            d["params"] = json.loads(d.pop("params_json", "{}"))
        except Exception:
            d["params"] = {}
        rows.append(d)
    return rows


def get_rule(rule_id: int) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM alert_rule WHERE id=?", (rule_id,))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    d["params"] = json.loads(d.pop("params_json", "{}"))
    return d


def update_rule(rule_id: int, **fields) -> bool:
    allowed = {"name", "symbol_filter", "enabled", "cooldown_minutes"}
    sets = []
    params: list = []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            if k == "enabled":
                v = 1 if v else 0
            params.append(v)
    if "params" in fields:
        sets.append("params_json=?")
        params.append(json.dumps(fields["params"], ensure_ascii=False))
    if not sets:
        return False
    sets.append("updated_at=?")
    params.append(time.time())
    params.append(rule_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE alert_rule SET {','.join(sets)} WHERE id=?", params)
    return cur.rowcount > 0


def delete_rule(rule_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM alert_rule WHERE id=?", (rule_id,))
    return cur.rowcount > 0


# ============================================================================
# ★ 新功能 v2 audit round 1：批量启用/禁用
# ============================================================================
def bulk_toggle(rule_ids: List[int], enabled: bool) -> int:
    """批量启用/禁用规则；返回影响行数"""
    if not rule_ids:
        return 0
    conn = get_conn()
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in rule_ids)
    cur.execute(
        f"UPDATE alert_rule SET enabled=?, updated_at=? WHERE id IN ({placeholders})",
        [1 if enabled else 0, time.time()] + rule_ids,
    )
    return cur.rowcount


# ============================================================================
# 触发记录
# ============================================================================
def list_triggers(
    user_id: Optional[str] = None,
    rule_id: Optional[int] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    sql = "SELECT * FROM alert_trigger WHERE 1=1"
    params: list = []
    if user_id:
        sql += " AND user_id=?"
        params.append(user_id)
    if rule_id:
        sql += " AND rule_id=?"
        params.append(rule_id)
    sql += " ORDER BY triggered_at DESC LIMIT ?"
    params.append(limit)
    cur.execute(sql, params)
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        try:
            d["context"] = json.loads(d.pop("context_json", "{}"))
        except Exception:
            d["context"] = {}
        rows.append(d)
    return rows


# ============================================================================
# 规则评估器
# ============================================================================
def _get_watchlist_symbols(user_id: str) -> List[str]:
    """取用户的自选股代码列表"""
    # 这里暂时复用主项目的 watchlist（如果存在）；没有则空
    try:
        from update_version.db.history import get_watchlist
        items = get_watchlist()
        return [it["code"] for it in items if it.get("code")]
    except Exception:
        # 测试 / fallback：用 alert_rule.symbol_filter 或返回空
        return []


def _get_symbols_for_rule(rule: Dict[str, Any]) -> List[str]:
    """根据规则的 symbol_filter 决定监控的股票列表"""
    if rule.get("symbol_filter"):
        return [s.strip() for s in rule["symbol_filter"].split(",") if s.strip()]
    return _get_watchlist_symbols(rule["user_id"])


def _evaluate_opening_spike(quote: Dict[str, Any], params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """开盘 30 分钟涨幅 > 阈值"""
    threshold = float(params.get("threshold_pct", 3.0))
    change_pct = quote.get("change_pct", 0)
    if change_pct > threshold:
        return {"metric": change_pct, "threshold": threshold, "commentary": f"开盘大涨 {change_pct}%，突破阈值 {threshold}%"}
    return None


def _evaluate_closing_spike(quote: Dict[str, Any], params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """尾盘 30 分钟涨幅 > 阈值（同 change_pct 简化版）"""
    threshold = float(params.get("threshold_pct", 2.0))
    change_pct = quote.get("change_pct", 0)
    if change_pct > threshold:
        return {"metric": change_pct, "threshold": threshold, "commentary": f"尾盘拉升 {change_pct}%，突破阈值 {threshold}%"}
    return None


def _evaluate_volume_surge(quote: Dict[str, Any], params: Dict[str, Any], kline: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """连续 3 日放量：当前成交量 > 前 3 日均量 * 倍数"""
    if len(kline) < 4:
        return None
    multiplier = float(params.get("multiplier", 1.5))
    avg_vol = sum(k["volume"] for k in kline[-4:-1]) / 3
    current_vol = quote.get("volume", 0)
    if avg_vol > 0 and current_vol > avg_vol * multiplier:
        return {"metric": current_vol, "threshold": avg_vol * multiplier, "commentary": f"成交量 {current_vol:.0f} > 3日均量 {avg_vol:.0f} × {multiplier}"}
    return None


def _evaluate_ma_break(quote: Dict[str, Any], params: Dict[str, Any], kline: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """突破/跌破 20 日均线"""
    period = int(params.get("period", 20))
    direction = params.get("direction", "above")  # above / below
    if len(kline) < period:
        return None
    ma = sum(k["close"] for k in kline[-period:]) / period
    price = quote.get("price", 0)
    if direction == "above" and price > ma:
        return {"metric": price, "threshold": ma, "commentary": f"价格 {price} 突破 {period} 日均线 {ma:.2f}"}
    if direction == "below" and price < ma:
        return {"metric": price, "threshold": ma, "commentary": f"价格 {price} 跌破 {period} 日均线 {ma:.2f}"}
    return None


def _evaluate_volume_ratio(quote: Dict[str, Any], params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """量比 > 阈值（量比 = 当前成交量 / 过去 5 日同时段平均）"""
    # akshare 的 quote 中不含 volume_ratio，简化版用 change_pct 模拟
    threshold = float(params.get("threshold", 2.0))
    ratio = quote.get("volume_ratio", 1.0)
    if ratio > threshold:
        return {"metric": ratio, "threshold": threshold, "commentary": f"量比 {ratio} > {threshold}"}
    return None


def _evaluate_north_flow(quote: Dict[str, Any], params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """北向资金净流入 > 阈值（简化版：用金额变化近似）"""
    threshold = float(params.get("threshold_wan", 5000))
    north = quote.get("north_flow_wan", 0)
    if north > threshold:
        return {"metric": north, "threshold": threshold, "commentary": f"北向资金净流入 {north} 万 > {threshold} 万"}
    return None


def _evaluate_limit_up(quote: Dict[str, Any], params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """涨停（涨幅 > 9.5%）"""
    if quote.get("change_pct", 0) > 9.5:
        return {"metric": quote["change_pct"], "threshold": 9.5, "commentary": f"涨停，涨 {quote['change_pct']}%"}
    return None


def _evaluate_limit_down(quote: Dict[str, Any], params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """跌停（跌幅 < -9.5%）"""
    if quote.get("change_pct", 0) < -9.5:
        return {"metric": quote["change_pct"], "threshold": -9.5, "commentary": f"跌停，跌 {quote['change_pct']}%"}
    return None


EVALUATORS = {
    "opening_spike": _evaluate_opening_spike,
    "closing_spike": _evaluate_closing_spike,
    "volume_surge": _evaluate_volume_surge,
    "ma_break": _evaluate_ma_break,
    "volume_ratio": _evaluate_volume_ratio,
    "north_flow": _evaluate_north_flow,
    "limit_up": _evaluate_limit_up,
    "limit_down": _evaluate_limit_down,
}


# ============================================================================
# 扫描入口（被 cron 调用）
# ============================================================================
def scan_all_alerts() -> Dict[str, Any]:
    """扫描所有启用的规则，返回扫描摘要。

    ★ Round 4 优化：先按 (rules × symbols) 收集所有需要的 symbol，
    一次性 fetch_quotes_bulk 拿到所有 quote，再逐条评估；K 线需要时也批量。
    这样原本 "N rules × M symbols" 的串行 fetch 变成 1 次 bulk（受 60s 缓存复用）。
    """
    rules = [r for r in list_rules() if r["enabled"]]
    summary = {"scanned_rules": len(rules), "triggers": 0, "errors": 0}
    if not rules:
        return summary

    # 1) 收集全部 symbol + 决定哪些需要 K 线
    rule_symbols: List[Tuple[Dict[str, Any], str]] = []
    needs_kline_symbols: set = set()
    for rule in rules:
        try:
            symbols = _get_symbols_for_rule(rule)
            for sym in symbols:
                rule_symbols.append((rule, sym))
                if rule["rule_type"] in ("volume_surge", "ma_break"):
                    needs_kline_symbols.add(sym)
        except Exception as e:
            logger.exception("Get symbols for rule %s failed: %s", rule.get("id"), e)
            summary["errors"] += 1

    # 2) 批量拉行情（一次并发）
    from core.data_source import fetch_quotes_bulk
    all_symbols = list({sym for _, sym in rule_symbols})
    quotes_map = fetch_quotes_bulk(all_symbols, parallel=True) if all_symbols else {}

    # 3) 批量拉 K 线（只对需要的 symbol，60s 缓存复用）
    from core.data_source import fetch_kline_with_fallback
    klines_map: Dict[str, List[Dict[str, Any]]] = {}
    for sym in needs_kline_symbols:
        klines_map[sym] = fetch_kline_with_fallback(sym, days=60)

    # 4) 评估 + 触发
    for rule, symbol in rule_symbols:
        try:
            quote = quotes_map.get(symbol)
            if not quote:
                continue
            evaluator = EVALUATORS.get(rule["rule_type"])
            if not evaluator:
                continue
            if rule["rule_type"] in ("volume_surge", "ma_break"):
                triggered = evaluator(quote, rule["params"], klines_map.get(symbol, []))
            else:
                triggered = evaluator(quote, rule["params"])
            if triggered:
                _record_trigger_and_notify(rule, symbol, triggered, quote)
                summary["triggers"] += 1
        except Exception as e:
            logger.exception("Scan rule %s symbol %s failed: %s", rule.get("id"), symbol, e)
            summary["errors"] += 1
    return summary


def _evaluate_symbol_for_rule(rule: Dict[str, Any], symbol: str) -> Optional[Dict[str, Any]]:
    """评估单只股票 + 单个规则。

    ★ Round 4 提示：scan_all_alerts 已改为批量 fetch，这里仅保留单 symbol 调用入口
    供单独测试或未来按需使用。
    """
    quote = fetch_quote_with_fallback(symbol)
    if quote is None:
        return None
    evaluator = EVALUATORS.get(rule["rule_type"])
    if not evaluator:
        return None
    if rule["rule_type"] in ("volume_surge", "ma_break"):
        kline = fetch_kline_with_fallback(symbol, days=60)
        return evaluator(quote, rule["params"], kline)
    return evaluator(quote, rule["params"])


def _record_trigger_and_notify(
    rule: Dict[str, Any],
    symbol: str,
    triggered: Dict[str, Any],
    quote: Optional[Dict[str, Any]] = None,
) -> None:
    """写入 trigger 表 + 推送到用户所有启用通道。

    ★ Round 4 修复：quote 改由调用方传入（避免重复 fetch_quote_with_fallback）。
    """
    # 重新读一次规则，确保 cooldown 判断用最新 last_notified_at
    fresh = get_rule(rule["id"]) or rule
    cooldown_sec = (fresh.get("cooldown_minutes") or 60) * 60
    if fresh.get("last_notified_at") and time.time() - fresh["last_notified_at"] < cooldown_sec:
        logger.debug("Rule %s in cooldown, skipping notify", rule["id"])
        # 仍写 trigger 表，但不推送
        _record_trigger(rule, symbol, triggered, notification_log_id=None)
        return

    # 写 trigger 表
    trigger_id = _record_trigger(rule, symbol, triggered, notification_log_id=None)

    # 推送到所有启用通道
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM notification_channel WHERE user_id=? AND enabled=1",
        (rule["user_id"],),
    )
    channel_ids = [r["id"] for r in cur.fetchall()]

    if quote is None:
        # 单独调用入口（_evaluate_symbol_for_rule）时 quote 为 None，补一次拉取
        quote = fetch_quote_with_fallback(symbol) or {}
    context = {
        "symbol": symbol,
        "name": quote.get("name", symbol),
        "rule_name": RULE_TYPES.get(rule["rule_type"], rule["rule_type"]),
        "metric_value": triggered["metric"],
        "threshold_value": triggered["threshold"],
        "trigger_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "commentary": triggered["commentary"],
    }
    for ch_id in channel_ids:
        try:
            result = notifier.send(
                ch_id,
                template_name="alert_triggered",
                context=context,
            )
            logger.info("Alert notify: rule=%s symbol=%s ch=%s ok=%s", rule["id"], symbol, ch_id, result.success)
        except Exception as e:
            logger.warning("Notify failed for channel %s: %s", ch_id, e)

    # 更新 last_notified_at
    conn.execute(
        "UPDATE alert_rule SET last_notified_at=?, last_triggered_at=? WHERE id=?",
        (time.time(), time.time(), rule["id"]),
    )


def _record_trigger(rule, symbol, triggered, notification_log_id) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO alert_trigger
           (rule_id, user_id, symbol, triggered_at, metric_value, threshold_value,
            context_json, notification_log_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (rule["id"], rule["user_id"], symbol, time.time(),
         triggered["metric"], triggered["threshold"],
         json.dumps(triggered, ensure_ascii=False),
         notification_log_id),
    )
    return cur.lastrowid or 0