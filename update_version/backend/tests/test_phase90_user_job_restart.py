"""Phase 90: 用户自定义 cron 任务必须在重启后恢复（cron_user_jobs 重放）

缺陷：`add_user_job()` 的 docstring 承诺"调度器就绪后下次 init() 启动会重新登记
到 APScheduler"，但 `init()` 只调用了 `_register_builtin_jobs()` 和
`register_user_scheduled_tasks()` —— 后者读的是 **history.db 的 scheduled_tasks**
（另一套机制），从来没有人读 `cron_user_jobs`。

后果：用户通过 `/cron/user-jobs` 建的定时任务，在容器/进程重启后
  - 元数据还在（`list_user_jobs` 照常返回，前端照常展示）
  - 但调度器里没有对应 job，**永远不会触发**
且没有任何日志或状态提示。schema 里 `idx_user_jobs_enabled` 的注释
"scheduler 重启时按 enabled 过滤" 也证实了这是漏实现，而不是有意设计。
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture()
def cron_env(tmp_path, monkeypatch):
    """给出一个干净 env：独立 db + 可控的调度器重放"""
    from core import cron
    from db import migrate as migrate_mod

    db_path = tmp_path / f"cron_restart_{uuid.uuid4().hex[:8]}.db"
    monkeypatch.setattr(migrate_mod, "DB_PATH", db_path)
    monkeypatch.setattr(cron, "DB_PATH", db_path)
    migrate_mod.migrate()

    cron.init(str(db_path))
    yield cron, db_path
    cron.shutdown()


def _scheduled_job_ids(cron):
    if not cron._scheduler:
        return set()
    return {j.id for j in cron._scheduler.get_jobs()}


def test_user_job_survives_scheduler_restart(cron_env):
    """建任务 → 模拟重启 → 任务仍应被调度"""
    cron, db_path = cron_env

    assert cron.add_user_job(
        user_id="alice",
        job_id="my_daily_scan",
        func_name="run_alert_scan",
        cron_expr="*/15 * * * *",
        description="alice 的扫描",
    ) is True
    assert "my_daily_scan" in _scheduled_job_ids(cron), "add_user_job 应立即登记到调度器"

    # 模拟进程重启：关掉调度器，再按启动流程重新 init()
    cron.shutdown()
    cron.init(str(db_path))

    scheduled = _scheduled_job_ids(cron)
    assert "my_daily_scan" in scheduled, (
        "重启后用户任务丢失：cron_user_jobs 里还有记录，但调度器里没有这个 job。"
        f"当前已调度: {sorted(scheduled)}"
    )


def test_disabled_user_job_is_not_registered_after_restart(cron_env):
    """enabled=0 的任务重启后不应被调度（schema 注释承诺的语义）"""
    cron, db_path = cron_env
    assert cron.add_user_job(
        user_id="alice",
        job_id="paused_job",
        func_name="run_alert_scan",
        cron_expr="*/15 * * * *",
    ) is True

    # 直接置 enabled=0（API 侧没有单独的启停端点，走库）
    conn = cron.get_conn()
    try:
        conn.execute("UPDATE cron_user_jobs SET enabled=0 WHERE job_id=?", ("paused_job",))
    finally:
        conn.close()

    cron.shutdown()
    cron.init(str(db_path))

    assert "paused_job" not in _scheduled_job_ids(cron), (
        "enabled=0 的用户任务重启后不应被调度"
    )


def test_direct_db_edit_of_cron_expr_takes_effect_after_restart(cron_env):
    """直接改库里 cron_expr（绕过 API）后重启，必须按新表达式调度。

    这条才真正区分「重放 cron_user_jobs」与「jobstore 侥幸持久化」：
    走 `add_user_job` 时 `replace_existing=True` 会顺手把 jobstore 里的记录
    就地改写成新表达式，所以重启后本来就是新的 —— 那种写法测不出差异。
    只有绕过 API 改库，才能暴露"重启不读 cron_user_jobs"的缺陷
    （用户停用/改期都可能由运维直接改库完成）。
    """
    cron, db_path = cron_env
    assert cron.add_user_job(
        user_id="alice",
        job_id="reschedule_me",
        func_name="run_alert_scan",
        cron_expr="*/15 * * * *",
    ) is True

    conn = cron.get_conn()
    try:
        conn.execute(
            "UPDATE cron_user_jobs SET cron_expr='0 9 * * *' WHERE job_id=?",
            ("reschedule_me",),
        )
    finally:
        conn.close()

    cron.shutdown()
    cron.init(str(db_path))

    job = cron._scheduler.get_job("reschedule_me")
    assert job is not None, "重启后任务应仍在调度器里"
    assert "hour='9'" in str(job.trigger), (
        f"重启后仍按旧表达式调度：trigger={job.trigger}，期望 hour='9'"
    )


def test_deleted_user_job_stays_gone_after_restart(cron_env):
    """删除后重启不应复活"""
    cron, db_path = cron_env
    assert cron.add_user_job(
        user_id="alice",
        job_id="temp_job",
        func_name="run_alert_scan",
        cron_expr="*/15 * * * *",
    ) is True
    assert cron.remove_user_job("temp_job") is True

    cron.shutdown()
    cron.init(str(db_path))

    assert "temp_job" not in _scheduled_job_ids(cron)


def test_job_with_unsupported_args_is_skipped_not_scheduled_to_fail(cron_env):
    """带 args 但目标函数不接受参数 → 跳过并告警，而不是注册一个必崩的 job。

    白名单里四个任务签名都是 `()`，而 API 接受 `args`（默认 {}）。
    若把非空 args 注册进去，每次触发都会 TypeError —— 重启正是静默重放它的时机，
    因此必须在注册时拦下。
    """
    cron, db_path = cron_env
    conn = cron.get_conn()
    try:
        conn.execute(
            """INSERT INTO cron_user_jobs
               (job_id, user_id, func_name, cron_expr, args_json, description, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, '', 1, ?)""",
            ("needs_args", "alice", "run_alert_scan", "*/5 * * * *", '{"limit": 10}', 0.0),
        )
    finally:
        conn.close()

    cron.shutdown()
    cron.init(str(db_path))

    assert "needs_args" not in _scheduled_job_ids(cron)


def test_restart_tolerates_bad_rows(cron_env):
    """库里存了非法 func_name / cron 的行时，重启不能整体失败"""
    cron, db_path = cron_env
    conn = cron.get_conn()
    try:
        conn.execute(
            """INSERT INTO cron_user_jobs
               (job_id, user_id, func_name, cron_expr, args_json, description, enabled, created_at)
               VALUES (?, ?, ?, ?, '{}', '', 1, ?)""",
            ("bad_func", "alice", "no_such_function", "*/5 * * * *", 0.0),
        )
        conn.execute(
            """INSERT INTO cron_user_jobs
               (job_id, user_id, func_name, cron_expr, args_json, description, enabled, created_at)
               VALUES (?, ?, ?, ?, '{}', '', 1, ?)""",
            ("bad_cron", "alice", "run_alert_scan", "not a cron", 0.0),
        )
    finally:
        conn.close()

    # 必须能正常启动，且坏行被跳过
    cron.shutdown()
    cron.init(str(db_path))
    cron.register_user_cron_jobs()

    scheduled = _scheduled_job_ids(cron)
    assert "bad_func" not in scheduled
    assert "bad_cron" not in scheduled
