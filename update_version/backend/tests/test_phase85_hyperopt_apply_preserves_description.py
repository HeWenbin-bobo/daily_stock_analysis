# -*- coding: utf-8 -*-
"""Regression test: applying hyperopt params must not destroy the strategy description.

``apply_best_params`` used to do:

    backup = f"[hyperopt #{run_id} 备份] {json.dumps(dsl)[:500]}"
    be.update_strategy(strategy_id, compiled_dsl=new_dsl, description=backup)

That overwrote the user's own strategy notes with a truncated JSON blob — the
notes were gone (and they are exposed through GET /backtest/strategies), while
the "backup" was too short to actually restore the previous DSL. Verified: a
strategy described "我的策略说明" came back with a `[hyperopt #1 备份]` prefix.

The previous DSL now goes into hyperopt_run.prev_dsl_json (added by a lightweight
column migration), and description is left alone.

Run: ``python -m pytest update_version/backend/tests/test_phase85_hyperopt_apply_preserves_description.py``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

import modules.backtest_engine as be
import modules.hyperopt_engine as he


_DESC = "我的策略说明：重点观察龙头股"
_ORIG_DSL = {
    "entry": [{"indicator": "ma", "operator": ">", "value": 10, "period": 20}],
    "exit": [],
}


def _make_completed_run(strategy_id: int, user_id: str, params: dict) -> int:
    conn = he.get_conn()
    conn.execute(
        """INSERT INTO hyperopt_run
           (strategy_id, user_id, symbol, epochs, loss_fn, status, best_params_json, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (strategy_id, user_id, "600519", 10, "sharpe", "completed", json.dumps(params), 1.0),
    )
    return conn.execute("SELECT MAX(id) FROM hyperopt_run").fetchone()[0]


@pytest.fixture()
def strategy():
    sid = be.create_strategy("u_ho", "ho", "均线上穿买入", description=_DESC)
    be.update_strategy(sid, compiled_dsl=json.dumps(_ORIG_DSL))
    return sid


def test_description_is_preserved(strategy) -> None:
    rid = _make_completed_run(strategy, "u_ho", {"entry.0.value": 12.0})

    he.apply_best_params(rid, "u_ho")

    assert be.get_strategy(strategy)["description"] == _DESC


def test_previous_dsl_is_stored_for_rollback(strategy) -> None:
    rid = _make_completed_run(strategy, "u_ho", {"entry.0.value": 12.0})

    he.apply_best_params(rid, "u_ho")

    row = he.get_conn().execute(
        "SELECT prev_dsl_json FROM hyperopt_run WHERE id=?", (rid,)
    ).fetchone()
    assert row["prev_dsl_json"], "上一版 DSL 未保存，无法回滚"
    assert json.loads(row["prev_dsl_json"]) == _ORIG_DSL


def test_new_params_actually_applied(strategy) -> None:
    rid = _make_completed_run(strategy, "u_ho", {"entry.0.value": 12.0})

    he.apply_best_params(rid, "u_ho")

    dsl = be.get_strategy(strategy)["compiled_dsl"]
    if isinstance(dsl, str):
        dsl = json.loads(dsl)
    assert dsl["entry"][0]["value"] == 12.0


def test_apply_still_rejects_other_users(strategy) -> None:
    rid = _make_completed_run(strategy, "u_ho", {"entry.0.value": 12.0})
    with pytest.raises(ValueError, match="无权"):
        he.apply_best_params(rid, "someone_else")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
