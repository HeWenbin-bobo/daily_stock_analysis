# -*- coding: utf-8 -*-
"""
策略回测引擎
- 用户写自然语言策略 → LLM 编译成 DSL（JSON 描述的指标 + 阈值）
- 在历史 K 线上跑 → 输出：总收益/年化/夏普/最大回撤/胜率

注意：**不提供 walk-forward 分段验证**。`run_backtest(walk_forward_windows=N)`
在 N != 1 时直接报「尚未实现」而不是静默忽略——旧 docstring 曾写作
"支持 walk-forward 验证"，与实现矛盾，已更正。跨段稳定性目前只能通过
hyperopt 的 train/test 切分（前 80% / 后 20%）间接观察。
"""
import time
import json
import math
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

from db.migrate import get_conn
from core import llm_router as llm
from core.data_source import fetch_kline_with_fallback
from core.json_utils import _extract_json_object

logger = logging.getLogger(__name__)


# ============================================================================
# DSL 规范化 / 校验
# ============================================================================
_VALID_OPERATORS = (">", "<", ">=", "<=", "==", "cross_above", "cross_below")
_VALID_INDICATORS = ("close", "ma", "ema", "macd", "rsi", "volume_ratio")
_MAX_PERIOD = 250


def _as_float(value: Any, field: str, cond: Dict[str, Any]) -> float:
    """把 DSL 里的阈值转成 float；转不了抛 ValueError（端点层转 400）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"DSL 条件 {cond.get('indicator', '?')} 的 value 必须是数字，got {value!r}"
        )


def _as_period(value: Any, cond: Dict[str, Any]) -> int:
    try:
        period = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"DSL 条件 {cond.get('indicator', '?')} 的 period 必须是整数，got {value!r}"
        )
    if period < 1 or period > _MAX_PERIOD:
        raise ValueError(f"DSL 条件 period 必须在 1..{_MAX_PERIOD}，got {period}")
    return period


def normalize_dsl(dsl: Any) -> Dict[str, Any]:
    """把 LLM 编译出的 DSL 规整为可安全执行的形态。

    ★ 探索期修复：compiled_dsl 是 **LLM 直接产出并原样落库** 的 JSON，类型完全
      不可信。此前 `_execute_backtest` 里 `val > threshold` 遇到字符串阈值抛
      TypeError、`int(period)` 遇到 "abc" 抛 ValueError，两者都不是 ValueError
      的语义分支（TypeError 未被端点捕获）→ 回测端点 500。止盈止损方向写反
      （take_profit_pct=-5）则会让"止盈"永远先触发，产生看似合法的错误回测。

    规则：
      - value / stop_loss_pct / take_profit_pct 统一转 float
      - period 统一转 int 并限制在 1..250
      - operator / indicator 非法直接报错（避免静默永不触发）
      - stop_loss_pct 必须 <= 0，take_profit_pct 必须 > 0（方向写反时自动纠正，
        因为这是 LLM 高频笔误，拒绝会让用户反复重试）
      - hold_days < 0 视为 0（不限）
    """
    if not isinstance(dsl, dict):
        raise ValueError("DSL 必须是 JSON 对象")
    if "error" in dsl:
        return dsl

    out = dict(dsl)
    for key in ("entry", "exit"):
        conds = out.get(key) or []
        if not isinstance(conds, list):
            raise ValueError(f"DSL.{key} 必须是数组")
        normalized = []
        for cond in conds:
            if not isinstance(cond, dict):
                raise ValueError(f"DSL.{key} 的每个条件必须是对象")
            item = dict(cond)
            indicator = item.get("indicator", "")
            if indicator not in _VALID_INDICATORS:
                raise ValueError(
                    f"DSL.{key} 含不支持的 indicator: {indicator!r} "
                    f"（支持 {', '.join(_VALID_INDICATORS)}）"
                )
            operator = item.get("operator", ">")
            if operator not in _VALID_OPERATORS:
                raise ValueError(
                    f"DSL.{key} 含不支持的 operator: {operator!r} "
                    f"（支持 {', '.join(_VALID_OPERATORS)}）"
                )
            item["operator"] = operator
            if "value" in item:
                item["value"] = _as_float(item["value"], "value", item)
            if "period" in item and indicator in ("ma", "ema", "rsi", "volume_ratio"):
                item["period"] = _as_period(item["period"], item)
            normalized.append(item)
        out[key] = normalized

    if "stop_loss_pct" in out and out["stop_loss_pct"] is not None:
        sl = _as_float(out["stop_loss_pct"], "stop_loss_pct", {})
        out["stop_loss_pct"] = -abs(sl)
    if "take_profit_pct" in out and out["take_profit_pct"] is not None:
        tp = _as_float(out["take_profit_pct"], "take_profit_pct", {})
        out["take_profit_pct"] = abs(tp)
    if "hold_days" in out and out["hold_days"] is not None:
        try:
            out["hold_days"] = max(int(out["hold_days"]), 0)
        except (TypeError, ValueError):
            raise ValueError(f"DSL.hold_days 必须是整数，got {out['hold_days']!r}")
    return out


# ============================================================================
# DSL 示例 — LLM 编译的目标格式
# ============================================================================
DSL_EXAMPLES = """
支持的指标 (indicator):
- ma(period=20)            - N 日均线
- ema(period=12)           - 指数移动均线
- macd(fast=12, slow=26, signal=9) - MACD
- rsi(period=14)           - RSI
- volume_ratio(period=5)   - 量比
- close                    - 收盘价

支持的比较 (operator):
- ">", "<", ">=", "<=", "==", "cross_above", "cross_below"

条件 (condition):
{"indicator": "macd", "operator": "cross_above", "value": 0}

策略 (strategy) JSON:
{
  "entry": [
    {"indicator": "macd", "operator": "cross_above", "value": 0},
    {"indicator": "volume_ratio", "operator": ">", "value": 1.5}
  ],
  "exit": [
    {"indicator": "rsi", "operator": ">", "value": 70}
  ],
  "stop_loss_pct": -5.0,
  "take_profit_pct": 10.0,
  "hold_days": 5
}

示例自然语言 → DSL：
- "MACD 金叉且量比大于 1.5 时买入，RSI 高于 70 卖出，止损 5%，持有 5 天"
- "收盘价突破 20 日均线买入，跌破 10 日均线卖出"
"""


# ============================================================================
# 策略 CRUD
# ============================================================================
def create_strategy(
    user_id: str,
    name: str,
    natural_language: str,
    *,
    description: str = "",
) -> int:
    conn = get_conn()
    cur = conn.cursor()
    now = time.time()
    cur.execute(
        """INSERT INTO backtest_strategy
           (user_id, name, description, natural_language, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, name, description, natural_language, now, now),
    )
    return cur.lastrowid or 0


def list_strategies(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    sql = "SELECT * FROM backtest_strategy"
    params: list = []
    if user_id:
        sql += " WHERE user_id=?"
        params.append(user_id)
    sql += " ORDER BY created_at DESC"
    cur.execute(sql, params)
    rows = []
    for r in cur.fetchall():
        d = dict(r)
        for k in ("compiled_dsl", "compiled_code"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except Exception:
                    pass
        rows.append(d)
    return rows


def get_strategy(strategy_id: int) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM backtest_strategy WHERE id=?", (strategy_id,))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("compiled_dsl", "compiled_code"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                pass
    return d


def update_strategy(strategy_id: int, **fields) -> bool:
    allowed = {"name", "description", "natural_language", "compiled_dsl", "compiled_code", "llm_model"}
    sets = []
    params: list = []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            if k in ("compiled_dsl", "compiled_code") and isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            params.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    params.append(time.time())
    params.append(strategy_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE backtest_strategy SET {','.join(sets)} WHERE id=?", params)
    return cur.rowcount > 0


def delete_strategy(strategy_id: int) -> bool:
    """删除策略，并级联清理其回测结果与参数优化记录。

    ★ 探索期修复：schema 里**没有任何 FOREIGN KEY**（0 条），旧实现只删
      backtest_strategy 一行 —— 实测回测结果仍留在库里。
      孤儿后果：`list_results(user_id=...)` 仍会把已删策略的回测结果返回给前端
      （点进去引用一个不存在的 strategy_id），hyperopt_run / hyperopt_epoch
      同理永久占库。改为同一事务内按 策略 → 结果/优化 顺序清理。
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("DELETE FROM hyperopt_epoch WHERE run_id IN "
                    "(SELECT id FROM hyperopt_run WHERE strategy_id=?)", (strategy_id,))
        cur.execute("DELETE FROM hyperopt_run WHERE strategy_id=?", (strategy_id,))
        cur.execute("DELETE FROM backtest_result WHERE strategy_id=?", (strategy_id,))
        cur.execute("DELETE FROM backtest_strategy WHERE id=?", (strategy_id,))
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
# LLM 编译自然语言 → DSL
# ============================================================================
def compile_strategy(
    strategy_id: int,
    *,
    task_complexity: str = "medium",
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """调 LLM 把自然语言翻译成 DSL"""
    strategy = get_strategy(strategy_id)
    if not strategy:
        raise ValueError(f"strategy {strategy_id} 不存在")

    system_prompt = f"""你是一个量化策略编译器。把用户的中文自然语言策略编译成 JSON DSL。

{DSL_EXAMPLES}

严格输出 JSON，不要解释，不要 markdown 代码块。如果策略不合理，返回 {{"error": "原因"}}。"""

    prompt = f"策略：{strategy['natural_language']}\n\n请输出 JSON DSL："

    result = llm.chat(
        prompt,
        system=system_prompt,
        task_complexity=task_complexity,
        user_id=user_id or strategy.get("user_id"),
        task_type="backtest_translate",
    )

    content = result["content"].strip()

    # 解析 DSL
    # ★ 探索期修复(合法编译结果被丢弃)：旧实现先剥 markdown 围栏，再把**整段
    #   content** 丢给 json.loads。LLM 只要多说一句（"好的，编译结果如下："
    #   或 JSON 之后的解释），解析就失败，用户看到「LLM 返回的不是有效 JSON」，
    #   而 DSL 其实就完整躺在响应里。改用与 deep_research 同一个括号配平抽取器。
    dsl = _extract_json_object(content)
    if dsl is None:
        dsl = {"error": "LLM 返回的不是有效 JSON", "raw": content[:500]}

    # ★ 探索期修复：LLM 输出类型不可信（value 可能是 "1.5" 字符串、period 可能是
    #   "20"、止盈止损可能写反）。入库前规范化，坏 DSL 直接标 error，避免落库后
    #   回测 500。规范化失败也视为编译失败，让用户重新描述策略。
    if "error" not in dsl:
        try:
            dsl = normalize_dsl(dsl)
        except ValueError as exc:
            dsl = {"error": f"策略表达式不合法：{exc}", "raw": content[:500]}

    if "error" not in dsl:
        update_strategy(
            strategy_id,
            compiled_dsl=dsl,
            llm_model=f"{result['provider']}:{result['model']}",
        )
    return {
        "strategy_id": strategy_id,
        "compiled_dsl": dsl,
        "provider": result["provider"],
        "model": result["model"],
        "tokens": result["tokens"],
        "cost_cny": result["cost_cny"],
        "mocked": result["mocked"],
    }


# ============================================================================
# 回测引擎（在 K 线上跑）
# ============================================================================
def run_backtest(
    user_id: str,
    strategy_id: int,
    *,
    symbol: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    initial_capital: float = 1000000.0,
    walk_forward_windows: int = 1,
) -> Dict[str, Any]:
    """
    执行回测
    - start_date / end_date: YYYY-MM-DD
    - walk_forward_windows: 切几段（1 = 整段）
    """
    strategy = get_strategy(strategy_id)
    if not strategy:
        raise ValueError(f"strategy {strategy_id} 不存在")
    if not strategy.get("compiled_dsl"):
        raise ValueError("策略未编译，请先调用 compile_strategy()")
    # ★ 迭代1 修复：walk_forward_windows 参数此前声明但静默忽略（传 2/3 会误导用户以为做了切段验证）
    if walk_forward_windows != 1:
        raise ValueError("walk-forward 分段验证尚未实现（walk_forward_windows 仅支持 1=整段）")

    dsl = strategy["compiled_dsl"]
    if "error" in dsl:
        raise ValueError(f"DSL 有误: {dsl['error']}")
    # ★ 探索期修复：历史策略里可能存着旧版（未规范化）的 DSL，运行前再规范化一次，
    #   保证类型错误在端点层变成 400 而不是 500。
    dsl = normalize_dsl(dsl)
    if "error" in dsl:
        raise ValueError(f"DSL 有误: {dsl['error']}")

    # 拉 K 线
    kline = fetch_kline_with_fallback(symbol, days=365 * 2)
    if not kline:
        raise ValueError(f"无法获取 {symbol} K 线")

    # 日期过滤
    # ★ 探索期修复：先截断再算指标会丢掉指标预热窗口 —— 同一策略全区间 46 笔交易，
    #   指定 start_date 后变成 0 笔（MA60 的前 60 根全是 None）。正确做法是先保留
    #   足够长的前缀用于指标预热，只把**交易区间**限制在 [start_date, end_date]。
    _WARMUP_BARS = 120  # ≥ 最长指标周期(60) 的两倍
    if start_date:
        cutoff = next((i for i, k in enumerate(kline) if k["date"] >= start_date), len(kline))
        kline = kline[max(0, cutoff - _WARMUP_BARS):]
        warmup = min(cutoff, _WARMUP_BARS)
    else:
        warmup = 0
    if end_date:
        kline = [k for k in kline if k["date"] <= end_date]

    if len(kline) < 30:
        raise ValueError(f"K 线数据不足（仅 {len(kline)} 天）")

    # 执行回测
    result = _execute_backtest(kline, dsl, initial_capital, warmup_bars=warmup)

    # 写结果
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO backtest_result
           (strategy_id, user_id, symbol, start_date, end_date,
            initial_capital, final_capital, total_return_pct,
            annualized_return_pct, sharpe_ratio, max_drawdown_pct,
            win_rate, total_trades, trades_json, equity_curve_json,
            status, created_at, completed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (strategy_id, user_id, symbol, kline[0]["date"], kline[-1]["date"],
         initial_capital, result["final_capital"], result["total_return_pct"],
         result["annualized_return_pct"], result["sharpe_ratio"], result["max_drawdown_pct"],
         result["win_rate"], result["total_trades"],
         json.dumps(result["trades"], ensure_ascii=False),
         json.dumps(result["equity_curve"], ensure_ascii=False),
         "success", time.time(), time.time()),
    )
    result_id = cur.lastrowid or 0
    result["result_id"] = result_id

    return result


def _execute_backtest(
    kline: List[Dict[str, Any]],
    dsl: Dict[str, Any],
    initial_capital: float,
    *,
    warmup_bars: int = 0,
) -> Dict[str, Any]:
    """在 K 线序列上跑策略。

    warmup_bars: 前 N 根只用于指标预热，不产生交易（用于 start_date 场景）。
    """
    closes = [k["close"] for k in kline]
    volumes = [k["volume"] for k in kline]

    # 预计算所有指标
    indicators = {}
    for period in set([20, 10, 30, 60, 5, 12, 26, 14]):
        if len(closes) >= period:
            indicators[f"ma_{period}"] = _ma(closes, period)
    if len(closes) >= 26:
        macd_line, signal_line, hist = _macd(closes)
        indicators["macd_line"] = macd_line
        indicators["macd_signal"] = signal_line
        indicators["macd_hist"] = hist
    if len(closes) >= 15:
        indicators["rsi_14"] = _rsi(closes, 14)
    if len(volumes) >= 5:
        indicators["volume_ratio"] = _volume_ratio(volumes, 5)

    # ★ Round 19 修复(H3)：按 DSL 条件里用到的 indicator+period 按需补算指标，
    #   否则 ema 无条件匹配(→永不交易)、rsi(period!=14)/volume_ratio(period!=5) 取空。
    for cond in (dsl.get("entry") or []) + (dsl.get("exit") or []):
        ind = (cond or {}).get("indicator", "")
        period = int((cond or {}).get("period", 0) or 0)
        if not ind or period <= 0:
            continue
        # ★ 探索期修复(策略静默不交易)：上面预计算的 MA 只覆盖固定 8 个周期
        #   （[5,10,12,14,20,26,30,60]），而 DSL 的 period 允许 1..250。
        #   按需补算分支此前只写了 ema/rsi/volume_ratio，**唯独漏了 ma** ——
        #   于是 `ma(period=90)` / `ma(period=120)` 这类合法策略取不到指标，
        #   `_eval_condition` 恒返回 False，回测"成功"但 0 笔交易，
        #   hyperopt 也把它当作合法参数组合。实测：ma20/30/60 各 96-108 笔，
        #   ma90/120 全部 0 笔。
        if ind == "ma" and f"ma_{period}" not in indicators and len(closes) >= period:
            indicators[f"ma_{period}"] = _ma(closes, period)
        elif ind == "ema" and f"ema_{period}" not in indicators and len(closes) >= period:
            indicators[f"ema_{period}"] = _ema(closes, period)
        elif ind == "rsi" and f"rsi_{period}" not in indicators and len(closes) >= period + 1:
            indicators[f"rsi_{period}"] = _rsi(closes, period)
        elif ind == "volume_ratio" and f"volume_ratio_{period}" not in indicators and len(volumes) >= period:
            indicators[f"volume_ratio_{period}"] = _volume_ratio(volumes, period)

    # 回测主循环
    cash = initial_capital
    position = 0  # 持仓股数
    entry_price = 0.0
    entry_date = ""
    entry_idx = -1
    trades = []
    equity_curve = []

    entry_conditions = dsl.get("entry", [])
    exit_conditions = dsl.get("exit", [])
    stop_loss = dsl.get("stop_loss_pct", -5.0)
    take_profit = dsl.get("take_profit_pct", 10.0)
    hold_days = dsl.get("hold_days", 5)

    for i, k in enumerate(kline):
        date = k["date"]
        close = k["close"]

        # ★ 探索期修复(回测 500)：停牌/脏数据的 close 可能是 0 ——
        #   `core.data_source._f()` 解析失败（akshare 的 "-"、None、NaN）时
        #   返回默认值 0.0，而 K 线拉取不逐根过滤。此前买入分支的
        #   `int(cash / close / 100)` 直接抛 ZeroDivisionError → 整个回测 500，
        #   且 hyperopt 调它时会把整轮参数优化打挂。
        #   这一根没有有效价格，跳过它（不产生交易、也不记入权益曲线）。
        if not close or close <= 0 or not math.isfinite(close):
            continue

        # ★ 预热段：只喂指标，不交易、不记录 equity（否则会把预热期的假平线算进收益/回撤）
        if i < warmup_bars:
            continue

        # 评估 equity
        total_equity = cash + position * close
        equity_curve.append({"date": date, "equity": round(total_equity, 2)})

        if position > 0:
            # 持仓中：检查卖出
            # ★ 修复(H3)：entry_price 理论上 >0（买入价），但防御性防护避免除零 500
            pnl_pct = (close - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
            should_exit = False
            exit_reason = ""

            # 止盈止损
            if pnl_pct <= stop_loss:
                should_exit = True
                exit_reason = f"止损 {pnl_pct:.1f}%"
            elif pnl_pct >= take_profit:
                should_exit = True
                exit_reason = f"止盈 {pnl_pct:.1f}%"
            elif hold_days > 0 and (i - entry_idx) >= hold_days:
                should_exit = True
                exit_reason = f"持有 {hold_days} 天到期"

            # 卖出条件
            if not should_exit:
                for cond in exit_conditions:
                    if _eval_condition(cond, i, closes, indicators):
                        should_exit = True
                        exit_reason = f"满足卖出条件: {cond}"
                        break

            if should_exit:
                revenue = position * close
                cash += revenue
                trades.append({
                    "type": "sell",
                    "date": date,
                    "price": close,
                    "quantity": position,
                    "amount": revenue,
                    "pnl_pct": round(pnl_pct, 2),
                    "reason": exit_reason,
                })
                position = 0
                entry_price = 0

        else:
            # 空仓：检查买入
            should_buy = all(_eval_condition(c, i, closes, indicators) for c in entry_conditions) if entry_conditions else False
            if should_buy and cash > 0:
                # 全仓买入
                qty = int(cash / close / 100) * 100  # 按手
                if qty > 0:
                    cost = qty * close
                    cash -= cost
                    position = qty
                    entry_price = close
                    entry_date = date
                    entry_idx = i
                    trades.append({
                        "type": "buy",
                        "date": date,
                        "price": close,
                        "quantity": qty,
                        "amount": cost,
                        "reason": "满足买入条件",
                    })

    # 统计
    final_value = cash + position * closes[-1]
    # ★ 迭代2 防御：initial_capital<=0 直接报错，避免 total_return_pct 除零
    if not initial_capital or initial_capital <= 0:
        raise ValueError("initial_capital 必须 > 0")
    total_return_pct = (final_value - initial_capital) / initial_capital * 100
    days = len(kline)
    annualized = ((final_value / initial_capital) ** (252 / max(days, 1)) - 1) * 100 if final_value > 0 else 0

    # 最大回撤
    peak = 0
    max_dd = 0
    for e in equity_curve:
        eq = e["equity"]
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100
            if dd > max_dd:
                max_dd = dd

    # 夏普比率（简化：日收益的均值/标准差 * sqrt(252)）
    daily_returns = []
    for i in range(1, len(equity_curve)):
        prev = equity_curve[i - 1]["equity"]
        cur = equity_curve[i]["equity"]
        if prev > 0:
            daily_returns.append((cur - prev) / prev)
    if daily_returns and len(daily_returns) > 1:
        avg_r = sum(daily_returns) / len(daily_returns)
        var_r = sum((r - avg_r) ** 2 for r in daily_returns) / len(daily_returns)
        std_r = math.sqrt(var_r)
        sharpe = (avg_r / std_r) * math.sqrt(252) if std_r > 0 else 0
    else:
        sharpe = 0

    # 胜率
    sell_trades = [t for t in trades if t["type"] == "sell"]
    if sell_trades:
        wins = sum(1 for t in sell_trades if t.get("pnl_pct", 0) > 0)
        win_rate = wins / len(sell_trades) * 100
    else:
        win_rate = 0

    return {
        "final_capital": round(final_value, 2),
        "total_return_pct": round(total_return_pct, 2),
        "annualized_return_pct": round(annualized, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "win_rate": round(win_rate, 2),
        "total_trades": len(sell_trades),
        "trades": trades,
        "equity_curve": equity_curve,
    }


def _eval_condition(cond: Dict[str, Any], idx: int, closes: List[float], indicators: Dict[str, Any]) -> bool:
    """评估单条条件"""
    indicator_name = cond.get("indicator", "")
    operator = cond.get("operator", ">")
    threshold = cond.get("value", 0)

    # 取得指标当前值
    if indicator_name == "close":
        val = closes[idx]
    elif indicator_name == "ma":
        period = cond.get("period", 20)
        ma = indicators.get(f"ma_{period}", [])
        val = ma[idx] if idx < len(ma) else None
    elif indicator_name == "ema":
        # ★ Round 19 修复(H3)：补上 ema 分支（DSL 文档里明确支持 ema(period=12)）
        period = cond.get("period", 12)
        ema = indicators.get(f"ema_{period}", [])
        val = ema[idx] if idx < len(ema) else None
    elif indicator_name == "macd":
        line = indicators.get("macd_line", [])
        signal = indicators.get("macd_signal", [])
        if operator == "cross_above":
            if idx < 1 or idx >= len(line) or idx >= len(signal):
                return False
            # ★ 修复：macd_line/signal_line 前 26 根为 None（EMA 预热期），
            #   None <= None 会抛 TypeError → 回测 500。先判 None 再比较。
            if line[idx - 1] is None or signal[idx - 1] is None or line[idx] is None or signal[idx] is None:
                return False
            return line[idx - 1] <= signal[idx - 1] and line[idx] > signal[idx]
        elif operator == "cross_below":
            if idx < 1 or idx >= len(line) or idx >= len(signal):
                return False
            if line[idx - 1] is None or signal[idx - 1] is None or line[idx] is None or signal[idx] is None:
                return False
            return line[idx - 1] >= signal[idx - 1] and line[idx] < signal[idx]
        else:
            hist = indicators.get("macd_hist", [])
            val = hist[idx] if idx < len(hist) else None
            if val is None:
                return False
            return _compare(val, operator, threshold)
    elif indicator_name == "rsi":
        period = cond.get("period", 14)
        # ★ Round 19 修复(H3)：支持任意 period（预计算阶段已按需补算 rsi_{period}）
        rsi = indicators.get(f"rsi_{period}", [])
        val = rsi[idx] if idx < len(rsi) else None
    elif indicator_name == "volume_ratio":
        period = cond.get("period", 5)
        # ★ Round 19 修复(H3)：用 period 化的 key；旧的无后缀 key 保留兼容
        vr = indicators.get(f"volume_ratio_{period}", indicators.get("volume_ratio", []))
        val = vr[idx] if idx < len(vr) else None
    else:
        return False

    if val is None:
        return False
    return _compare(val, operator, threshold)


def _compare(val: float, op: str, threshold: float) -> bool:
    if op == ">":
        return val > threshold
    elif op == "<":
        return val < threshold
    elif op == ">=":
        return val >= threshold
    elif op == "<=":
        return val <= threshold
    elif op == "==":
        return abs(val - threshold) < 1e-9
    return False


def _ma(data: List[float], period: int) -> List[float]:
    """简单移动均线（前 period-1 个为 None）"""
    result = [None] * len(data)
    for i in range(period - 1, len(data)):
        result[i] = sum(data[i - period + 1:i + 1]) / period
    return result


def _ema(data: List[float], period: int) -> List[float]:
    """指数移动均线"""
    result = [None] * len(data)
    if len(data) < period:
        return result
    multiplier = 2 / (period + 1)
    result[period - 1] = sum(data[:period]) / period
    for i in range(period, len(data)):
        result[i] = (data[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


def _macd(data: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """MACD 线 / 信号线 / 柱状图"""
    ema_fast = _ema(data, fast)
    ema_slow = _ema(data, slow)
    macd_line = [None if ema_fast[i] is None or ema_slow[i] is None else ema_fast[i] - ema_slow[i]
                 for i in range(len(data))]
    # 信号线是 macd_line 的 EMA
    valid_macd = [v for v in macd_line if v is not None]
    if len(valid_macd) < signal:
        return macd_line, [None] * len(data), [None] * len(data)
    sig_full = _ema([v for v in macd_line if v is not None], signal)
    # 对齐
    offset = next(i for i, v in enumerate(macd_line) if v is not None)
    signal_line = [None] * offset + sig_full + [None] * (len(data) - offset - len(sig_full))
    hist = [None if (macd_line[i] is None or signal_line[i] is None) else macd_line[i] - signal_line[i]
            for i in range(len(data))]
    return macd_line, signal_line, hist


def _rsi(data: List[float], period: int = 14) -> List[float]:
    """RSI"""
    result = [None] * len(data)
    if len(data) <= period:
        return result
    gains = []
    losses = []
    for i in range(1, len(data)):
        diff = data[i] - data[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    # 平盘（gain=loss=0）时 RSI=50
    result[period] = 100 - 100 / (1 + avg_gain / avg_loss) if avg_loss > 0 else (100 if avg_gain > 0 else 50)
    for i in range(period + 1, len(data)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        result[i] = 100 - 100 / (1 + avg_gain / avg_loss) if avg_loss > 0 else (100 if avg_gain > 0 else 50)
    return result


def _volume_ratio(volumes: List[float], period: int = 5) -> List[float]:
    """量比"""
    result = [None] * len(volumes)
    for i in range(period, len(volumes)):
        avg = sum(volumes[i - period:i]) / period
        result[i] = volumes[i] / avg if avg > 0 else 1.0
    return result


# ============================================================================
# 回测结果查询
# ============================================================================
def list_results(user_id: Optional[str] = None, strategy_id: Optional[int] = None, limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    sql = "SELECT * FROM backtest_result WHERE 1=1"
    params: list = []
    if user_id:
        sql += " AND user_id=?"
        params.append(user_id)
    if strategy_id:
        sql += " AND strategy_id=?"
        params.append(strategy_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    cur.execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def get_result(result_id: int) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM backtest_result WHERE id=?", (result_id,))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("trades_json", "equity_curve_json"):
        if d.get(k):
            try:
                d[k.replace("_json", "")] = json.loads(d[k])
                del d[k]
            except Exception:
                pass
    return d


# ============================================================================
# ★ 新功能 v2 audit round 1：策略结果对比（A vs B）
# ============================================================================
def compare_backtest_results(result_id_a: int, result_id_b: int) -> Dict[str, Any]:
    """对比两个回测结果：收益、sharpe、回撤、胜率差异"""
    a = get_result(result_id_a)
    b = get_result(result_id_b)
    if not a or not b:
        return {"error": "result 不存在"}
    metrics = ["total_return_pct", "annualized_return_pct", "sharpe_ratio",
               "max_drawdown_pct", "win_rate", "total_trades", "final_capital"]
    comparison = {}
    for m in metrics:
        va = a.get(m, 0)
        vb = b.get(m, 0)
        comparison[m] = {
            "A": va,
            "B": vb,
            "diff": round((vb or 0) - (va or 0), 4),
        }
    # 推荐：B 显著优于 A → B；反之 A
    if comparison["sharpe_ratio"]["diff"] > 0.3 and comparison["total_return_pct"]["diff"] > 0:
        winner = "B"
    elif comparison["sharpe_ratio"]["diff"] < -0.3 and comparison["total_return_pct"]["diff"] < 0:
        winner = "A"
    else:
        winner = "tie"
    return {
        "A": {"id": result_id_a, "symbol": a.get("symbol"), "strategy_id": a.get("strategy_id")},
        "B": {"id": result_id_b, "symbol": b.get("symbol"), "strategy_id": b.get("strategy_id")},
        "metrics_comparison": comparison,
        "winner": winner,
    }