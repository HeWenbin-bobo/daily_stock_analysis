# -*- coding: utf-8 -*-
"""
组合管理
- 录入持仓（成本、数量、买入日期）
- 计算每日盈亏（市值 vs 成本）
- 风险预警：单股占比 > 30%、行业集中度 > 60%、总亏损 > 20%
- 强制在每个分析报告底部加风险提示
"""
import time
import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from db.migrate import get_conn
from core.data_source import fetch_quote_with_fallback
from core.crypto import encrypt_str, decrypt_str
from core.security_headers import append_disclaimer

logger = logging.getLogger(__name__)


# ============================================================================
# 持仓管理（简化版：与 paper_trading 类似但属于"真实持仓"概念 + 加密成本）
# ============================================================================
def add_holding(
    user_id: str,
    symbol: str,
    cost: float,
    quantity: int,
    *,
    purchase_date: Optional[str] = None,
    note: str = "",
) -> int:
    """添加持仓（成本存入 price 列，reason 记 note；不搞假加密）

    ★ Round 21 修复(H5)：旧代码把 encrypted_cost 塞进 reason 的 meta 里，但 price 列
      仍存明文成本，list_holdings 也直接用 price —— "加密存储"从未被解密使用，是安全假象。
      改为诚实：成本以 price 列为准（与 paper_trade 其它交易一致），meta 只存 note。
    """
    if cost <= 0 or quantity <= 0:
        raise ValueError("cost 和 quantity 必须 > 0")
    # ★ 探索期修复：加上限校验——此前 quantity=10**18 也接受（返回 200），
    #   会产生无意义的天文数字持仓并污染组合统计（总市值溢出/展示崩溃）
    if cost > 10**9 or quantity > 10**9:
        raise ValueError("cost 和 quantity 超出合理范围（上限 1e9）")
    conn = get_conn()
    cur = conn.cursor()
    trade_date = purchase_date or datetime.now().strftime("%Y-%m-%d")
    # 找或建账户
    cur.execute("SELECT id FROM paper_account WHERE user_id=? AND name='我的持仓'", (user_id,))
    row = cur.fetchone()
    if not row:
        from modules.paper_trading import create_account
        acc_id = create_account(user_id, "我的持仓")
    else:
        acc_id = row["id"]

    amount = cost * quantity
    meta = json.dumps({"note": note}, ensure_ascii=False)
    cur.execute(
        """INSERT INTO paper_trade
           (account_id, user_id, symbol, side, price, quantity, amount, fee,
            reason, trade_date, created_at)
           VALUES (?, ?, ?, 'buy', ?, ?, ?, 0, ?, ?, ?)""",
        (acc_id, user_id, symbol, cost, quantity, amount,
         f"portfolio_holding|{meta}", trade_date, time.time()),
    )
    return cur.lastrowid or 0


def list_holdings(user_id: str) -> List[Dict[str, Any]]:
    """列出用户的所有持仓（按 symbol 聚合）。★ Round 4：批量并发拉行情。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT symbol, side, price, quantity, trade_date, reason
           FROM paper_trade WHERE user_id=? AND reason LIKE 'portfolio_holding%'
           ORDER BY created_at ASC""",
        (user_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]

    pos: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sym = r["symbol"]
        if sym not in pos:
            pos[sym] = {
                "symbol": sym,
                "quantity": 0,
                "total_cost": 0.0,
                "earliest_date": r["trade_date"],
                "note": "",
            }
        if r["side"] == "buy":
            pos[sym]["quantity"] += r["quantity"]
            pos[sym]["total_cost"] += r["quantity"] * r["price"]
            if r["trade_date"] < pos[sym]["earliest_date"]:
                pos[sym]["earliest_date"] = r["trade_date"]
            # 提取 note
            reason = r["reason"]
            if "|" in reason:
                try:
                    meta = json.loads(reason.split("|", 1)[1])
                    pos[sym]["note"] = meta.get("note", "")
                except Exception:
                    logger.warning("failed to parse portfolio_holding reason meta: %s", reason[:80])

    result = []
    # ★ Round 4 优化：批量并发拉取行情，避免 N+1（参考 paper_trading.get_positions）
    from core.data_source import fetch_quotes_bulk
    symbols = [sym for sym, p in pos.items() if p["quantity"] > 0]
    quotes_map = fetch_quotes_bulk(symbols, parallel=True) if symbols else {}
    for sym, p in pos.items():
        if p["quantity"] <= 0:
            continue
        avg_cost = p["total_cost"] / p["quantity"]
        quote = quotes_map.get(sym) or {}
        last_price = quote.get("price", avg_cost)
        market_value = p["quantity"] * last_price
        pnl = market_value - p["total_cost"]
        pnl_pct = (pnl / p["total_cost"]) * 100 if p["total_cost"] > 0 else 0
        result.append({
            "symbol": sym,
            "name": quote.get("name", sym),
            "industry": quote.get("industry", "未知"),
            "quantity": p["quantity"],
            "avg_cost": round(avg_cost, 2),
            "last_price": last_price,
            "market_value": round(market_value, 2),
            "total_cost": round(p["total_cost"], 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "earliest_purchase_date": p["earliest_date"],
            "note": p["note"],
            # ★ 修复(L4)：旧实现恒填 0（"在 overview 中填充"），调用 list_holdings 的
            #   消费方（如前端直接调用列表端点）看到 0 权重。这里先计算各持仓占比，
            #   get_overview 后续再基于 total_market_value 重新精确填充。
            "position_weight": 0,
        })
    # 计算总市值并填充权重（列表端点也应有真实权重）
    total_mv = sum(h["market_value"] for h in result)
    for h in result:
        h["position_weight"] = round((h["market_value"] / total_mv) * 100, 2) if total_mv > 0 else 0
    return result


def remove_holding(user_id: str, symbol: str) -> bool:
    """移除某 symbol 的所有买入记录"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """DELETE FROM paper_trade
           WHERE user_id=? AND symbol=? AND reason LIKE 'portfolio_holding%'""",
        (user_id, symbol),
    )
    return cur.rowcount > 0


# ============================================================================
# 总览 + 风险预警
# ============================================================================
def get_overview(user_id: str) -> Dict[str, Any]:
    """组合总览：总市值 / 盈亏 / 行业分布 / 风险预警"""
    holdings = list_holdings(user_id)
    total_market_value = sum(h["market_value"] for h in holdings)
    total_cost = sum(h["total_cost"] for h in holdings)
    total_pnl = total_market_value - total_cost
    total_pnl_pct = (total_pnl / total_cost) * 100 if total_cost > 0 else 0

    # 填充 position_weight
    for h in holdings:
        h["position_weight"] = round((h["market_value"] / total_market_value) * 100, 2) if total_market_value > 0 else 0

    # 行业分布
    industry_breakdown: Dict[str, float] = {}
    for h in holdings:
        ind = h["industry"]
        industry_breakdown[ind] = industry_breakdown.get(ind, 0) + h["market_value"]
    industry_pct = {
        ind: round((val / total_market_value) * 100, 2) if total_market_value > 0 else 0
        for ind, val in industry_breakdown.items()
    }

    # 风险预警
    warnings = []
    for h in holdings:
        if h["position_weight"] > 30:
            warnings.append({
                "level": "high",
                "type": "concentration",
                "symbol": h["symbol"],
                "message": f"⚠️ {h['symbol']} {h['name']} 占比 {h['position_weight']:.1f}%，超过 30% 警戒线",
            })
    for ind, pct in industry_pct.items():
        if pct > 60:
            warnings.append({
                "level": "high",
                "type": "industry_concentration",
                "industry": ind,
                "message": f"⚠️ {ind} 行业占比 {pct:.1f}%，超过 60% 警戒线",
            })
    if total_pnl_pct < -20:
        warnings.append({
            "level": "critical",
            "type": "total_loss",
            "message": f"🚨 总亏损 {total_pnl_pct:.1f}%，超过 -20% 警戒线，请审视持仓",
        })

    return {
        "user_id": user_id,
        "holdings": holdings,
        "total_market_value": round(total_market_value, 2),
        "total_cost": round(total_cost, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "industry_breakdown": industry_breakdown,
        "industry_pct": industry_pct,
        "warnings": warnings,
        "disclaimer": "本报告由 AI 生成，仅供研究参考，不构成投资建议。",
    }


# ============================================================================
# 分析报告（带强制风险提示）
# ============================================================================
def generate_report(user_id: str) -> Dict[str, Any]:
    """生成组合分析报告（自动追加风险提示）"""
    overview = get_overview(user_id)
    content_lines = [
        f"# 📊 组合分析报告",
        f"",
        f"**总市值**：¥{overview['total_market_value']:,.0f}",
        f"**总成本**：¥{overview['total_cost']:,.0f}",
        f"**总盈亏**：¥{overview['total_pnl']:,.0f}（{overview['total_pnl_pct']:+.2f}%）",
        f"",
        f"## 📈 持仓明细",
    ]
    for h in overview["holdings"]:
        content_lines.append(
            f"- **{h['symbol']} {h['name']}** ({h['industry']}) "
            f"{h['quantity']} 股 @ ¥{h['avg_cost']:.2f} → ¥{h['last_price']:.2f} "
            f"盈亏 {h['pnl_pct']:+.2f}%（占比 {h['position_weight']:.1f}%）"
        )
    content_lines.append("")
    content_lines.append("## 🏭 行业分布")
    for ind, pct in overview["industry_pct"].items():
        content_lines.append(f"- {ind}: {pct:.1f}%")
    content_lines.append("")
    if overview["warnings"]:
        content_lines.append("## ⚠️ 风险预警")
        for w in overview["warnings"]:
            content_lines.append(f"- [{w['level'].upper()}] {w['message']}")
    content_lines.append("")

    # 强制加风险提示
    content = append_disclaimer("\n".join(content_lines))
    return {
        "user_id": user_id,
        "overview": overview,
        "content": content,
        "generated_at": time.time(),
    }


# ============================================================================
# 加密验证（成本字段）
# ============================================================================
def test_encryption_roundtrip():
    """演示成本字段的加密存储"""
    cost = 1234.56
    enc = encrypt_str(str(cost))
    dec = decrypt_str(enc)
    assert float(dec) == cost
    return {"cost": cost, "encrypted": enc, "decrypted": float(dec)}


# ============================================================================
# ★ v2 升级：再平衡建议（按目标权重）
# ============================================================================
def suggest_rebalance(
    user_id: str,
    target_weights: Optional[Dict[str, float]] = None,
    max_position_pct: float = 30.0,
    max_industry_pct: float = 60.0,
) -> Dict[str, Any]:
    """生成再平衡建议

    target_weights: 期望的 symbol -> 权重(%)；缺省则把超 max_position_pct 的
                   持仓按 (max_position_pct, current) 等比下调
    max_position_pct: 单股权重警戒线
    max_industry_pct: 行业集中度警戒线

    返回：
    {
      "actions": [{"symbol": "600519", "action": "trim|add|hold", "current_pct": 35.0,
                   "target_pct": 30.0, "suggest_amount": 50000.0}],
      "current_total": 1000000.0,
      "industry_actions": [...]
    }
    """
    overview = get_overview(user_id)
    holdings = overview["holdings"]
    total_mv = overview["total_market_value"]
    if total_mv <= 0:
        return {"actions": [], "current_total": 0, "industry_actions": [], "note": "无持仓"}

    actions: List[Dict[str, Any]] = []
    target_pcts: Dict[str, float] = {}

    if target_weights:
        # 用户自定义目标
        # ★ 迭代4 修复(H4)：target_weights 校验/钳制，负值拒绝、>100 或超警戒线钳到 [0, max_position_pct]
        for sym, w in target_weights.items():
            wv = float(w)
            if wv < 0:
                raise ValueError(f"target_weights[{sym}] 不能为负")
            target_pcts[sym] = min(max_position_pct, wv) if wv > max_position_pct else wv
        # 其它持仓保持当前权重
        for h in holdings:
            if h["symbol"] not in target_pcts:
                target_pcts[h["symbol"]] = h["position_weight"]
    else:
        # 默认：把 > max_position_pct 的下调到 max
        for h in holdings:
            cur = h["position_weight"]
            if cur > max_position_pct:
                target_pcts[h["symbol"]] = max_position_pct
            else:
                target_pcts[h["symbol"]] = cur

    for h in holdings:
        cur = h["position_weight"]
        tgt = target_pcts.get(h["symbol"], cur)
        if abs(cur - tgt) < 0.5:  # 误差 < 0.5% 不动
            actions.append({
                "symbol": h["symbol"],
                "name": h["name"],
                "action": "hold",
                "current_pct": round(cur, 2),
                "target_pct": round(tgt, 2),
                "suggest_amount": 0.0,
            })
            continue
        action = "trim" if tgt < cur else "add"
        delta_pct = tgt - cur
        delta_amount = total_mv * delta_pct / 100.0
        actions.append({
            "symbol": h["symbol"],
            "name": h["name"],
            "action": action,
            "current_pct": round(cur, 2),
            "target_pct": round(tgt, 2),
            "suggest_amount": round(delta_amount, 2),
        })

    # 行业再平衡（仅在行业集中度超限时给出建议）
    industry_actions: List[Dict[str, Any]] = []
    for ind, pct in overview["industry_pct"].items():
        if pct > max_industry_pct:
            industry_actions.append({
                "industry": ind,
                "current_pct": round(pct, 2),
                "target_pct": max_industry_pct,
                "message": f"行业 {ind} 占比 {pct:.1f}% 超过 {max_industry_pct}%，建议分散到其他行业",
            })

    return {
        "current_total": round(total_mv, 2),
        "max_position_pct": max_position_pct,
        "max_industry_pct": max_industry_pct,
        "actions": actions,
        "industry_actions": industry_actions,
        "note": "再平衡建议仅供参考，不构成投资建议",
    }