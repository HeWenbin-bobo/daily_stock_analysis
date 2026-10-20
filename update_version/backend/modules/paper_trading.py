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
from core.data_source import fetch_quote_with_fallback, fetch_quotes_bulk, is_mock_payload

logger = logging.getLogger(__name__)

INITIAL_CAPITAL_DEFAULT = 1_000_000.0

# 哨兵：区分"调用方没传基准行情（自行现取）"与"调用方传了 None（已取过但失败）"。
# 用 None 当"未传"会让 settle_daily 在所有源都失败时退回逐账户重取 ——
# 恰恰是最慢的场景。见 take_snapshot 的 docstring。
_UNSET_BENCHMARK: Any = object()

# 沪深300 基准的取数代码。
# ★ 探索期修复(基准线永远等于初始资金)：此前直接传 "000300"。baostock 把纯
#   数字当成**股票**代码，指数必须写成 `sh.000300`，于是 `000300` 每次都取不到
#   真实行情，回落到 mock → `is_mock_payload()` 判真 → 快照里基准按 0% 处理 →
#   `benchmark_equity` 恒等于 initial_capital，"相对基准超额收益"从未真正生效。
#   实测：`000300` 10.4s → mock=True（price 148.82 是伪造的）；
#         `sh.000300` 0.5s → mock=False（price 4152.1，真实点数）。
_BENCHMARK_CODE = "sh.000300"
# 初始资金上限：与 place_order / add_holding 的 1e9 护栏同量级。
# 旧实现只校验 > 0，`initial_capital=1e300` 会被接受并直接成为排行榜榜首
# （实测 leaderboard 返回 total_equity=1e300 / total_return_pct=0.0），
# 一次请求就能污染全站榜单，且 1e300 参与后续浮点运算会溢出成 inf/nan。
MAX_INITIAL_CAPITAL = 1e12


# ============================================================================
# 账户管理
# ============================================================================
def create_account(user_id: str, name: str, initial_capital: float = INITIAL_CAPITAL_DEFAULT) -> int:
    # ★ 探索期修复：name/user_id 传入 dict/list 时 sqlite3 直接抛
    #   ProgrammingError → 端点 500（实测 `{"name": {}}`）。
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name 必须是非空字符串")
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("user_id 必须是非空字符串")
    name = name.strip()
    # ★ 探索期修复：资金入口护栏放在共用函数里，所有调用方（端点 / 组合模块
    #   惰性建户 / 测试）都受保护，而不是只在某个端点补。
    try:
        initial_capital = float(initial_capital)
    except (TypeError, ValueError):
        raise ValueError("initial_capital 必须是数字")
    if initial_capital <= 0:
        raise ValueError("initial_capital 必须 > 0")
    if initial_capital > MAX_INITIAL_CAPITAL:
        raise ValueError(f"initial_capital 超出合理范围（上限 {MAX_INITIAL_CAPITAL:.0f}）")
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
    """删除账户，并级联清理其交易/快照/榜单依赖数据。

    ★ 探索期修复：旧实现只 `DELETE FROM paper_account`，而 schema 里
      **没有任何 FOREIGN KEY**（0 条），于是 paper_trade / paper_snapshot
      全部变成孤儿（实测：删账户后 trades 仍有 1 条）。
      孤儿数据的后果：
        - `leaderboard()` 走 JOIN paper_account，孤儿快照不会显示，但会一直占库；
        - 更隐蔽的是同一 user 重建同名账户后拿到**新的** account_id，
          旧交易不会复活，看似无害；但 `paper_trade` 按 user_id 聚合的地方
          （如 portfolio 统计）可能把已删除账户的历史算进来。
      改为同一事务内先删子表再删主表；顺序固定，避免忘记级联。
      注意：组合持仓账本（reason LIKE 'portfolio_holding%'）不在此处删除 ——
      它归 /portfolio/holdings 管理，走 remove_holding。
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("DELETE FROM paper_snapshot WHERE account_id=?", (account_id,))
        cur.execute(
            f"DELETE FROM paper_trade WHERE account_id=? AND {_PAPER_LEDGER_FILTER}",
            (account_id,),
        )
        cur.execute("DELETE FROM paper_account WHERE id=?", (account_id,))
        deleted = cur.rowcount > 0
        cur.execute("COMMIT")
        return deleted
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        raise


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
    # 快速预检（真正保证见事务内条件更新，避免 TOCTOU 透支）
    if src["cash"] < amount:
        raise ValueError(f"现金不足：可用 {src['cash']:.0f}，要转 {amount:.0f}")
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        # ★ Round 22 修复(TOCTOU)：余额检查必须在事务内用条件更新做，并检查 rowcount。
        #   旧实现只在本事务外读了一次 cash，两个并发转账都能通过预检 → cash 透支为负。
        cur = conn.execute(
            "UPDATE paper_account SET cash=cash-? WHERE id=? AND cash>=?",
            (amount, from_account_id, amount),
        )
        if cur.rowcount == 0:
            conn.execute("ROLLBACK")
            # 用最新值报错，更友好
            fresh = get_account(from_account_id)
            bal = fresh["cash"] if fresh else 0
            raise ValueError(f"现金不足：可用 {bal:.0f}，要转 {amount:.0f}")
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
    # ★ 修复(M4)：返回值用事务内最新余额 —— 旧实现用事务外 get_account 读到的
    #   src/dst cash 计算，并发转账下返回值与实际 DB 余额漂移。这里重新读。
    try:
        fresh_src = get_account(from_account_id)
        fresh_dst = get_account(to_account_id)
        from_new_cash = fresh_src["cash"] if fresh_src else 0
        to_new_cash = fresh_dst["cash"] if fresh_dst else 0
    except Exception:
        from_new_cash = src["cash"] - amount
        to_new_cash = dst["cash"] + amount
    return {
        "from_account_id": from_account_id,
        "to_account_id": to_account_id,
        "amount": amount,
        "from_new_cash": from_new_cash,
        "to_new_cash": to_new_cash,
        "note": note,
    }


# ============================================================================
# 持仓（实时计算，不存表）
# ============================================================================
# 组合持仓（modules/portfolio.py）与模拟盘共用 paper_trade 表，用 reason 前缀
# 区分两套账本。凡是模拟盘的持仓/可卖额度计算，都必须排除组合那部分。
#
# ★ 探索期修复(Critical)：旧实现完全不区分账本 ——
#   `POST /portfolio/holdings` 录入一笔"真实持仓"后，`GET/POST
#   /paper/accounts/{id}/orders` 会把它当成模拟盘持仓，可以按**调用方自选的
#   价格**卖出并增加该账户现金（实测 1000 股 @1000 → cash +9995），随后进入
#   `leaderboard`。等价于一台凭空造钱的排行榜刷分器。
_PORTFOLIO_REASON_PREFIX = "portfolio_holding"
_PAPER_LEDGER_FILTER = "COALESCE(reason, '') NOT LIKE 'portfolio_holding%'"


def is_portfolio_account(account_id: int) -> bool:
    """该账户是否属于"组合持仓"账本（不是模拟盘账户）。

    判定：账户名是组合模块建的 `我的持仓`，或名下存在 portfolio_holding 记录。
    两者都查，因为老数据可能只满足其中一条。
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT name FROM paper_account WHERE id=?", (account_id,)
    ).fetchone()
    if not row:
        return False
    if (row["name"] or "") == "我的持仓":
        return True
    hit = conn.execute(
        "SELECT 1 FROM paper_trade WHERE account_id=? AND COALESCE(reason,'') LIKE ? LIMIT 1",
        (account_id, _PORTFOLIO_REASON_PREFIX + "%"),
    ).fetchone()
    return hit is not None


def get_positions(account_id: int) -> List[Dict[str, Any]]:
    """计算当前持仓（按 last trade 状态推算）。

    只统计模拟盘自身的成交，排除组合持仓账本（见 _PAPER_LEDGER_FILTER）。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        f"""SELECT symbol, side, quantity, price, trade_date
           FROM paper_trade WHERE account_id=? AND {_PAPER_LEDGER_FILTER}
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
        # ★ 探索期修复：估值路径此前不判 mock —— sandbox/断网时 fetch_quotes_bulk
        #   返回 source="mock" 的合成价，会被写进 paper_snapshot（不可回滚），
        #   排行榜与业绩曲线从此被假价格污染（实测成交价 100 的持仓被估成 9999）。
        #   下单与基准早有 is_mock_payload 防护，这里补齐估值侧：mock 时回退成本价。
        price_is_mock = is_mock_payload(quote)
        last_price = avg_cost if price_is_mock else quote.get("price", avg_cost)
        market_value = p["quantity"] * last_price
        pnl = market_value - p["total_cost"]
        pnl_pct = (pnl / p["total_cost"]) * 100 if p["total_cost"] > 0 else 0
        result.append({
            "symbol": sym,
            "name": quote.get("name", sym),
            "quantity": p["quantity"],
            "avg_cost": round(avg_cost, 2),
            "last_price": last_price,
            "price_is_mock": price_is_mock,
            "market_value": round(market_value, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
        })
    return result


# ============================================================================
# 下单
# ============================================================================
def _shares_bought_before(account_id: int, symbol: str, before_date: str) -> int:
    """截至 before_date 之前（不含当日）已买入且尚未卖出的股数 —— T+1 可用卖出额度。

    A 股 T+1：当日买入不可当日卖出。这里累加 trade_date < before_date 的买入，
    减去同窗口的卖出，得到"今日可卖"上限。

    ★ 修复(T+1 绕过)：还必须减去**当日已卖出**的股数。旧实现只看 trade_date < today，
    导致"昨日买 200 + 今日买 100 → 今日先卖 200 成功、再卖 100 也成功"（可卖额度
    永远返回 200，今日卖出不扣减），当日买入被重复卖出。
    """
    conn = get_conn()
    cur = conn.cursor()
    # ★ 探索期修复：排除组合持仓账本，否则组合里的"真实持仓"会被算成可卖额度
    cur.execute(
        f"""SELECT side, quantity, trade_date FROM paper_trade
           WHERE account_id=? AND symbol=? AND {_PAPER_LEDGER_FILTER}
           ORDER BY created_at ASC""",
        (account_id, symbol),
    )
    held = 0  # 截至 today 之前的累计净买入（可卖基数）
    sold_today = 0  # 今日已卖出（占用 T+1 额度）
    for r in cur.fetchall():
        if r["trade_date"] < before_date:
            # 今日之前的交易：买入增加可卖基数，卖出减少
            if r["side"] == "buy":
                held += r["quantity"]
            else:
                held -= r["quantity"]
        else:
            # 今日的交易：买入不影响可卖（T+1 当日买入不可卖），卖出占用可卖额度
            if r["side"] == "sell":
                sold_today += r["quantity"]
    return max(held - sold_today, 0)


def _held_and_sellable(conn, account_id: int, symbol: str, today: str) -> "tuple[int, int]":
    """在给定连接/事务上重新计算当前净持仓与今日可卖额度（T+1）。

    ★ 本轮修复(TOCTOU)：place_order 卖出分支原先用 get_positions() /
    _shares_bought_before() 各自开一条新连接、在写事务之外读取，两个并发卖单
    可以读到同一份"卖出前"快照都通过校验，都成功 INSERT，导致净持仓被卖成负数
    （且 get_positions 会把 quantity<=0 的仓位从展示中过滤掉，超卖被悄悄吞掉、
    界面上看不出异常）。这里在 BEGIN IMMEDIATE 已经拿到写锁之后，用同一个 conn
    重新核验一次，作为唯一权威判断；外层调用处仍保留一次无锁预检以便快速失败。
    """
    cur = conn.execute(
        f"""SELECT side, quantity, trade_date FROM paper_trade
           WHERE account_id=? AND symbol=? AND {_PAPER_LEDGER_FILTER}
           ORDER BY created_at ASC""",
        (account_id, symbol),
    )
    total_held = 0
    held_before_today = 0
    sold_today = 0
    for r in cur.fetchall():
        qty = r["quantity"]
        if r["side"] == "buy":
            total_held += qty
            if r["trade_date"] < today:
                held_before_today += qty
        else:
            total_held -= qty
            if r["trade_date"] < today:
                held_before_today -= qty
            else:
                sold_today += qty
    return total_held, max(held_before_today - sold_today, 0)


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
    ★ Round 20：补上 T+1 校验 —— 当日买入的股票不可当日卖出（README 声称支持 T+1 但未实现）
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side 必须是 buy/sell，got {side}")
    if quantity <= 0:
        raise ValueError("quantity 必须 > 0")
    # ★ 探索期修复：与 portfolio.add_holding 对齐的上限护栏（防天文数字订单污染账本）
    if quantity > 10**9 or (price is not None and price > 10**9):
        raise ValueError("quantity/price 超出合理范围（上限 1e9）")
    # ★ 探索期修复(零/负价成交)：旧实现只校验了 quantity 与 price 上限，
    #   未校验下限 —— price=0 可零成本建仓、price=-100 会写入 amount=-1000
    #   并给账户记账，两种都能污染持仓成本与排行榜权益。显式传价必须 > 0；
    #   price=None 由下面的行情兜底赋值（那里同样保证 > 0）。
    if price is not None and price <= 0:
        raise ValueError(f"price 必须 > 0，got {price}")

    account = get_account(account_id)
    if not account:
        raise ValueError(f"账户 {account_id} 不存在")
    # ★ 探索期修复(Critical)：组合持仓账本不能被当作模拟盘下单。
    #   旧实现允许对"我的持仓"账户卖出，价格由调用方任意指定，卖出所得直接
    #   加到该账户 cash（实测 10 股 @1000 → cash 1,000,000 → 1,009,995），
    #   而该账户又出现在 /paper/leaderboard 里 —— 等于排行榜刷分器。
    if is_portfolio_account(account_id):
        raise ValueError(
            "该账户是组合持仓账本，不能通过模拟盘下单。"
            "请用 /portfolio/holdings 管理真实持仓。"
        )

    # 价格：未指定则拉实时（fail-soft：失败时让用户传 price 重试）
    # ★ 本轮修复(M-mock)：即便 quote 不为 None，也要排除 mock 数据 —— 避免 sandbox/离线时
    #   用按 code 字符串 hash 编造的假行情成交并写入账本，污染用户回测/分析决策。
    # 显式传 price 时不拉行情（price 由调用方负责），完全不走 mock 路径，不受影响。
    if price is None:
        quote = fetch_quote_with_fallback(symbol)
        if not quote or is_mock_payload(quote):
            # 注：not quote 已可拦住 fetch_quote 返回 None（如无效代码），
            # is_mock_payload(quote) 拦的是 sandbox 全源失败时返回的合成 dict。
            raise ValueError(
                f"无法获取 {symbol} 行情（数据源不可用或返回占位数据），"
                f"请稍后重试或显式传 price 参数"
            )
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
        # ★ Round 20 修复(T+1)：当日买入的股票不可当日卖出
        today = datetime.now().strftime("%Y-%m-%d")
        sellable = _shares_bought_before(account_id, symbol, today)
        if quantity > sellable:
            raise ValueError(
                f"T+1 限制：{symbol} 今日可卖 {sellable} 股（当日买入不可当日卖出），实际卖出 {quantity}"
            )

    # 写入交易（★ Round 13：包裹在 BEGIN/COMMIT 中，避免部分写入）
    # ★ 本轮修复(TOCTOU)：BEGIN → BEGIN IMMEDIATE，立即拿写锁；卖出分支在锁内
    #   用 _held_and_sellable() 重新核验一次持仓/T+1 额度 —— 上面的检查是无锁预检，
    #   不是权威判断，见 _held_and_sellable 的注释。
    conn = get_conn()
    cur = conn.cursor()
    trade_date = datetime.now().strftime("%Y-%m-%d")
    try:
        cur.execute("BEGIN IMMEDIATE")
        if side == "sell":
            total_held, sellable_now = _held_and_sellable(conn, account_id, symbol, trade_date)
            if total_held < quantity:
                conn.execute("ROLLBACK")
                raise ValueError(f"持仓不足：{symbol} 持有 {max(total_held, 0)}，要卖 {quantity}")
            if quantity > sellable_now:
                conn.execute("ROLLBACK")
                raise ValueError(
                    f"T+1 限制：{symbol} 今日可卖 {sellable_now} 股（当日买入不可当日卖出），实际卖出 {quantity}"
                )
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
            # ★ Round 22 修复(TOCTOU)：买入用条件更新 SET cash=cash-?，事务内检查 rowcount，
            #   旧实现用事务外读到的旧 cash 做绝对写，两个并发买单都能通过 → 透支
            cur2 = conn.execute(
                "UPDATE paper_account SET cash=cash-? WHERE id=? AND cash>=?",
                (amount + fee, account_id, amount + fee),
            )
            if cur2.rowcount == 0:
                conn.execute("ROLLBACK")
                fresh = get_account(account_id)
                bal = fresh["cash"] if fresh else 0
                raise ValueError(f"现金不足：可用 {bal:.0f}，需要 {amount + fee:.0f}")
            row = conn.execute("SELECT cash FROM paper_account WHERE id=?", (account_id,)).fetchone()
            new_cash = row["cash"] if row else 0
        else:
            # ★ 修复(L3)：卖出分支旧实现 new_cash = account["cash"] + amount - fee
            #   （account 是事务外读的旧值），并发成交下返回值与实际 DB 余额漂移。
            #   改为事务内重读最新余额（与买入分支一致）。
            conn.execute(
                "UPDATE paper_account SET cash=cash+? WHERE id=?",
                (amount - fee, account_id),
            )
            row = conn.execute("SELECT cash FROM paper_account WHERE id=?", (account_id,)).fetchone()
            new_cash = row["cash"] if row else 0
        cur.execute("COMMIT")
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        raise

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
def take_snapshot(
    account_id: int,
    snapshot_date: Optional[str] = None,
    *,
    benchmark_quote: Any = _UNSET_BENCHMARK,
) -> Dict[str, Any]:
    """每日快照：记录总资产、持仓市值、现金、沪深 300

    ★ 探索期修复(日终结算跑 205s)：``settle_daily`` 逐账户调用本函数，而每个
      账户都要重新拉一次沪深 300 基准行情（``fetch_quote_with_fallback`` 本身
      **不缓存**）。实测 17 个账户 → 205s，且账户越多越慢，最终会顶到
      max_instances=1 的 5 分钟扫描间隔之上。
      ``benchmark_quote`` 让批量调用方（settle_daily）只取一次并复用；
      不传时行为与之前完全一致（单账户手动快照仍需现取）。
    """
    if snapshot_date is None:
        snapshot_date = datetime.now().strftime("%Y-%m-%d")

    account = get_account(account_id)
    if not account:
        raise ValueError(f"账户 {account_id} 不存在")

    positions = get_positions(account_id)
    positions_value = sum(p["market_value"] for p in positions)
    total_equity = account["cash"] + positions_value

    # 沪深 300 基准（简化为百分比变化）
    # ★ 本轮修复(M-mock)：若基准本身是 mock 行情（sandbox/离线），
    #   不把假基准写入快照，因为基准会被业绩对比/排行榜直接消费。
    # 选择"标记并把基准视为 0%"而非"完全跳过快照"的理由：
    #   快照本身（总资产/持仓市值/现金）是真实数据，盲目跳过会让用户的每日持仓记录中断；
    #   只把不可信的基准部分置零并打 is_mock 标记，前端就能区分"基准有/无"。
    benchmark_raw = (
        fetch_quote_with_fallback(_BENCHMARK_CODE)
        if benchmark_quote is _UNSET_BENCHMARK
        else benchmark_quote
    )
    benchmark_is_mock = bool(benchmark_raw and is_mock_payload(benchmark_raw))
    if benchmark_is_mock:
        benchmark = {}
    else:
        benchmark = benchmark_raw or {}
    # ★ 探索期修复：基准权益必须基于**该账户的初始资金**，旧实现硬编码
    #   INITIAL_CAPITAL_DEFAULT(100 万)。实测初始资金 10,000 的账户，
    #   total_equity=10,000 而 benchmark_equity=1,005,000 —— 超额收益/业绩对比
    #   全错，且前端会显示一个量级错误的基准线。
    base_capital = account.get("initial_capital") or INITIAL_CAPITAL_DEFAULT
    benchmark_equity = base_capital * (1 + benchmark.get("change_pct", 0) / 100)

    snapshot = {
        "account_id": account_id,
        "snapshot_date": snapshot_date,
        "total_equity": round(total_equity, 2),
        "cash": round(account["cash"], 2),
        "positions_value": round(positions_value, 2),
        "positions": positions,
        "benchmark_equity": round(benchmark_equity, 2),
        # ★ 本轮修复(M-mock)：快照中追加基准数据来源标记，便于前端区分显示
        "benchmark_is_mock": benchmark_is_mock,
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
    """所有**模拟盘**账户每日快照（组合持仓账本不参与）"""
    accounts = list_accounts()
    today = datetime.now().strftime("%Y-%m-%d")
    summary = {"settled": 0, "errors": 0, "skipped_portfolio": 0}
    # ★ 探索期修复(日终结算跑 205s)：基准行情只取一次，所有账户复用。
    #   旧实现每个账户各拉一次沪深 300（fetch_quote_with_fallback 不缓存），
    #   17 个账户就是 17 次网络往返 —— 实测 205s，逼近 max_instances=1
    #   的间隔上限，账户再多会直接顶穿。
    #   取一次失败时传 None，take_snapshot 会各自回退现取（行为不变）。
    benchmark_quote = None
    try:
        benchmark_quote = fetch_quote_with_fallback(_BENCHMARK_CODE)
    except Exception as e:
        logger.warning("settle: benchmark fetch failed, per-account fallback: %s", e)
    for acc in accounts:
        # ★ 探索期修复：组合持仓账本不是模拟盘，不该产生回测/排行榜用的快照
        try:
            if is_portfolio_account(acc["id"]):
                summary["skipped_portfolio"] += 1
                continue
        except Exception as e:
            logger.warning("settle: portfolio check failed for %s: %s", acc["id"], e)
        try:
            take_snapshot(acc["id"], today, benchmark_quote=benchmark_quote)
            summary["settled"] += 1
        except Exception as e:
            logger.exception("Settle account %s failed: %s", acc["id"], e)
            summary["errors"] += 1
    return summary


# ============================================================================
# 排行榜（按总收益率）
# ============================================================================
def leaderboard(limit: int = 50) -> List[Dict[str, Any]]:
    """取最新快照，按总收益率降序（只含模拟盘账户）"""
    conn = get_conn()
    cur = conn.cursor()
    # 取每个账户的最新快照
    ## ★ 探索期修复：组合持仓账本（"我的持仓"）不是模拟盘，混进排行榜会
    #   让"录入真实持仓"的用户凭空占据榜首。用 name + portfolio_holding 记录
    #   双重排除，与 is_portfolio_account() 的判定保持一致。
    cur.execute(
        """SELECT s.*, a.user_id, a.name as account_name, a.initial_capital
           FROM paper_snapshot s
           JOIN paper_account a ON s.account_id = a.id
           WHERE s.snapshot_date = (
             SELECT MAX(snapshot_date) FROM paper_snapshot s2 WHERE s2.account_id = s.account_id
           )
           AND a.name != '我的持仓'
           AND NOT EXISTS (
             SELECT 1 FROM paper_trade t
             WHERE t.account_id = a.id
               AND COALESCE(t.reason, '') LIKE 'portfolio_holding%'
           )
           ORDER BY s.total_equity DESC LIMIT ?""",
        (limit,),
    )
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        # ★ 修复(除零)：initial_capital=0 时 leaderboard 500；兜底返回 0%
        initial = d.get("initial_capital") or 0
        ret_pct = (d["total_equity"] - initial) / initial * 100 if initial > 0 else 0.0
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
    # ★ 修复(除零)：initial_capital=0 时 current_pnl_pct 除零；兜底返回 0
    initial = account.get("initial_capital") or 0
    current_equity = round(account["cash"] + positions_value, 2)
    pnl_pct = (current_equity - initial) / initial * 100 if initial > 0 else 0.0

    return {
        "account": account,
        "positions": positions,
        "current_equity": current_equity,
        "current_pnl": round(current_equity - initial, 2),
        "current_pnl_pct": round(pnl_pct, 2),
        "last_snapshot": last_snapshot,
    }