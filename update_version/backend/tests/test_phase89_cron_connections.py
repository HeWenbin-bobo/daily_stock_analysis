"""Phase 89: cron.py 的 SQLite 连接必须关闭（fd 泄漏回归）

探索期实测：每条未关闭的 sqlite3 连接占用 **2 个 fd**（DB 文件 + -wal）。
cron.py 的监听器 / 运行日志 / 用户任务 CRUD 全是 `conn = get_conn()` 后不关闭，
其中 `_attach_listener_if_needed` 的 `on_event` 在**每个** cron 任务执行后都会跑一次。
生产配置下每天约 76 次内置触发（异动扫描 */5 × 12h + 复盘 2 + 结算 1 + 榜单 1），
即每天稳定泄漏上百个 fd —— 长期运行的容器会先耗尽 fd，再让所有端点报 EMFILE。

这里用计数型假连接断言「拿到的每个连接都被 close」，不依赖真实 fd 计数
（Windows 上 fd 语义不同，计数方案不可移植）。
"""
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class _CountingConn:
    """包装真实连接，记录 close 次数"""

    def __init__(self, inner, registry):
        self._inner = inner
        self._registry = registry

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self._registry["closed"] += 1
        return self._inner.close()


@pytest.fixture()
def cron_mod(tmp_path, monkeypatch):
    """加载 cron 模块并把 get_conn 换成计数包装（每个测试独立 db 文件）"""
    from core import cron

    # tmp_path 已是 per-test 目录，文件名加唯一后缀避免与其他用例撞库
    db_path = tmp_path / f"cron_fd_{uuid.uuid4().hex[:8]}.db"
    real_get_conn = cron.get_conn
    registry = {"opened": 0, "closed": 0}

    def counting_get_conn():
        registry["opened"] += 1
        return _CountingConn(real_get_conn(), registry)

    monkeypatch.setattr(cron, "get_conn", counting_get_conn)
    # 真正的库路径在 db.migrate.DB_PATH（cron.get_conn 是它的导入别名），
    # 只改 cron.DB_PATH 不影响连接指向 —— 必须两边都指到本测试的库。
    from db import migrate as migrate_mod

    monkeypatch.setattr(migrate_mod, "DB_PATH", db_path)
    monkeypatch.setattr(cron, "DB_PATH", db_path)
    migrate_mod.migrate()
    cron.init(str(db_path))
    # init() 内部的 migrate/get_conn 用的是被 monkeypatch 替换后的函数，
    # 把启动阶段的开销清掉，只统计测试自身触发的连接。
    registry["opened"] = 0
    registry["closed"] = 0
    try:
        yield cron, registry, db_path
    finally:
        cron.shutdown()


def test_list_user_jobs_closes_connection(cron_mod):
    cron, registry, _ = cron_mod
    cron.add_user_job(
        user_id="alice",
        job_id="job_a",
        func_name="run_alert_scan",
        cron_expr="*/10 * * * *",
        description="probe",
    )
    registry["opened"] = registry["closed"] = 0

    rows = cron.list_user_jobs("alice")

    assert [r["job_id"] for r in rows] == ["job_a"]
    assert registry["opened"] > 0, "本测试应至少打开一个连接"
    assert registry["closed"] == registry["opened"], (
        f"list_user_jobs 泄漏连接：opened={registry['opened']} closed={registry['closed']}"
    )


def test_list_run_log_closes_connection(cron_mod):
    cron, registry, _ = cron_mod
    registry["opened"] = registry["closed"] = 0

    cron.list_run_log(limit=5)

    assert registry["opened"] > 0
    assert registry["closed"] == registry["opened"], (
        f"list_run_log 泄漏连接：opened={registry['opened']} closed={registry['closed']}"
    )


def test_remove_user_job_closes_connection(cron_mod):
    cron, registry, _ = cron_mod
    cron.add_user_job(
        user_id="alice",
        job_id="job_b",
        func_name="run_alert_scan",
        cron_expr="*/10 * * * *",
    )
    registry["opened"] = registry["closed"] = 0

    assert cron.remove_user_job("job_b") is True

    assert registry["closed"] == registry["opened"], (
        f"remove_user_job 泄漏连接：opened={registry['opened']} closed={registry['closed']}"
    )
    assert "job_b" not in {r["job_id"] for r in cron.list_user_jobs()}


def test_add_user_job_closes_connection_on_all_paths(cron_mod):
    """归属校验查询 + 写入两步都要关闭"""
    cron, registry, _ = cron_mod
    registry["opened"] = registry["closed"] = 0

    cron.add_user_job(
        user_id="alice",
        job_id="job_c",
        func_name="run_alert_scan",
        cron_expr="*/10 * * * *",
    )
    assert registry["closed"] == registry["opened"], (
        f"add_user_job 泄漏连接：opened={registry['opened']} closed={registry['closed']}"
    )


def test_rejected_add_user_job_still_closes_connection(cron_mod):
    """被拒绝的分支（job_id 已属于别人）也必须关闭查询用的连接"""
    cron, registry, _ = cron_mod
    cron.add_user_job(
        user_id="alice",
        job_id="job_d",
        func_name="run_alert_scan",
        cron_expr="*/10 * * * *",
    )
    registry["opened"] = registry["closed"] = 0

    assert cron.add_user_job(
        user_id="bob",
        job_id="job_d",
        func_name="run_paper_settle",
        cron_expr="0 3 * * *",
    ) is False

    assert registry["closed"] == registry["opened"], (
        f"被拒路径泄漏连接：opened={registry['opened']} closed={registry['closed']}"
    )


def test_wrap_with_log_closes_connections(cron_mod):
    """_wrap_with_log 的 insert + 收尾 update 两条连接"""
    cron, registry, _ = cron_mod
    registry["opened"] = registry["closed"] = 0

    wrapped = cron._wrap_with_log("probe_job", lambda: 42)
    assert wrapped() == 42

    assert registry["closed"] == registry["opened"], (
        f"_wrap_with_log 泄漏连接：opened={registry['opened']} closed={registry['closed']}"
    )


def test_wrap_with_log_closes_connections_on_failure(cron_mod):
    """失败路径的收尾 update 同样要关闭"""
    cron, registry, _ = cron_mod
    registry["opened"] = registry["closed"] = 0

    def boom():
        raise RuntimeError("boom")

    wrapped = cron._wrap_with_log("probe_job_fail", boom)
    with pytest.raises(RuntimeError):
        wrapped()

    assert registry["closed"] == registry["opened"], (
        f"_wrap_with_log 失败路径泄漏连接：opened={registry['opened']} closed={registry['closed']}"
    )
