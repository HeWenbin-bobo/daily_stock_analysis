"""Phase 91: /cron/user-jobs 的身份必须来自 X-User-Id，且读端点不能横向越权

探索期实测（容器内，修复前）：

  1) 身份伪造 —— POST 时 handler 用 `payload.get("user_id", "default")`，
     UserIDMiddleware 写入的 `request.state.user_id` 被完全忽略。
     带 `X-User-Id: alice` 建任务，库里存下来的 user_id 却是 "default"。
     由于 job_id 是主键、`add_user_job` 又先做归属校验，身份错乱会直接
     导致"自己的任务删不掉/改不了"。

  2) 横向越权 —— `GET /cron/user-jobs` 把 `user_id` query 直接透传，
     不带参数就返回**全部**用户的任务。实测 bob 带 `X-User-Id: bob` 拿到了
     alice 的 job_id / func_name / cron_expr / args_json。
     DELETE 早已有 `_check_resource_owner`，读端点漏了，构成完整越权链。
"""
import os
import sys
import uuid

import pytest

BACKEND = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, BACKEND)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个测试独立 DB + 独立调度器，避免限流桶/库状态互相污染"""
    from db import migrate as migrate_mod

    db_path = tmp_path / f"cron_idor_{uuid.uuid4().hex[:8]}.db"
    monkeypatch.setattr(migrate_mod, "DB_PATH", db_path)
    migrate_mod.migrate()

    from core import cron
    monkeypatch.setattr(cron, "DB_PATH", db_path)
    cron.init(str(db_path))

    from fastapi.testclient import TestClient
    from app import app

    yield TestClient(app)
    cron.shutdown()


def _post_job(client, uid, job_id="j1", func="run_alert_scan", cron_expr="*/30 * * * *"):
    return client.post(
        "/api/v2/cron/user-jobs",
        json={"job_id": job_id, "func_name": func, "cron_expr": cron_expr},
        headers={"X-User-Id": uid},
    )


def test_post_uses_header_identity_not_payload_default(client):
    """带 X-User-Id 建的任务，user_id 必须等于 header，而不是 "default" """
    assert _post_job(client, "alice").status_code == 200

    listed = client.get("/api/v2/cron/user-jobs", headers={"X-User-Id": "alice"})
    assert listed.status_code == 200
    jobs = listed.json()["user_jobs"]
    assert len(jobs) == 1
    assert jobs[0]["user_id"] == "alice", (
        f"身份被忽略：user_id={jobs[0]['user_id']!r}，期望 'alice'"
    )


def test_post_rejects_identity_mismatch(client):
    """payload 里的 user_id 与 header 冲突 → 403"""
    r = client.post(
        "/api/v2/cron/user-jobs",
        json={
            "job_id": "spoof",
            "func_name": "run_alert_scan",
            "cron_expr": "*/30 * * * *",
            "user_id": "victim",
        },
        headers={"X-User-Id": "attacker"},
    )
    assert r.status_code == 403, f"期望 403，实际 {r.status_code}: {r.text[:120]}"


def test_post_requires_identity(client):
    """完全没有身份（无 header、无 payload user_id）→ 401"""
    r = client.post(
        "/api/v2/cron/user-jobs",
        json={"job_id": "anon", "func_name": "run_alert_scan", "cron_expr": "*/30 * * * *"},
    )
    assert r.status_code == 401, f"期望 401，实际 {r.status_code}: {r.text[:120]}"


def test_get_does_not_leak_other_users_jobs(client):
    """bob 列任务时不能看到 alice 的任务"""
    assert _post_job(client, "alice", job_id="alice_private").status_code == 200

    r = client.get("/api/v2/cron/user-jobs", headers={"X-User-Id": "bob"})
    assert r.status_code == 200
    job_ids = {j["job_id"] for j in r.json()["user_jobs"]}
    assert "alice_private" not in job_ids, (
        f"横向越权：bob 看到了 alice 的任务 {job_ids}"
    )


def test_get_rejects_query_user_id_conflicting_with_header(client):
    """显式传 user_id query 冒充他人 → 403"""
    assert _post_job(client, "alice", job_id="alice_private2").status_code == 200

    r = client.get(
        "/api/v2/cron/user-jobs?user_id=alice", headers={"X-User-Id": "bob"}
    )
    assert r.status_code == 403, f"期望 403，实际 {r.status_code}: {r.text[:120]}"


def test_get_requires_identity(client):
    """无身份时不应返回任何人的任务"""
    assert _post_job(client, "alice", job_id="alice_private3").status_code == 200

    r = client.get("/api/v2/cron/user-jobs")
    assert r.status_code == 401, f"期望 401，实际 {r.status_code}: {r.text[:120]}"


def test_owner_can_list_and_delete_own_job(client):
    """正例：归属本人时 GET/DELETE 都正常"""
    assert _post_job(client, "alice", job_id="mine").status_code == 200

    r = client.get("/api/v2/cron/user-jobs", headers={"X-User-Id": "alice"})
    assert {j["job_id"] for j in r.json()["user_jobs"]} == {"mine"}

    d = client.delete("/api/v2/cron/user-jobs/mine", headers={"X-User-Id": "alice"})
    assert d.status_code == 200, f"本人删除失败: {d.status_code} {d.text[:120]}"
