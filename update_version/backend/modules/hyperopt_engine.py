# -*- coding: utf-8 -*-
"""
Hyperopt 最小版 —— 借鉴 freqtrade Hyperopt 的参数优化引擎

思路（砍掉 freqtrade 的大量机制，只保留核心）：
- 用 optuna TPE 贝叶斯采样，在策略 DSL 的数值参数上搜索
- 目标函数 = 复用 backtest_engine._execute_backtest（纯函数、毫秒级，无需预计算）
- loss 函数化：sharpe / sortino / multi_metric，与 freqtrade 同思路
- MIN_TRADES 惩罚：交易数过少 → MAX_LOSS（防止"不交易=回撤0"的假最优）
- 早停：best loss 连续 N epoch 不改善即停
- ★ 强制样本外验证：优化只在 train 段（前 80% 时间）跑，test 段（后 20%）单独报告
  （freqtrade 不强制，但这是防过拟合的红线）
"""
import time
import json
import logging
import math
import os
from typing import Dict, Any, List, Optional, Callable

import optuna
from optuna.samplers import TPESampler

from db.migrate import get_conn
from core.data_source import fetch_kline_with_fallback
from modules import backtest_engine as be

logger = logging.getLogger(__name__)

MAX_LOSS = 100000.0
MIN_TRADES = 5  # 交易数低于此 → MAX_LOSS（防"不交易=最优"）
EARLY_STOP_EPOCHS = 20  # best loss 连续 N epoch 不改善 → 停
TEST_SPLIT_RATIO = 0.2  # 最后 20% 时间留作样本外

# ============================================================================
# DSL → optuna 搜索空间
# ============================================================================
# 各参数自动推断范围（策略 JSON 里可显式写 "optimize": {...} 覆盖）
_PERIOD_RANGES = {  # 指标 period 的搜索范围
    "ma": (5, 60),
    "ema": (5, 60),
    "rsi": (7, 21),
    "volume_ratio": (3, 20),
    "macd": None,  # macd fast/slow/signal 是结构参数，不优化
}
_RISK_RANGES = {
    "stop_loss_pct": (-15.0, -2.0),
    "take_profit_pct": (3.0, 20.0),
    "hold_days": (1, 20),
}
# value 阈值自动推断（按指标+运算符）
_VALUE_RANGES = {
    "rsi": (30.0, 85.0),
    "volume_ratio": (0.5, 3.5),
    "macd_hist": (-3.0, 3.0),
    "close": None,  # close 相对值无法安全推断，不自动优化
}


def _split_by_date(kline: List[Dict[str, Any]], ratio: float = TEST_SPLIT_RATIO):
    """按时间顺序切 train/test，返回 (train, test)。"""
    n = len(kline)
    cut = int(n * (1 - ratio))
    return kline[:cut], kline[cut:]


def _build_search_space(dsl: Dict[str, Any]) -> List[Dict[str, Any]]:
    """解析 DSL，返回参数声明列表 [{key, kind, low, high}]。
    key 形如 "entry.0.value" / "stop_loss_pct"，kind ∈ int|float。"""
    space: List[Dict[str, Any]] = []
    explicit = dsl.get("optimize") or {}  # 显式覆盖，形如 {"stop_loss_pct": [-8, -3]}

    def add(key: str, low, high, kind):
        if key in explicit:
            lo, hi = explicit[key]
            space.append({"key": key, "kind": kind, "low": lo, "high": hi})
        elif low is not None:
            space.append({"key": key, "kind": kind, "low": low, "high": high})

    for cond in (dsl.get("entry") or []) + (dsl.get("exit") or []):
        if not cond:
            continue
        ind = cond.get("indicator", "")
        # 找到条件在哪个列表
        pos = None
        for list_name in ("entry", "exit"):
            lst = dsl.get(list_name) or []
            if cond in lst:
                pos = (list_name, lst.index(cond))
                break
        if pos is None:
            continue
        list_name, i = pos
        # period（如果 DSL 带 period）
        if "period" in cond:
            rng = _PERIOD_RANGES.get(ind)
            if rng:
                add(f"{list_name}.{i}.period", rng[0], rng[1], "int")
        # value 阈值
        if "value" in cond:
            rng = _VALUE_RANGES.get(ind)
            if ind == "macd" and cond.get("operator") not in ("cross_above", "cross_below"):
                rng = _VALUE_RANGES["macd_hist"]
            add(f"{list_name}.{i}.value", (rng or (None, None))[0], (rng or (None, None))[1], "float")

    # 风险参数
    add("stop_loss_pct", *_RISK_RANGES["stop_loss_pct"], "float")
    add("take_profit_pct", *_RISK_RANGES["take_profit_pct"], "float")
    add("hold_days", *_RISK_RANGES["hold_days"], "int")

    return space


def _apply_params(dsl: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    """把采样到的参数写回 DSL（深拷贝，不污染原策略）。"""
    import copy
    dsl2 = copy.deepcopy(dsl)
    for key, val in params.items():
        parts = key.split(".")
        if len(parts) == 3 and parts[0] in ("entry", "exit"):
            list_name, i, field = parts
            lst = dsl2.get(list_name)
            if lst and int(i) < len(lst):
                lst[int(i)][field] = val
        elif key in dsl2:
            dsl2[key] = val
    return dsl2


# ============================================================================
# Loss 函数（对照 freqtrade hyperopt_loss/，返回越小越好）
# ============================================================================
def _loss_sharpe(metrics: Dict[str, Any]) -> float:
    return -metrics["sharpe_ratio"] if metrics["sharpe_ratio"] else MAX_LOSS


def _daily_returns(metrics: Dict[str, Any]) -> List[float]:
    """从 equity_curve 提取日收益序列（缺失时返回空）。"""
    curve = metrics.get("equity_curve") or []
    out = []
    for i in range(1, len(curve)):
        prev = curve[i - 1].get("equity", 0)
        cur = curve[i].get("equity", 0)
        if prev > 0:
            out.append((cur - prev) / prev)
    return out


def _loss_sortino(metrics: Dict[str, Any]) -> float:
    """真 Sortino：下行偏差（只算负收益的波动）替代总波动。
    ★ P1 修复：此前是 -annualized/max_dd（Calmar 变体），现在从 equity_curve 算真 Sortino。
    年化收益 = 日均收益 × 252；下行偏差 = sqrt(Σ负收益² / 总天数)；年化下行偏差 = 日偏差 × √252。"""
    rets = _daily_returns(metrics)
    if len(rets) < 2:
        return MAX_LOSS
    downside = [r for r in rets if r < 0]
    if not downside:
        return MAX_LOSS  # 无负收益 → 无法评估下行风险，保守 MAX_LOSS（防"从不亏"假最优）
    ann_return = (sum(rets) / len(rets)) * 252
    dd_dev = (sum(r * r for r in downside) / len(rets)) ** 0.5  # 日频下行偏差
    sortino = ann_return / (dd_dev * 252 ** 0.5) if dd_dev > 0 else 0
    return -sortino


def _loss_multi_metric(metrics: Dict[str, Any]) -> float:
    """综合：收益 + 胜率 + 回撤（借鉴 freqtrade MultiMetricHyperOptLoss 思路）。
    返回越小越好：收益越高越好(取负)、回撤越小越好(正惩罚)、胜率越高越好(取负)。"""
    ret = metrics["total_return_pct"] or 0
    wr = metrics["win_rate"] or 0
    dd = metrics["max_drawdown_pct"] or 0
    trades = metrics["total_trades"] or 0
    if trades < MIN_TRADES:
        return MAX_LOSS
    # 收益每 1% 给 1 分；回撤每 1% 扣 0.055 分（与 freqtrade DRAWDOWN_MULT 一致）
    return -ret / 100 + 0.055 * dd / 100 - wr / 100


LOSS_FUNCTIONS: Dict[str, Callable] = {
    "sharpe": _loss_sharpe,
    "sortino": _loss_sortino,
    "multi_metric": _loss_multi_metric,
}


# ============================================================================
# 主入口
# ============================================================================
def run_hyperopt(
    user_id: str,
    strategy_id: int,
    *,
    symbol: str,
    epochs: int = 100,
    loss_fn: str = "sharpe",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> Dict[str, Any]:
    """跑一轮参数优化。返回 {run_id, best_params, best_loss, train_metrics, oos_metrics}"""
    if loss_fn not in LOSS_FUNCTIONS:
        raise ValueError(f"loss_fn 必须是 {list(LOSS_FUNCTIONS.keys())} 之一")
    # ★ 迭代1 修复：epochs 校验（负数/0/超大值 → 友好报错，防 DoS）
    try:
        epochs = int(epochs)
    except (TypeError, ValueError):
        raise ValueError("epochs 必须是正整数")
    if epochs <= 0:
        raise ValueError("epochs 必须 > 0")
    if epochs > 5000:
        raise ValueError("epochs 过大（上限 5000），请降低后重试")
    strategy = be.get_strategy(strategy_id)
    if not strategy:
        raise ValueError(f"strategy {strategy_id} 不存在")
    dsl = strategy.get("compiled_dsl")
    if not dsl or "error" in dsl:
        raise ValueError("策略未编译或 DSL 有误")

    # 拉 K 线
    kline = fetch_kline_with_fallback(symbol, days=365 * 2)
    if not kline:
        raise ValueError(f"无法获取 {symbol} K 线")
    if start_date:
        kline = [k for k in kline if k["date"] >= start_date]
    if end_date:
        kline = [k for k in kline if k["date"] <= end_date]
    if len(kline) < 60:
        raise ValueError(f"K 线数据不足（仅 {len(kline)} 天），至少需 60 天")

    train, test = _split_by_date(kline)
    space = _build_search_space(dsl)
    if not space:
        raise ValueError("DSL 无可优化参数（都是固定值/不支持自动推断），请在策略 JSON 加 optimize 字段")

    # 建 run 记录
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO hyperopt_run
           (strategy_id, user_id, symbol, epochs, loss_fn,
            train_start, train_end, test_start, test_end, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?, 'running', ?)""",
        (strategy_id, user_id, symbol, epochs, loss_fn,
         train[0]["date"], train[-1]["date"], test[0]["date"], test[-1]["date"], time.time()),
    )
    run_id = cur.lastrowid or 0

    sampler = TPESampler(n_startup_trials=30, seed=42)
    study = optuna.create_study(direction="minimize", sampler=sampler)

    # ★ 早停：用 optuna Terminator（BestValueStagnationEvaluator）——连续 EARLY_STOP_EPOCHS
    #   个 trial best loss 不改善即停。★ P1 修复：旧 MedianPruner 对单目标回测不生效
    #   （无 intermediate value），EARLY_STOP_EPOCHS 原本是死代码。
    from optuna.terminator import BestValueStagnationEvaluator, Terminator
    terminator = Terminator(BestValueStagnationEvaluator(EARLY_STOP_EPOCHS))

    # ★ joblib 并行（借鉴 freqtrade）：批量 ask → 并行评估 → 批量 tell
    #   目标函数是纯函数（train 段 K 线 + DSL + 指标），线程安全，直接 joblib Parallel
    import joblib
    n_jobs = int(os.environ.get("HYPEROPT_JOBS", "0") or 0)  # 0/负 → 自动
    if n_jobs <= 0:
        import multiprocessing
        n_jobs = min(max(1, multiprocessing.cpu_count() - 1), 8)

    def _evaluate_one(trial_params: tuple) -> Dict[str, Any]:
        """单个参数组合的评估（joblib worker 内调用）。"""
        trial_idx, params = trial_params
        dsl_trial = _apply_params(dsl, params)
        try:
            metrics = be._execute_backtest(train, dsl_trial, initial_capital=1000000.0)
        except Exception:
            metrics = None
        loss = LOSS_FUNCTIONS[loss_fn](metrics) if metrics else MAX_LOSS
        return {
            "trial_idx": trial_idx,
            "params": params,
            "loss": loss,
            "metrics": metrics,
        }

    # 保存每个 epoch 到 DB（独立连接，供并行 worker 写）
    def _save_epoch(run_id_, epoch_no, params, loss, metrics):
        try:
            get_conn().execute(
                """INSERT INTO hyperopt_epoch (run_id, epoch, params_json, loss, metrics_json, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (run_id_, epoch_no, json.dumps(params, ensure_ascii=False),
                 loss, json.dumps(metrics, ensure_ascii=False, default=str), time.time()),
            )
        except Exception as e:
            logger.warning("save epoch failed: %s", e)

    try:
        # 并行模式：批量 ask → joblib 并行 → tell + 落库
        # optuna ask/tell 是顺序 API；并行版需要自己管理 batch（参考 freqtrade get_asked_points）
        remaining = epochs
        trial_no = 0
        with joblib.Parallel(n_jobs=n_jobs, prefer="threads") as parallel:
            while remaining > 0:
                batch = min(remaining, n_jobs * 2)
                asked = [study.ask() for _ in range(batch)]
                params_list = []
                for t in asked:
                    p = {}
                    for s in space:
                        if s["kind"] == "int":
                            p[s["key"]] = t.suggest_int(s["key"], int(s["low"]), int(s["high"]))
                        else:
                            p[s["key"]] = t.suggest_float(s["key"], s["low"], s["high"])
                    params_list.append(p)
                results = parallel(
                    joblib.delayed(_evaluate_one)((trial_no + i, p))
                    for i, p in enumerate(params_list)
                )
                for t, r in zip(asked, results):
                    study.tell(t, r["loss"])
                    trial_no += 1
                    _save_epoch(run_id, trial_no, r["params"], r["loss"], r["metrics"])
                remaining -= batch
                # ★ 早停生效（P1）：每个 batch 后检查 best loss 是否已停滞
                if terminator.should_terminate(study):
                    logger.info("early stop: best loss 连续 %d epochs 未改善，提前终止", EARLY_STOP_EPOCHS)
                    break
    except KeyboardInterrupt:
        logger.info("hyperopt interrupted by user")

    best = study.best_trial
    best_params = best.params

    # 训练段指标（best）
    dsl_best = _apply_params(dsl, best_params)
    train_metrics = be._execute_backtest(train, dsl_best, initial_capital=1000000.0)
    # 样本外验证（test 段）
    oos_metrics = be._execute_backtest(test, dsl_best, initial_capital=1000000.0)
    oos_loss = LOSS_FUNCTIONS[loss_fn](oos_metrics)

    # 更新 run 记录
    cur.execute(
        """UPDATE hyperopt_run SET
           best_params_json=?, best_loss=?, best_train_json=?, oos_metrics_json=?,
           oos_loss=?, status='completed', completed_at=?
           WHERE id=?""",
        (json.dumps(best_params, ensure_ascii=False), best.value,
         json.dumps(train_metrics, ensure_ascii=False, default=str),
         json.dumps(oos_metrics, ensure_ascii=False, default=str), oos_loss, time.time(), run_id),
    )

    return {
        "run_id": run_id,
        "best_params": best_params,
        "best_loss": best.value,
        "train_metrics": train_metrics,
        "oos_metrics": oos_metrics,
        "oos_loss": oos_loss,
        # ★ 迭代3 修复：样本外无交易 = 策略失效/过拟合，必须显式标记
        #   （此前前端把 total_return=0 显示成"样本外持平"，误导用户）
        "oos_no_trades": bool(oos_metrics and (oos_metrics.get("total_trades") or 0) == 0),
        "train_range": (train[0]["date"], train[-1]["date"]),
        "test_range": (test[0]["date"], test[-1]["date"]),
        "search_space_size": len(space),
        "n_trials": study.trials.__len__(),
    }


def list_runs(user_id: Optional[str] = None, strategy_id: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    sql = "SELECT * FROM hyperopt_run WHERE 1=1"
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


# ============================================================================
# P0：应用最优参数到策略（闭环最后一环，对齐 freqtrade try_export_params）
# ============================================================================
def apply_best_params(run_id: int, user_id: str) -> Dict[str, Any]:
    """把 hyperopt run 的 best_params 写回策略 compiled_dsl。

    - 原 DSL 备份到 strategy.description（防误操作，可手工恢复）
    - 校验 run 归属 + 状态 completed
    """
    run = get_run(run_id)
    if not run:
        raise ValueError(f"run {run_id} 不存在")
    if run.get("user_id") != user_id:
        raise ValueError("无权应用他人优化结果")
    if run.get("status") != "completed" or not run.get("best_params"):
        raise ValueError("run 未完成或无最优参数")

    strategy_id = run["strategy_id"]
    strategy = be.get_strategy(strategy_id)
    if not strategy:
        raise ValueError(f"strategy {strategy_id} 不存在")
    if strategy.get("user_id") != user_id:
        raise ValueError("无权修改他人策略")

    dsl = strategy.get("compiled_dsl")
    if not dsl or not isinstance(dsl, dict):
        raise ValueError("策略无 compiled_dsl")

    best = run["best_params"]
    # 备份原 DSL 到 description（时间戳）
    backup = f"[hyperopt #{run_id} 备份] {json.dumps(dsl, ensure_ascii=False)[:500]}"
    new_dsl = json.dumps(_apply_params(dsl, best), ensure_ascii=False)
    be.update_strategy(strategy_id, compiled_dsl=new_dsl, description=backup)
    return {
        "strategy_id": strategy_id,
        "run_id": run_id,
        "applied_params": best,
        "backup_hint": f"原 DSL 已备份到策略 description（hyperopt #{run_id} 备份）",
    }


# ============================================================================
# P2：清理脏 run（API 超时/进程中断 → 永久卡 running）
# ============================================================================
def cleanup_stale_runs(stale_seconds: float = 3600) -> int:
    """把超过 stale_seconds 未更新且仍 running 的 run 标记为 interrupted。"""
    conn = get_conn()
    cur = conn.cursor()
    cutoff = time.time() - stale_seconds
    cur.execute(
        "UPDATE hyperopt_run SET status='interrupted', completed_at=? "
        "WHERE status='running' AND created_at < ?",
        (time.time(), cutoff),
    )
    return cur.rowcount


def get_run(run_id: int) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM hyperopt_run WHERE id=?", (run_id,))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("best_params_json", "best_train_json", "oos_metrics_json"):
        if d.get(k):
            try:
                d[k.replace("_json", "")] = json.loads(d[k])
                del d[k]
            except Exception:
                pass
    # ★ 迭代3 修复：旧 run 无 oos_no_trades 字段 → 从 oos_metrics 补算
    if "oos_no_trades" not in d:
        oos = d.get("oos_metrics") or {}
        d["oos_no_trades"] = bool((oos.get("total_trades") or 0) == 0)
    return d


def list_epochs(run_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM hyperopt_epoch WHERE run_id=? ORDER BY epoch ASC LIMIT ?",
        (run_id, limit),
    )
    return [dict(r) for r in cur.fetchall()]
