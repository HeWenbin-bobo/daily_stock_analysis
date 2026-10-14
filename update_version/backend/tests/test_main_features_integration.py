# -*- coding: utf-8 -*-
"""主项目新功能接入 v4 的端点测试：
- POST /api/v2/etf-rotation/run          — ETF 双动量轮动
- GET/POST /api/v2/portfolio-p0/*        — 组合总览 (P0 组合域)
- POST /api/v2/web-intent/resolve        — Web Chat 意图识别

网络类（ETF 真实抓取）用 mock 数据源保持离线可跑；意图解析走规则路径
（不依赖 LLM）；组合 P0 直接操作主项目 storage 层（独立 SQLite）。
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from core import cron as cron_mod
    cron_mod.APS_AVAILABLE = False
    from app import app
    return TestClient(app)


# ============================================================================
# 组合 P0：账户 CRUD + 快照（真实走主项目 storage，无网络）
# ============================================================================
def test_portfolio_p0_account_crud_and_snapshot(client):
    from api.endpoints_v2 import _get_pf_service

    svc = _get_pf_service()
    # 清理同名词账户（幂等）
    for a in svc.list_accounts():
        if a["name"] == "__v4_test_account__":
            svc.repo.deactivate_account(a["id"]) if hasattr(svc.repo, "deactivate_account") else None

    r = client.post("/api/v2/portfolio-p0/accounts", json={
        "name": "__v4_test_account__", "market": "cn", "base_currency": "CNY",
    })
    assert r.status_code == 200, r.text
    account = r.json()
    assert account["id"] and account["market"] == "cn"

    # 缺字段 → 400
    r = client.post("/api/v2/portfolio-p0/accounts", json={"name": "x"})
    assert r.status_code == 400

    # 记一笔买入
    r = client.post("/api/v2/portfolio-p0/trades", json={
        "account_id": account["id"], "symbol": "600519",
        "trade_date": "2026-10-09", "side": "buy",
        "quantity": 100, "price": 1500.0,
    })
    assert r.status_code == 200, r.text
    assert "id" in r.json()

    # 卖超 → 400
    r = client.post("/api/v2/portfolio-p0/trades", json={
        "account_id": account["id"], "symbol": "600519",
        "trade_date": "2026-10-10", "side": "sell",
        "quantity": 999, "price": 1600.0,
    })
    assert r.status_code == 400

    # 快照有持仓
    r = client.get("/api/v2/portfolio-p0/snapshot", params={"account_id": account["id"]})
    assert r.status_code == 200
    snap = r.json()
    assert snap["account_count"] >= 1

    # 列表包含新账户
    r = client.get("/api/v2/portfolio-p0/accounts")
    assert r.status_code == 200
    assert any(a["name"] == "__v4_test_account__" for a in r.json()["accounts"])


# ============================================================================
# Web 意图：规则路径（无 config 也可用规则；此处带 config，但消息高置信直达不触 LLM）
# ============================================================================
def test_web_intent_rule_path(client, monkeypatch):
    # 强制走规则：monkeypatch 掉 _get_intent_resolver 换成无 config 实例
    from src.agent.web_intent_resolver import WebIntentResolver

    rule_only = WebIntentResolver()  # 无 config → LLM 兜底禁用
    from api import endpoints_hot
    monkeypatch.setattr(endpoints_hot, "_intent_resolver", rule_only)

    r = client.post("/api/v2/web-intent/resolve", json={"message": "分析一下600519"})
    assert r.status_code == 200, r.text
    groups = r.json()["groups"]
    assert len(groups) == 1 and len(groups[0]) == 1
    task = groups[0][0]
    assert task["intent"] == "stock_analysis"
    assert any(s["code"] == "600519" for s in task["stocks"])
    assert task["source"] == "rule"

    # 缺 message → 400
    r = client.post("/api/v2/web-intent/resolve", json={})
    assert r.status_code == 400

    # 多子消息切分
    r = client.post("/api/v2/web-intent/resolve", json={"message": "查查600519的行情"})
    assert r.status_code == 200
    assert r.json()["groups"]


# ============================================================================
# ETF 轮动：mock 数据源离线跑（不依赖网络）
# ============================================================================
def test_etf_rotation_run_offline(client, monkeypatch):
    import numpy as np
    import pandas as pd
    from api import endpoints_hot

    def fake_run(config, send_notification=False, **kwargs):
        """绕过真实抓取：直接构造 RotationReport。"""
        from dataclasses import replace
        from src.services.etf_rotation_service import RotationReport
        import pandas as pd
        return RotationReport(
            markdown="# ETF 轮动信号（测试）\n",
            as_of=pd.Timestamp("2026-10-13"),
            target_weights={"510300": 0.5, "511880": 0.5},
            failed_codes={},
            warnings=[],
        )

    # monkeypatch service 层入口：loader 恢复 sys.modules 后 service 模块全名缓存命中
    import sys
    from pathlib import Path
    main_root = str(Path(endpoints_hot.__file__).resolve().parent.parent.parent.parent)
    if main_root not in sys.path:
        sys.path.insert(0, main_root)
    import src.services.etf_rotation_service as svc_mod
    monkeypatch.setattr(svc_mod, "run_etf_rotation", fake_run)

    r = client.post("/api/v2/etf-rotation/run")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["as_of"] == "2026-10-13"
    assert data["target_weights"] == {"510300": 0.5, "511880": 0.5}
    assert "测试" in data["markdown"]
