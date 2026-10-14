# -*- coding: utf-8 -*-
"""
Hyperopt 最小版测试（独立可跑，不依赖网络）
- 用随机游走合成 K 线 mock 数据源
- 验证：搜索空间构建 / 参数回写 / 优化收敛 / 样本外验证 / 落库 / 查跑记录
"""
import sys
import os
import random
import math
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ★ 迭代2 修复（测试隔离）：切到独立测试库 v2_test.db，防止 hyperopt 测试写满生产库
os.environ["V4_DB_PATH"] = str(Path(__file__).resolve().parent.parent / "data" / "v2_test.db")

from db.migrate import migrate
migrate()  # 确保测试库 schema 就绪（幂等）

# --- mock 数据源：生成随机游走 K 线 ---
def _gen_kline(n: int = 300, seed: int = 7) -> list:
    rng = random.Random(seed)
    price = 50.0
    rows = []
    for i in range(n):
        drift = 0.001 if i < n * 0.65 else -0.002  # 前 65% 上涨，后 35% 下跌（让策略有区分度）
        ret = rng.gauss(drift, 0.02)
        price *= (1 + ret)
        rows.append({
            "date": f"2025-{i // 28 + 1:02d}-{i % 28 + 1:02d}",
            "open": round(price * (1 + rng.gauss(0, 0.003)), 2),
            "high": round(price * (1 + abs(rng.gauss(0, 0.01))), 2),
            "low": round(price * (1 - abs(rng.gauss(0, 0.01))), 2),
            "close": round(price, 2),
            "volume": int(rng.uniform(1e6, 5e6)),
            "amount": 0.0,
        })
    return rows


import modules.hyperopt_engine as he
import modules.backtest_engine as be
from core import data_source as ds

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name} {detail}")
        FAILURES.append(name)


# 1. 搜索空间构建
print("== 1. 搜索空间构建 ==")
dsl = {
    "entry": [
        {"indicator": "ma", "period": 20, "operator": ">", "value": 0},
        {"indicator": "rsi", "period": 14, "operator": "<", "value": 30},
    ],
    "exit": [
        {"indicator": "rsi", "period": 14, "operator": ">", "value": 70},
    ],
    "stop_loss_pct": -5.0,
    "take_profit_pct": 10.0,
    "hold_days": 5,
}
space = he._build_search_space(dsl)
keys = [s["key"] for s in space]
check("含 entry.0.period (ma)", "entry.0.period" in keys)
check("ma.value 不优化(相对比较无法推断范围)", "entry.0.value" not in keys)
check("含 entry.1.period (rsi)", "entry.1.period" in keys)
check("含 entry.1.value (rsi)", "entry.1.value" in keys)
check("含 exit.0.value (rsi)", "exit.0.value" in keys)
check("含 stop_loss_pct", "stop_loss_pct" in keys)
check("含 take_profit_pct", "take_profit_pct" in keys)
check("含 hold_days", "hold_days" in keys)
print(f"  空间大小: {len(space)} 个参数")

# 2. 参数回写
print("== 2. 参数回写 ==")
params = {"entry.0.period": 30, "entry.0.value": 0.5, "stop_loss_pct": -8.0, "hold_days": 10}
dsl2 = he._apply_params(dsl, params)
check("period 回写", dsl2["entry"][0]["period"] == 30)
check("value 回写", dsl2["entry"][0]["value"] == 0.5)
check("stop_loss 回写", dsl2["stop_loss_pct"] == -8.0)
check("hold_days 回写", dsl2["hold_days"] == 10)
check("原 DSL 未被污染", dsl["entry"][0]["period"] == 20)

# 3. 优化收敛 + 样本外验证（mock 数据源）
print("== 3. run_hyperopt 全流程 ==")
# ★ 迭代4 修复：mock 数据源函数赋值后必须恢复。此前不恢复，本文件
#   （字母序 test_hyperopt_min 排在 test_phase18/22 之前）把
#   ds.fetch_kline_with_fallback 永久替换为返回 _gen_kline(300) 的 lambda
#   → phase18 的 kline 测试拿到 300 根合成 K 线（期望 30/120 根）、
#   phase22 的 mock 测试拿到无 is_mock 属性的外部列表 → 假失败。
_orig_fetch_kline = ds.fetch_kline_with_fallback
kline = _gen_kline(300)
ds.fetch_kline_with_fallback = lambda *a, **kw: kline  # mock 数据源

# 建策略
conn = __import__("db.migrate", fromlist=["get_conn"]).get_conn()
cur = conn.cursor()
now = time.time()
cur.execute(
    "INSERT INTO backtest_strategy (user_id, name, natural_language, compiled_dsl, created_at, updated_at) VALUES (?,?,?,?,?,?)",
    ("test_user", "hyperopt_test", "测试策略", __import__("json").dumps(dsl, ensure_ascii=False), now, now),
)
sid = cur.lastrowid

t0 = time.time()
res = he.run_hyperopt("test_user", sid, symbol="600519", epochs=30, loss_fn="sharpe")
dt = time.time() - t0
print(f"  30 epochs 耗时 {dt:.1f}s")

check("返回 run_id", res.get("run_id") > 0)
check("best_params 非空", bool(res.get("best_params")))
check("train_metrics 有 sharpe", "sharpe_ratio" in res["train_metrics"])
check("oos_metrics 存在", "oos_metrics" in res and res["oos_metrics"].get("total_return_pct") is not None)
check("train/test 区间分离", res["train_range"][1] <= res["test_range"][0])
print(f"  best_loss={res['best_loss']:.4f}  train_sharpe={res['train_metrics'].get('sharpe_ratio')}  oos_return={res['oos_metrics'].get('total_return_pct')}%")

# 4. 落库查询
print("== 4. 落库查询 ==")
run = he.get_run(res["run_id"])
check("get_run 可取", run is not None and run["status"] == "completed")
check("get_run 解析 best_params", isinstance(run.get("best_params"), dict))
runs = he.list_runs(user_id="test_user")
check("list_runs 含本次", any(r["id"] == res["run_id"] for r in runs))
epochs = he.list_epochs(res["run_id"])
check("epochs 已记录", len(epochs) == 30)

# 5. loss 函数
print("== 5. loss 函数 ==")
m = {"total_return_pct": 20, "annualized_return_pct": 15, "sharpe_ratio": 1.2,
     "max_drawdown_pct": 10, "win_rate": 55, "total_trades": 10}
check("sharpe loss = -1.2", abs(he._loss_sharpe(m) - (-1.2)) < 1e-9)
# multi_metric: -20/100 + 0.055*10/100 - 55/100 = -0.2 + 0.0055 - 0.55 = -0.7445（负值正常，loss 越小越好）
check("multi_metric = -0.7445", abs(he._loss_multi_metric(m) - (-0.7445)) < 1e-9)
m2 = dict(m, total_trades=2)
check("MIN_TRADES 惩罚", he._loss_multi_metric(m2) == he.MAX_LOSS)

# 6. 真 Sortino（P1）：从 equity_curve 算下行偏差，与旧 Calmar 变体不同
print("== 6. 真 Sortino ==")
eq_up = [{"equity": 100 + i * 2} for i in range(11)]          # 单调上涨 → 无下行
eq_dd = [{"equity": 100 - (i % 3) * 10} for i in range(10)]   # 反复回撤
m_up = dict(m, equity_curve=eq_up)
m_dd = dict(m, equity_curve=eq_dd)
check("无下行收益 → MAX_LOSS(保守)", he._loss_sortino(m_up) == he.MAX_LOSS)
check("有回撤 → 有限值", abs(he._loss_sortino(m_dd)) < he.MAX_LOSS)
check("sortino 与 sharpe 不同(非 Calmar)", he._loss_sortino(m_dd) != he._loss_sortino(m_up))

# 7. 早停生效（P1）：Terminator 正常构造
print("== 7. 早停 ==")
from optuna.terminator import BestValueStagnationEvaluator, Terminator
check("terminator 构造正常", Terminator(BestValueStagnationEvaluator(he.EARLY_STOP_EPOCHS)) is not None)

# 8. 应用最优参数闭环（P0）
print("== 8. 应用参数闭环 ==")
run = he.get_run(res["run_id"])
check("apply 前 best_params 存在", isinstance(run.get("best_params"), dict) and len(run["best_params"]) > 0)
ap = he.apply_best_params(res["run_id"], "test_user")
check("apply 返回 strategy_id", ap["strategy_id"] == sid)
check("apply 返回 applied_params", isinstance(ap.get("applied_params"), dict))
# 验证策略 DSL 已被更新
s_after = be.get_strategy(sid)
dsl_after = s_after["compiled_dsl"]
check("策略 DSL 已含新参数", dsl_after["stop_loss_pct"] == run["best_params"]["stop_loss_pct"])
check("原 DSL 已备份到 description", "hyperopt" in (s_after.get("description") or ""))
# 归属校验
try:
    he.apply_best_params(res["run_id"], "other_user")
    check("越权 apply 被拒", False)
except ValueError:
    check("越权 apply 被拒", True)

# 9. 脏 run 清理（P2）
print("== 9. 脏 run 清理 ==")
conn = __import__("db.migrate", fromlist=["get_conn"]).get_conn()
conn.execute(
    "INSERT INTO hyperopt_run (strategy_id, user_id, symbol, status, created_at) VALUES (?,?,?,'running',?)",
    (sid, "test_user", "600519", time.time() - 7200),  # 2 小时前 → stale
)
stale_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
n = he.cleanup_stale_runs(stale_seconds=3600)
check("stale run 被标记 interrupted", n >= 1)
stale_run = he.get_run(stale_id)
check("stale run 状态=interrupted", stale_run["status"] == "interrupted")

# ★ 迭代4 修复：恢复被 mock 的数据源函数（脚本式测试在退出前统一还原，
#   防止污染字母序在其后的 test_phase18/22 的 kline 相关测试）
ds.fetch_kline_with_fallback = _orig_fetch_kline

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败: {FAILURES}")
    sys.exit(1)
print("✅ 全部通过")
