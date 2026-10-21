"""Phase 92: 分页 limit 参数必须拒绝负值

SQLite 的 `LIMIT -1` 表示**不限制**，`LIMIT -5` 同理。而所有列表端点的
`limit` 只声明了上界 `Query(..., le=N)`，没有下界，于是：

    GET /api/v2/cron/run-log?limit=-5   -> 返回全部 272 行

声称"最多 500 条"的上限被一个负号彻底绕过。数据量小时只是响应变大，
长期运行（cron_run_log 每天 ~100 行、audit_log 只写不清）后会变成
一次重型全表响应，足以拖慢甚至打挂 Web 线程池。

同类缺口覆盖 endpoints_v2.py / endpoints_hot.py 共 22 处分页参数，
统一补 `ge=1`。这里挑几个有代表性的端点守住语义。
"""
import os
import sys

import pytest

BACKEND = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, BACKEND)


@pytest.fixture()
def client():
    from db.migrate import migrate

    migrate()
    from fastapi.testclient import TestClient
    from app import app

    return TestClient(app)


NEGATIVE_LIMIT_CASES = [
    ("/api/v2/cron/run-log", {"limit": -5}),
    ("/api/v2/cron/run-log", {"limit": -1}),
    ("/api/v2/audit/recent", {"limit": -1}),
    ("/api/v2/notifications/logs", {"limit": -100}),
    ("/api/v2/alerts/triggers", {"limit": -3}),
    ("/api/v2/community/hot-list", {"limit": -1}),
    ("/api/v2/paper/leaderboard", {"limit": -50}),
    ("/api/v2/backtest/results", {"limit": -1}),
    ("/api/v2/backtest/hyperopt/runs", {"limit": -1}),
]


@pytest.mark.parametrize("path,params", NEGATIVE_LIMIT_CASES)
def test_negative_limit_is_rejected(client, path, params):
    """负数 limit 必须是 422，不能落成 SQLite 的"不限制" """
    r = client.get(path, params=params, headers={"X-User-Id": "u"})
    assert r.status_code == 422, (
        f"{path}?{params} 应为 422（参数非法），实际 {r.status_code}。"
        f"负 limit 在 SQLite 中等于不限行数。"
    )


def test_zero_limit_is_rejected(client):
    """limit=0 同样无意义（返回空集易被误读为"没有数据"），一并拦下"""
    r = client.get("/api/v2/cron/run-log", params={"limit": 0}, headers={"X-User-Id": "u"})
    assert r.status_code == 422


def test_valid_limit_still_works(client):
    """正例：正常 limit 不受影响"""
    r = client.get("/api/v2/cron/run-log", params={"limit": 5}, headers={"X-User-Id": "u"})
    assert r.status_code == 200
    assert isinstance(r.json()["logs"], list)
    assert len(r.json()["logs"]) <= 5


def test_upper_bound_still_enforced(client):
    """上界校验不能被下界修复破坏"""
    r = client.get("/api/v2/cron/run-log", params={"limit": 99999}, headers={"X-User-Id": "u"})
    assert r.status_code == 422
