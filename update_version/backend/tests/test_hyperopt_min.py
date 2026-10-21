# -*- coding: utf-8 -*-
"""Hyperopt 引擎测试（合成 K 线，不依赖网络）。

★ 移植说明：本文件原先是一个**脚本**——模块级直接执行，失败时调用
  `sys.exit(1)`，且模块级把 `V4_DB_PATH` 指向 `data/v2_test.db`。
  被 pytest 采集时有两个严重后果：

  1. `sys.exit(1)` 在**收集阶段**触发，整轮测试以
     `INTERNALERROR> SystemExit: 1` 中断，只剩 129 个用例被收集
     （本应 700+），而退出码被 pytest 吞掉，看起来像"跑完了"。
  2. 模块级 `os.environ["V4_DB_PATH"] = .../data/v2_test.db` 绕过
     conftest 的临时库隔离，直接在仓库工作目录里生成 97MB 的残留库。

  现改写为标准 pytest 用例：不再设置 V4_DB_PATH（交给 conftest），
  不再 sys.exit，数据源 mock 用 monkeypatch 自动还原。
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

import modules.backtest_engine as be
import modules.hyperopt_engine as he
from core import data_source as ds


def _gen_kline(n: int = 300, seed: int = 7) -> list:
    """随机游走 K 线：前 65% 上涨、后 35% 下跌，让策略有区分度。"""
    rng = random.Random(seed)
    price = 50.0
    rows = []
    for i in range(n):
        drift = 0.001 if i < n * 0.65 else -0.002
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


_DSL = {
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


# ---------------------------------------------------------------------------
# 1. 搜索空间构建
# ---------------------------------------------------------------------------
def test_search_space_includes_periods_but_not_relative_ma_value() -> None:
    keys = [s["key"] for s in he._build_search_space(_DSL)]
    assert "entry.0.period" in keys
    assert "entry.1.period" in keys
    assert "entry.1.value" in keys
    assert "exit.0.value" in keys
    assert "stop_loss_pct" in keys
    assert "take_profit_pct" in keys
    assert "hold_days" in keys
    # ma 的 value 是相对比较，无法推断合理范围 → 不自动优化
    assert "entry.0.value" not in keys


# ---------------------------------------------------------------------------
# 2. 参数回写
# ---------------------------------------------------------------------------
def test_apply_params_writes_back_without_mutating_the_source() -> None:
    params = {"entry.0.period": 30, "entry.0.value": 0.5,
              "stop_loss_pct": -8.0, "hold_days": 10}
    dsl2 = he._apply_params(_DSL, params)

    assert dsl2["entry"][0]["period"] == 30
    assert dsl2["entry"][0]["value"] == 0.5
    assert dsl2["stop_loss_pct"] == -8.0
    assert dsl2["hold_days"] == 10
    # 原 DSL 必须保持不变（纯函数）
    assert _DSL["entry"][0]["period"] == 20


# ---------------------------------------------------------------------------
# 3. 全流程：优化 → 落库 → 查询
# ---------------------------------------------------------------------------
@pytest.fixture()
def hyperopt_run(monkeypatch):
    """跑一轮 30 epoch 的优化，返回 (strategy_id, result)。"""
    kline = _gen_kline(300)
    monkeypatch.setattr(ds, "fetch_kline_with_fallback", lambda *a, **kw: kline)

    conn = be.get_conn()
    now = time.time()
    cur = conn.execute(
        "INSERT INTO backtest_strategy "
        "(user_id, name, natural_language, compiled_dsl, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("test_user", "hyperopt_test", "测试策略",
         json.dumps(_DSL, ensure_ascii=False), now, now),
    )
    sid = cur.lastrowid

    res = he.run_hyperopt("test_user", sid, symbol="600519", epochs=30, loss_fn="sharpe")
    return sid, res


def test_run_hyperopt_returns_complete_result(hyperopt_run) -> None:
    _, res = hyperopt_run
    assert res.get("run_id") > 0
    assert res.get("best_params")
    assert "sharpe_ratio" in res["train_metrics"]
    assert res["oos_metrics"].get("total_return_pct") is not None
    # train 与 test 区间必须分离，否则样本外验证毫无意义
    assert res["train_range"][1] <= res["test_range"][0]


def test_run_hyperopt_persists_run_and_epochs(hyperopt_run) -> None:
    _, res = hyperopt_run

    run = he.get_run(res["run_id"])
    assert run is not None and run["status"] == "completed"
    assert isinstance(run.get("best_params"), dict)

    assert any(r["id"] == res["run_id"] for r in he.list_runs(user_id="test_user"))
    assert len(he.list_epochs(res["run_id"])) == 30


# ---------------------------------------------------------------------------
# 4. loss 函数
# ---------------------------------------------------------------------------
_M = {"total_return_pct": 20, "annualized_return_pct": 15, "sharpe_ratio": 1.2,
      "max_drawdown_pct": 10, "win_rate": 55, "total_trades": 10}


def test_sharpe_loss_is_negated_sharpe() -> None:
    assert abs(he._loss_sharpe(_M) - (-1.2)) < 1e-9


def test_multi_metric_loss_combines_return_drawdown_winrate() -> None:
    # -20/100 + 0.055*10/100 - 55/100 = -0.7445
    assert abs(he._loss_multi_metric(_M) - (-0.7445)) < 1e-9


def test_multi_metric_penalises_too_few_trades() -> None:
    assert he._loss_multi_metric(dict(_M, total_trades=2)) == he.MAX_LOSS


# ---------------------------------------------------------------------------
# 5. 真 Sortino
# ---------------------------------------------------------------------------
def test_sortino_returns_max_loss_when_no_downside() -> None:
    eq_up = [{"equity": 100 + i * 2} for i in range(11)]   # 单调上涨
    assert he._loss_sortino(dict(_M, equity_curve=eq_up)) == he.MAX_LOSS


def test_sortino_is_finite_with_drawdowns_and_differs_from_upside() -> None:
    eq_dd = [{"equity": 100 - (i % 3) * 10} for i in range(10)]
    eq_up = [{"equity": 100 + i * 2} for i in range(11)]
    dd = he._loss_sortino(dict(_M, equity_curve=eq_dd))
    assert abs(dd) < he.MAX_LOSS
    assert dd != he._loss_sortino(dict(_M, equity_curve=eq_up))


# ---------------------------------------------------------------------------
# 6. 早停
# ---------------------------------------------------------------------------
def test_terminator_can_be_constructed() -> None:
    from optuna.terminator import BestValueStagnationEvaluator, Terminator
    assert Terminator(BestValueStagnationEvaluator(he.EARLY_STOP_EPOCHS)) is not None


# ---------------------------------------------------------------------------
# 7. 应用最优参数闭环
# ---------------------------------------------------------------------------
def test_apply_best_params_updates_dsl_and_keeps_description(hyperopt_run) -> None:
    sid, res = hyperopt_run
    best = he.get_run(res["run_id"])["best_params"]
    assert best

    ap = he.apply_best_params(res["run_id"], "test_user")
    assert ap["strategy_id"] == sid
    assert isinstance(ap.get("applied_params"), dict)

    s_after = be.get_strategy(sid)
    dsl_after = s_after["compiled_dsl"]
    if isinstance(dsl_after, str):
        dsl_after = json.loads(dsl_after)
    assert dsl_after["stop_loss_pct"] == best["stop_loss_pct"]

    # 用户写的 description 不能被备份串覆盖（旧实现的缺陷）
    assert "hyperopt" not in (s_after.get("description") or "")
    # 上一版 DSL 存进 run 记录，便于回滚
    prev = he.get_run(res["run_id"]).get("prev_dsl_json")
    assert prev and "stop_loss_pct" in str(prev)


def test_apply_best_params_rejects_other_user(hyperopt_run) -> None:
    _, res = hyperopt_run
    with pytest.raises(ValueError):
        he.apply_best_params(res["run_id"], "other_user")


# ---------------------------------------------------------------------------
# 8. 脏 run 清理
# ---------------------------------------------------------------------------
def test_cleanup_marks_stale_running_runs_interrupted(hyperopt_run) -> None:
    sid, _ = hyperopt_run
    conn = be.get_conn()
    cur = conn.execute(
        "INSERT INTO hyperopt_run (strategy_id, user_id, symbol, status, created_at) "
        "VALUES (?,?,?,'running',?)",
        (sid, "test_user", "600519", time.time() - 7200),   # 2 小时前
    )
    stale_id = cur.lastrowid

    assert he.cleanup_stale_runs(stale_seconds=3600) >= 1
    assert he.get_run(stale_id)["status"] == "interrupted"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
