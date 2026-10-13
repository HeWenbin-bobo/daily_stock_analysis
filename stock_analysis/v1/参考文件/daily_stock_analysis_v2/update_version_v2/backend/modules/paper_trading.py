# -*- coding: utf-8 -*-
"""
模拟盘（虚拟交易）
- 虚拟账户（初始资金 ¥1,000,000）
- 接受手动下单 / 异动信号 / 回测信号
- T+1 结算（次日按收盘价结算）
- 每日快照（资产 + 持仓市值 + 现金 + 沪深 300 对比）
- 排行榜（按总收益率）
"""
import time
import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from db.migrate import get_conn
from core.data_source import fetch_quote_with_fallback, fetch_quotes_bulk

logger = logging.getLogger(__name__)

INITIAL_CAPITAL_DEFAULT = 1_000_000.0


# ============================================================================
# 账户管理
# ============================================================================
def create_account(user_id: str, name: str, initial_capital: float = INITIAL_CAPITAL_DEFAULT) -> int:
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO paper_account (user_id, name, initial_capital, cash, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, name, initial_capital, initial_capital, time.time()),
        )
        return cur.lastrowid or 0
    except Exception as e:
        if "UNIQUE" in str(e):
            cur.execute("SELECT id FROM paper_account WHERE user_id=? AND name=?", (user_id, name))
            return cur.fetchone()["id"]
        raise


def list_accounts(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    sql = "SELECT * FROM paper_account"
    params: list = []
    if user_id:
        sql += " WHERE user_id=?"
        params.append(user_id)
    sql += " ORDER BY created_at DESC"
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def get_account(account_id: int) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM paper_account WHERE id=?", (account_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def delete_account(account_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM paper_account WHERE id=?", (account_id,))
    return cur.rowcount > 0


# ============================================================================
# ★ 新功能 v2 audit round 1：账户间转账
# ============================================================================
def transfer_between_accounts(
    from_account_id: int, to_account_id: int, amount: float, note: str = ""
) -> Dict[str, Any]:
    """账户间转账（虚拟）"""
    if amount <= 0:
        raise ValueError("amount 必须 > 0")
    if from_account_id == to_account_id:
        raise ValueError("不能转给自己")
    src = get_account(from_account_id)
    dst = get_account(to_account_id)
    if not src or not dst:
        raise ValueError("账户不存在")
    if src["cash"] < amount:
        raise ValueError(f"现金不足：可用 {src['cash']:.0f}，要转 {amount:.0f}")
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE paper_account SET cash=cash-? WHERE id=?", (amount, from_account_id))
        conn.execute("UPDATE paper_account SET cash=cash+? WHERE id=?", (amount, to_account_id))
        # 记录两笔交易（转出 + 转入）
        now = time.time()
        trade_date = datetime.now().strftime("%Y-%m-%d")
        conn.execute(
            """INSERT INTO paper_trade (account_id, user_id, symbol, side, price, quantity, amount, fee, reason, trade_date, created_at)
               VALUES (?, ?, '_CASH_', 'sell', 1, 0, ?, 0, ?, ?, ?)""",
            (from_account_id, src["user_id"], amount, f"transfer_out:{note}"[:200], trade_date, now),
        )
        conn.execute(
            """INSERT INTO paper_trade (account_id, user_id, symbol, side, price, quantity, amount, fee, reason, trade_date, created_at)
               VALUES (?, ?, '_CASH_', 'buy', 1, 0, ?, 0, ?, ?, ?)""",
            (to_account_id, dst["user_id"], amount, f"transfer_in:{note}"[:200], trade_date, now),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    return {
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "amount": amount,
        "from_new_cash": src["cash"] - amount,
        "to_new_cash": dst["cash"] + amount,
        "note": note,
    }


# ============================================================================
# 持仓（实时计算，不存表）
# ============================================================================
def get_positions(account_id: int) -> List[Dict[str, Any]]:
    """计算当前持仓（按 last trade 状态推算）"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT symbol, side, quantity, price, trade_date
           FROM paper_trade WHERE account_id=?
           ORDER BY created_at ASC""",
        (account_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]

    pos: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sym = r["symbol"]
        if sym not in pos:
            pos[sym] = {"symbol": sym, "quantity": 0, "avg_cost": 0.0, "total_cost": 0.0}
        if r["side"] == "buy":
            pos[sym]["total_cost"] += r["quantity"] * r["price"]
            pos[sym]["quantity"] += r["quantity"]
        else:  # sell
            if pos[sym]["quantity"] > 0:
                avg = pos[sym]["total_cost"] / pos[sym]["quantity"]
                pos[sym]["total_cost"] -= r["quantity"] * avg
                pos[sym]["quantity"] -= r["quantity"]

    result = []
    # ★ 第三轮优化：批量并发拉取行情，避免 N+1
    symbols_with_pos = [sym for sym, p in pos.items() if p["quantity"] > 0]
    quotes_map = fetch_quotes_bulk(symbols_with_pos, parallel=True) if symbols_with_pos else {}
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
            "quantity": p["quantity"],
            "avg_cost": round(avg_cost, 2),
            "last_price": last_price,
            "market_value": round(market_value, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
        })
    return result


# ============================================================================
# 下单
# ============================================================================
def place_order(
    account_id: int,
    symbol: str,
    side: str,            # buy / sell
    quantity: int,
    price: Optional[float] = None,
    *,
    reason: str = "manual",
    related_alert_id: Optional[int] = None,
    related_strategy_id: Optional[int] = None,
) -> Dict[str, Any]:
    """模拟下单

    ★ Round 5：未指定 price 时，调用 fetch_quote 改为 best-effort（2s timeout via fallback 链）
    — 若 fetch_quote 返回 None，让用户重试或传 price 显式下单，而不是阻塞事件循环
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side 必须是 buy/sell，got {side}")
    if quantity <= 0:
        raise ValueError("quantity 必须 > 0")

    account = get_account(account_id)
    if not account:
        raise ValueError(f"账户 {account_id} 不存在")

    # 价格：未指定则拉实时（fail-soft：失败时让用户传 price 重试）
    if price is None:
        quote = fetch_quote_with_fallback(symbol)
        if not quote:
            raise ValueError(f"无法获取 {symbol} 行情，请稍后重试或显式传 price 参数")
        price = quote["price"]

    amount = quantity * price
    fee = max(amount * 0.0003, 5.0)  # 万三佣金，最低 5 元

    # 检查现金 / 持仓
    if side == "buy":
        if account["cash"] < amount + fee:
            raise ValueError(f"现金不足：可用 {account['cash']:.0f}，需要 {amount + fee:.0f}")
    else:  # sell
        positions = get_positions(account_id)
        held = next((p for p in positions if p["symbol"] == symbol), None)
        if not held or held["quantity"] < quantity:
            raise ValueError(f"持仓不足：{symbol} 持有 {held['quantity'] if held else 0}，要卖 {quantity}")

    # 写入交易
    conn = get_conn()
    cur = conn.cursor()
    trade_date = datetime.now().strftime("%Y-%m-%d")
    cur.execute(
        """INSERT INTO paper_trade
           (account_id, user_id, symbol, side, price, quantity, amount, fee,
            reason, related_alert_id, related_strategy_id, trade_date, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (account_id, account["user_id"], symbol, side, price, quantity, amount, fee,
         reason, related_alert_id, related_strategy_id, trade_date, time.time()),
    )
    trade_id = cur.lastrowid or 0

    # 更新账户现金
    if side == "buy":
        new_cash = account["cash"] - amount - fee
    else:
        new_cash = account["cash"] + amount - fee
    conn.execute("UPDATE paper_account SET cash=? WHERE id=?", (new_cash, account_id))

    return {
        "trade_id": trade_id,
        "symbol": symbol,
        "side": side,
        "price": price,
        "quantity": quantity,
        "amount": amount,
        "fee": fee,
        "new_cash": new_cash,
        "trade_date": trade_date,
    }


def list_trades(account_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT * FROM paper_trade WHERE account_id=?
           ORDER BY created_at DESC LIMIT ?""",
        (account_id, limit),
    )
    return [dict(r) for r in cur.fetchall()]


# ============================================================================
# 快照（每日结算）
# ============================================================================
def take_snapshot(account_id: int, snapshot_date: Optional[str] = None) -> Dict[str, Any]:
    """每日快照：记录总资产、持仓市值、现金、沪深 300"""
    if snapshot_date is None:
        snapshot_date = datetime.now().strftime("%Y-%m-%d")

    account = get_account(account_id)
    if not account:
        raise ValueError(f"账户 {account_id} 不存在")

    positions = get_positions(account_id)
    positions_value = sum(p["market_value"] for p in positions)
    total_equity = account["cash"] + positions_value

    # 沪深 300 基准（简化为百分比变化）
    benchmark = fetch_quote_with_fallback("000300") or {}
    benchmark_equity = INITIAL_CAPITAL_DEFAULT * (1 + benchmark.get("change_pct", 0) / 100)

    snapshot = {
        "account_id": account_id,
        "snapshot_date": snapshot_date,
        "total_equity": round(total_equity, 2),
        "cash": round(account["cash"], 2),
        "positions_value": round(positions_value, 2),
        "positions": positions,
        "benchmark_equity": round(benchmark_equity, 2),
    }

    # 写库
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO paper_snapshot
           (account_id, snapshot_date, total_equity, cash, positions_value,
            positions_json, benchmark_equity, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (account_id, snapshot_date, snapshot["total_equity"], snapshot["cash"],
         snapshot["positions_value"], json.dumps(positions, ensure_ascii=False),
         snapshot["benchmark_equity"], time.time()),
    )
    return snapshot


def list_snapshots(account_id: int, limit: int = 365) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT * FROM paper_snapshot WHERE account_id=?
           ORDER BY snapshot_date DESC LIMIT ?""",
        (account_id, limit),
    )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        try:
            d["positions"] = json.loads(d.pop("positions_json", "[]"))
        except Exception:
            d["positions"] = []
        rows.append(d)
    return rows


def get_snapshot(account_id: int, snapshot_date: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM paper_snapshot WHERE account_id=? AND snapshot_date=?",
        (account_id, snapshot_date),
    )
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["positions"] = json.loads(d.pop("positions_json", "[]"))
    except Exception:
        d["positions"] = []
    return d


# ============================================================================
# 日终结算（cron 调用）
# ============================================================================
def settle_daily() -> Dict[str, Any]:
    """所有账户每日快照"""
    accounts = list_accounts()
    today = datetime.now().strftime("%Y-%m-%d")
    summary = {"settled": 0, "errors": 0}
    for acc in accounts:
        try:
            take_snapshot(acc["id"], today)
            summary["settled"] += 1
        except Exception as e:
            logger.exception("Settle account %s failed: %s", acc["id"], e)
            summary["errors"] += 1
    return summary


# ============================================================================
# 排行榜（按总收益率）
# ============================================================================
def leaderboard(limit: int = 50) -> List[Dict[str, Any]]:
    """取最新快照，按 total_equity 降序"""
    conn = get_conn()
    cur = conn.cursor()
    # 取每个账户的最新快照
    cur.execute(
        """SELECT s.*, a.user_id, a.name as account_name, a.initial_capital
           FROM paper_snapshot s
           JOIN paper_account a ON s.account_id = a.id
           WHERE s.snapshot_date = (
             SELECT MAX(snapshot_date) FROM paper_snapshot s2 WHERE s2.account_id = s.account_id
           )
           ORDER BY s.total_equity DESC LIMIT ?""",
        (limit,),
    )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        ret_pct = (d["total_equity"] - d["initial_capital"]) / d["initial_capital"] * 100
        d["total_return_pct"] = round(ret_pct, 2)
        rows.append(d)
    return rows


# ============================================================================
# 账户总览
# ============================================================================
def get_account_overview(account_id: int) -> Dict[str, Any]:
    """返回账户 + 持仓 + 最新快照"""
    account = get_account(account_id)
    if not account:
        return {}
    positions = get_positions(account_id)
    positions_value = sum(p["market_value"] for p in positions)
    snapshots = list_snapshots(account_id, limit=2)
    last_snapshot = snapshots[0] if snapshots else None

    return {
        "account": account,
        "positions": positions,
        "current_equity": round(account["cash"] + positions_value, 2),
        "current_pnl": round(account["cash"] + positions_value - account["initial_capital"], 2),
        "current_pnl_pct": round((account["cash"] + positions_value - account["initial_capital"]) / account["initial_capital"] * 100, 2),
        "last_snapshot": last_snapshot,
    }