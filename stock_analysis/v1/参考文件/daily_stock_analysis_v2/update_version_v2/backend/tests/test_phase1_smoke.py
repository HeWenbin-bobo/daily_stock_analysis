# -*- coding: utf-8 -*-
"""Phase 1 smoke test: APScheduler + 持久化 + 任务包装"""
import time
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrate import get_conn
from core import cron as cron_mod


def test_apscheduler_available():
    assert cron_mod.APS_AVAILABLE, "APScheduler not installed"
    print(f"OK apscheduler available: APS_AVAILABLE={cron_mod.APS_AVAILABLE}")


def test_init_scheduler():
    """初始化调度器（需要单独的 db 文件避免污染）"""
    test_db = "data/test_cron.db"
    # 删除旧的
    if os.path.exists(test_db):
        os.remove(test_db)
    scheduler = cron_mod.init(db_path=test_db)
    assert scheduler is not None
    assert cron_mod._scheduler is not None
    print("OK scheduler initialized")


def test_builtin_jobs_registered():
    jobs = cron_mod.list_jobs()
    job_ids = [j["id"] for j in jobs]
    assert "builtin_market_review_daily" in job_ids
    assert "builtin_alert_scan" in job_ids
    assert "builtin_paper_settle" in job_ids
    assert "builtin_community_rank" in job_ids
    print(f"OK builtin jobs registered: {len(jobs)} ({job_ids})")


def test_list_jobs_format():
    jobs = cron_mod.list_jobs()
    assert len(jobs) > 0
    for j in jobs:
        assert "id" in j and "name" in j
        assert j["next_run"] is not None, f"Job {j['id']} missing next_run"
        assert "cron" in j["trigger"].lower() or "interval" in j["trigger"].lower()
    print(f"OK job format: {len(jobs)} jobs with next_run")


def test_wrap_with_log_records_run():
    """包装函数执行后应在 cron_run_log 中留下一条 success 记录"""
    conn = get_conn()
    conn.execute("DELETE FROM cron_run_log WHERE job_id='test_wrap_job'")

    counter = {"n": 0}

    def my_job():
        counter["n"] += 1
        return "ok"

    wrapped = cron_mod._wrap_with_log("test_wrap_job", my_job)
    result = wrapped()
    assert result == "ok"
    assert counter["n"] == 1

    cur = conn.cursor()
    cur.execute("SELECT * FROM cron_run_log WHERE job_id='test_wrap_job'")
    rows = list(cur.fetchall())
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["duration_seconds"] is not None
    assert rows[0]["finished_at"] is not None
    print(f"OK wrap log: status={rows[0]['status']}, duration={rows[0]['duration_seconds']:.4f}s")


def test_wrap_with_log_records_failure():
    conn = get_conn()
    conn.execute("DELETE FROM cron_run_log WHERE job_id='test_fail_job'")

    def my_fail():
        raise RuntimeError("simulated")

    wrapped = cron_mod._wrap_with_log("test_fail_job", my_fail)
    try:
        wrapped()
    except RuntimeError:
        pass

    cur = conn.cursor()
    cur.execute("SELECT * FROM cron_run_log WHERE job_id='test_fail_job'")
    rows = list(cur.fetchall())
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "simulated" in rows[0]["error_message"]
    print(f"OK wrap log failure: error={rows[0]['error_message'][:30]}")


def test_resolve_builtin_func():
    func = cron_mod._resolve_func("run_market_review_daily")
    assert callable(func)
    print(f"OK resolve builtin func: {func.__name__}")


def test_resolve_unknown_raises():
    try:
        cron_mod._resolve_func("malicious_func")
        assert False, "Should have raised"
    except ValueError as e:
        assert "Unknown func" in str(e)
        print(f"OK resolve unknown blocked: {e}")


def test_shutdown_and_restart():
    cron_mod.shutdown()
    assert cron_mod._scheduler is None
    # 重启
    test_db = "data/test_cron2.db"
    if os.path.exists(test_db):
        os.remove(test_db)
    cron_mod.init(db_path=test_db)
    assert cron_mod._scheduler is not None
    print("OK shutdown + restart")


def test_cleanup():
    cron_mod.shutdown()
    print("OK cleanup")


if __name__ == "__main__":
    test_apscheduler_available()
    test_init_scheduler()
    test_builtin_jobs_registered()
    test_list_jobs_format()
    test_wrap_with_log_records_run()
    test_wrap_with_log_records_failure()
    test_resolve_builtin_func()
    test_resolve_unknown_raises()
    test_shutdown_and_restart()
    test_cleanup()
    print("\n=== Phase 1 smoke test: ALL 10 PASSED ===")