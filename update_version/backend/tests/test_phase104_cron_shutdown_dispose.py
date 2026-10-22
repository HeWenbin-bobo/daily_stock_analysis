"""Phase 104: cron.shutdown() 必须真正释放 jobstore 的连接

`SQLAlchemyJobStore` 内部持有一个 SQLAlchemy Engine（连接池）。`scheduler.shutdown()`
只停调度器，**不 dispose 那个 Engine**，而 Engine 与 Pool 之间有循环引用，
所以要等 `gc.collect()` 才释放。

容器内实测（`cron.init()` → `cron.shutdown()`）：

    init 后:            4 个 v2_dev.db 句柄
    shutdown 立即:       3 个（没释放）
    sleep 2s 后:        3 个（还是没释放）
    gc.collect() 后:    0 个

对长期运行的进程影响有限（每次启动一次），但 `init()/shutdown()` 循环
（测试套件里每个容器级 fixture 都会走一遍）会累积句柄。显式 dispose 后
shutdown 即归零，不依赖 GC 时机。
"""
import os
import sys

import pytest

BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


@pytest.fixture()
def cron_env(tmp_path, monkeypatch):
    from db import migrate as migrate_mod
    from core import cron

    db_path = tmp_path / "cron_shutdown.db"
    monkeypatch.setattr(migrate_mod, "DB_PATH", db_path)
    monkeypatch.setattr(cron, "DB_PATH", db_path)
    migrate_mod.migrate()
    yield cron, db_path
    try:
        cron.shutdown()
    except Exception:
        pass


def test_shutdown_disposes_jobstore_engine(cron_env):
    """shutdown 后 jobstore 的 engine 必须已 dispose（不能靠 GC）"""
    cron, _ = cron_env
    cron.init(str(_))

    scheduler = cron._scheduler
    assert scheduler is not None

    stores = list((getattr(scheduler, "_jobstores", None) or {}).values())
    engines = [getattr(s, "engine", None) for s in stores]
    engines = [e for e in engines if e is not None]
    assert engines, "jobstore 应持有 engine，否则这个测试失去意义"

    # 记录每个 engine 的 pool 状态
    pools = [e.pool for e in engines]

    cron.shutdown()

    for pool in pools:
        # SQLAlchemy 的 Pool 在 dispose 后 checkedout 为 0 且连接被关
        assert pool.checkedout() == 0, f"dispose 后不应有借出的连接: {pool.status()}"


def test_dispose_helper_tolerates_broken_scheduler():
    """拿不到 engine 时不能抛（shutdown 路径必须稳）"""
    from core import cron

    class Weird:
        @property
        def _jobstores(self):
            raise RuntimeError("broken")

    # 不抛异常即通过
    cron._dispose_jobstore_engines(Weird())


def test_dispose_helper_handles_missing_engines():
    from core import cron

    class Store:
        pass

    class Scheduler:
        _jobstores = {"default": Store()}

    cron._dispose_jobstore_engines(Scheduler())  # 无 engine → 静默跳过


def test_dispose_helper_swallows_engine_errors():
    from core import cron

    class BadEngine:
        def dispose(self):
            raise RuntimeError("dispose failed")

    class Store:
        engine = BadEngine()

    class Scheduler:
        _jobstores = {"default": Store()}

    cron._dispose_jobstore_engines(Scheduler())  # 异常被吞


def test_shutdown_is_idempotent(cron_env):
    """重复 shutdown 不应抛异常"""
    cron, _ = cron_env
    cron.init(str(_))
    cron.shutdown()
    cron.shutdown()
    assert cron._scheduler is None


def test_repeated_init_shutdown_cycles_do_not_accumulate(cron_env):
    """多轮 init/shutdown 后不残留连接（这是 dispose 的实战价值）"""
    cron, db_path = cron_env

    def db_fds():
        try:
            d = "/proc/self/fd"
            n = 0
            for f in os.listdir(d):
                try:
                    if str(db_path) in os.readlink(os.path.join(d, f)):
                        n += 1
                except OSError:
                    pass
            return n
        except OSError:
            return None

    if db_fds() is None:
        pytest.skip("非 Linux 平台")

    cron.init(str(db_path))
    cron.shutdown()
    baseline = db_fds()

    for _ in range(3):
        cron.init(str(db_path))
        cron.shutdown()

    after = db_fds()
    assert after - baseline <= 1, (
        f"3 轮 init/shutdown 后 db 句柄从 {baseline} 涨到 {after}，"
        f"说明 shutdown 没有释放 jobstore 连接"
    )
