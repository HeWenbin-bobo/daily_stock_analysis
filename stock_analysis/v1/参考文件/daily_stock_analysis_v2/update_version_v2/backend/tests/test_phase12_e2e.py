# -*- coding: utf-8 -*-
"""Phase 12 端到端：HTTP API 全链路 + TestClient"""
import os
import sys
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 必须先 import app 才不会循环
from db.migrate import migrate
from fastapi.testclient import TestClient

# 强制 mock 外部依赖
from core import data_source as ds
from core import cron as cron_mod

# 防止 cron 启动污染
cron_mod.APS_AVAILABLE = False  # 强制跳过 scheduler

from app import app

client = TestClient(app)


def _reset():
    """每个 test 前重置 DB"""
    conn_path = "data/v2_dev_e2e.db"
    if os.path.exists(conn_path):
        os.remove(conn_path)
    # 用 e2e 数据库
    from db.migrate import DB_PATH
    import db.migrate
    db.migrate.DB_PATH = type(DB_PATH)(conn_path)  # Path


def setup_module(module):
    migrate()


# ============================================================================
# Health
# ============================================================================
def test_health():
    r = client.get("/api/v2/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    print(f"OK /health: {data}")


# ============================================================================
# 通知推送
# ============================================================================
def test_notification_flow():
    # 创建通道
    r = client.post("/api/v2/notifications/channels", json={
        "user_id": "u1",
        "channel_type": "serverchan",
        "target": "SCTkey123",
    })
    assert r.status_code == 200
    ch_id = r.json()["channel_id"]
    # 列出
    r = client.get("/api/v2/notifications/channels?user_id=u1")
    assert r.status_code == 200
    assert len(r.json()["channels"]) == 1
    # 推送测试（推到不可达 URL，会失败但有日志）
    r = client.post("/api/v2/notifications/test", json={
        "channel_id": ch_id, "title": "test", "content": "hello"
    })
    assert r.status_code == 200
    # 推送日志
    r = client.get("/api/v2/notifications/logs?user_id=u1")
    assert r.status_code == 200
    assert len(r.json()["logs"]) >= 1
    # 删除 — ★ Round 5：DELETE 需要 X-User-Id header（防匿名伪造）
    r = client.delete(
        f"/api/v2/notifications/channels/{ch_id}?user_id=u1",
        headers={"X-User-Id": "u1"},
    )
    assert r.status_code == 200, f"got {r.status_code}: {r.text}"
    print(f"OK notification flow: ch={ch_id}")


# ============================================================================
# 异动规则
# ============================================================================
def test_alert_rule_flow():
    r = client.get("/api/v2/alerts/rule-types")
    assert r.status_code == 200
    assert len(r.json()["rule_types"]) == 8

    r = client.post("/api/v2/alerts/rules", json={
        "user_id": "u1", "name": "test", "rule_type": "limit_up",
        "symbol_filter": "600519", "params": {},
    })
    assert r.status_code == 200
    rid = r.json()["rule_id"]

    r = client.get("/api/v2/alerts/rules?user_id=u1")
    assert len(r.json()["rules"]) >= 1

    r = client.put(f"/api/v2/alerts/rules/{rid}", json={"name": "renamed"})
    assert r.status_code == 200

    r = client.delete(f"/api/v2/alerts/rules/{rid}?user_id=u1", headers={"X-User-Id": "u1"})
    assert r.status_code == 200
    print(f"OK alert rule CRUD: id={rid}")


# ============================================================================
# 策略回测
# ============================================================================
def test_backtest_flow():
    # mock kline
    import datetime
    base = datetime.date(2024, 1, 1)
    kline = [{
        "date": (base + datetime.timedelta(days=i)).strftime("%Y-%m-%d"),
        "open": 100 + i * 0.3, "high": 105 + i * 0.3, "low": 95 + i * 0.3,
        "close": 100 + i * 0.3, "volume": 1000000,
    } for i in range(200)]
    from modules import backtest_engine as be_mod
    orig = be_mod.fetch_kline_with_fallback
    be_mod.fetch_kline_with_fallback = lambda *a, **kw: kline

    try:
        # 创建策略
        r = client.post("/api/v2/backtest/strategies", json={
            "user_id": "u1", "name": "test_strategy",
            "natural_language": "MACD 金叉买入",
        })
        assert r.status_code == 200
        sid = r.json()["strategy_id"]

        # 直接写 DSL（避免 LLM 编译）
        from modules.backtest_engine import update_strategy
        update_strategy(sid, compiled_dsl={
            "entry": [{"indicator": "close", "operator": ">", "value": 0}],
            "exit": [],
            "stop_loss_pct": -50.0,
            "take_profit_pct": 999.0,
            "hold_days": 30,
        })

        # 运行回测
        r = client.post("/api/v2/backtest/run", json={
            "user_id": "u1", "strategy_id": sid, "symbol": "600519",
        })
        assert r.status_code == 200, f"Backtest failed: {r.text}"
        result = r.json()
        assert "total_return_pct" in result
        assert "sharpe_ratio" in result

        # 列结果
        r = client.get("/api/v2/backtest/results?user_id=u1")
        assert r.status_code == 200
        assert len(r.json()["results"]) >= 1

        # 删除
        client.delete(f"/api/v2/backtest/strategies/{sid}")
        print(f"OK backtest flow: total_return={result['total_return_pct']}%, sharpe={result['sharpe_ratio']}")
    finally:
        be_mod.fetch_kline_with_fallback = orig


# ============================================================================
# 模拟盘
# ============================================================================
def test_paper_trading_flow():
    # mock 行情
    from modules import paper_trading as pt_mod
    orig = pt_mod.fetch_quote_with_fallback
    pt_mod.fetch_quote_with_fallback = lambda code: {"price": 100.0, "name": "test", "industry": "tech"}
    try:
        # 创建账户
        r = client.post("/api/v2/paper/accounts", json={"user_id": "u1", "name": "test_acc"})
        acc_id = r.json()["account_id"]

        # 下单
        r = client.post(f"/api/v2/paper/accounts/{acc_id}/orders", json={
            "symbol": "600519", "side": "buy", "quantity": 100, "price": 100.0,
        })
        assert r.status_code == 200

        # 概览
        r = client.get(f"/api/v2/paper/accounts/{acc_id}/overview")
        assert r.status_code == 200
        overview = r.json()
        assert overview["current_equity"] > 0

        # 快照
        r = client.post(f"/api/v2/paper/accounts/{acc_id}/snapshot", json={"snapshot_date": "2026-07-30"})
        assert r.status_code == 200

        # 排行榜
        r = client.get("/api/v2/paper/leaderboard")
        assert r.status_code == 200
        board = r.json()["leaderboard"]
        assert any(b["account_id"] == acc_id for b in board)
        print(f"OK paper flow: equity=¥{overview['current_equity']:,.0f}, leaderboard has {len(board)}")
    finally:
        pt_mod.fetch_quote_with_fallback = orig


# ============================================================================
# 社区
# ============================================================================
def test_community_flow():
    # 创建分享
    r = client.post("/api/v2/community/shares", json={
        "user_id": "u1", "resource_type": "backtest_result",
        "resource_id": "123", "title": "我的回测",
    })
    assert r.status_code == 200
    token = r.json()["share_token"]

    # 解析
    r = client.get(f"/api/v2/community/shares/{token}")
    assert r.status_code == 200

    # 关注
    r = client.post("/api/v2/community/follow", json={"follower_id": "u2", "followee_id": "u1"})
    assert r.status_code == 200

    # profile
    r = client.get("/api/v2/community/profile/u1")
    assert r.status_code == 200
    profile = r.json()
    assert profile["followers"] >= 1
    print(f"OK community: token={token}, followers={profile['followers']}")


# ============================================================================
# 组合管理
# ============================================================================
def test_portfolio_flow():
    r = client.post("/api/v2/portfolio/holdings", json={
        "user_id": "u1", "symbol": "600519", "cost": 1500.0, "quantity": 100,
    })
    assert r.status_code == 200

    r = client.get("/api/v2/portfolio/holdings?user_id=u1")
    assert r.status_code == 200
    assert len(r.json()["holdings"]) == 1

    r = client.get("/api/v2/portfolio/overview?user_id=u1")
    assert r.status_code == 200
    overview = r.json()
    assert "warnings" in overview
    assert "industry_pct" in overview

    r = client.get("/api/v2/portfolio/report?user_id=u1")
    assert r.status_code == 200
    report = r.json()
    assert "不构成" in report["content"]  # 强制风险提示
    assert "投资有风险" in report["content"]
    print(f"OK portfolio: warnings={len(overview['warnings'])}, disclaimer present")


# ============================================================================
# 数据 + LLM + 审计
# ============================================================================
def test_data_sources_and_llm():
    # 数据源健康度（mock 失败以产生数据）
    r = client.get("/api/v2/data/health")
    assert r.status_code == 200

    # LLM 用量
    r = client.get("/api/v2/llm/usage?days=30")
    assert r.status_code == 200

    # LLM chat（mock LLM 无 key）
    r = client.post("/api/v2/llm/chat", json={"prompt": "hello", "user_id": "u1"})
    assert r.status_code == 200
    data = r.json()
    assert "content" in data

    # 审计
    r = client.get("/api/v2/audit/recent")
    assert r.status_code == 200
    assert "logs" in r.json()
    print(f"OK data/llm/audit: chat_provider={data['provider']}")


# ============================================================================
# 错误路径
# ============================================================================
def test_404_on_nonexistent_resource():
    r = client.get("/api/v2/community/shares/nonexistent_token")
    assert r.status_code == 404
    print("OK 404 returned for nonexistent share")


def test_400_on_invalid_backtest_request():
    r = client.post("/api/v2/backtest/run", json={"user_id": "u1", "strategy_id": 99999, "symbol": "x"})
    # 不存在的 strategy_id 应 404
    assert r.status_code == 404 or r.status_code == 400
    print(f"OK invalid backtest returns {r.status_code}")


def test_security_headers():
    r = client.get("/api/v2/health")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    # HSTS 仅在 https 下加（middleware 设计如此）
    print(f"OK security headers: X-CTO={r.headers.get('X-Content-Type-Options')}, X-FO={r.headers.get('X-Frame-Options')}, HSTS={r.headers.get('Strict-Transport-Security', '(http only)')}")


if __name__ == "__main__":
    setup_module(None)
    test_health()
    test_notification_flow()
    test_alert_rule_flow()
    test_backtest_flow()
    test_paper_trading_flow()
    test_community_flow()
    test_portfolio_flow()
    test_data_sources_and_llm()
    test_404_on_nonexistent_resource()
    test_400_on_invalid_backtest_request()
    test_security_headers()
    print("\n=== Phase 12 e2e: ALL 11 PASSED ===")