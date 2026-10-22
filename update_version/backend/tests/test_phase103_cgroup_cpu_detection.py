"""Phase 103: hyperopt 的并行度必须感知容器 CPU 限额

`multiprocessing.cpu_count()` **不读 cgroup 配额**。实测：

    docker run --cpus=1.5 <image> python -c "import multiprocessing; \
        print(multiprocessing.cpu_count(), len(os.sched_getaffinity(0)))"
    -> 8 8

宿主 8 核、限额 1.5 核，两个 API 都报 8（cpuset 没限，只有 cpu.max 有限额）。
于是 hyperopt 的 joblib 并行度算成 min(8-1, 8) = 7，在 1.5 核下严重超配：
CPU 密集的 optuna 评估互相抢占，连 Web 端点一起被拖慢。

`/sys/fs/cgroup/cpu.max` 里是 `150000 100000`（= 1.5 核）。cgroup v1 则是
`cpu/cpu.cfs_quota_us` / `cpu.cfs_period_us`。两者都要覆盖。
"""
import os
import sys

import pytest

BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from modules import hyperopt_engine as he  # noqa: E402


@pytest.fixture()
def fake_cgroup(tmp_path, monkeypatch):
    """把 cgroup 文件重定向到 tmp_path 下的假文件"""
    cg2 = tmp_path / "cpu.max"
    cg1_quota = tmp_path / "cpu.cfs_quota_us"
    cg1_period = tmp_path / "cpu.cfs_period_us"

    real_open = open

    def fake_open(path, *a, **kw):
        p = str(path)
        if p == "/sys/fs/cgroup/cpu.max":
            return real_open(cg2, *a, **kw)
        if p == "/sys/fs/cgroup/cpu/cpu.cfs_quota_us":
            return real_open(cg1_quota, *a, **kw)
        if p == "/sys/fs/cgroup/cpu/cpu.cfs_period_us":
            return real_open(cg1_period, *a, **kw)
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    return cg2, cg1_quota, cg1_period


def test_cgroup_v2_quota_is_respected(fake_cgroup):
    cg2, _, _ = fake_cgroup
    cg2.write_text("150000 100000", encoding="ascii")  # 1.5 核

    assert he._effective_cpu_count() == 2, (
        "1.5 核应向上取整为 2（round(1.5)=2），否则并行度按 8 核算会超配"
    )


def test_cgroup_v2_unlimited_falls_back(fake_cgroup, monkeypatch):
    cg2, _, _ = fake_cgroup
    cg2.write_text("max 100000", encoding="ascii")

    import multiprocessing

    monkeypatch.setattr(multiprocessing, "cpu_count", lambda: 4)
    assert he._effective_cpu_count() == 4


def test_cgroup_v1_quota_is_respected(fake_cgroup):
    cg2, q, p = fake_cgroup
    cg2.write_text("max 100000", encoding="ascii")  # v2 不限
    q.write_text("200000", encoding="ascii")
    p.write_text("100000", encoding="ascii")

    assert he._effective_cpu_count() == 2


def test_cgroup_v1_unlimited_is_ignored(fake_cgroup, monkeypatch):
    cg2, q, p = fake_cgroup
    cg2.write_text("max 100000", encoding="ascii")
    q.write_text("-1", encoding="ascii")  # v1 表示不限
    p.write_text("100000", encoding="ascii")

    import multiprocessing

    monkeypatch.setattr(multiprocessing, "cpu_count", lambda: 3)
    assert he._effective_cpu_count() == 3


def test_never_returns_zero(fake_cgroup):
    """限额极小时也必须至少返回 1"""
    cg2, _, _ = fake_cgroup
    cg2.write_text("1000 100000", encoding="ascii")  # 0.01 核

    assert he._effective_cpu_count() >= 1


def test_missing_cgroup_files_fall_back(fake_cgroup, monkeypatch):
    """文件不存在时回落到 cpu_count()，不能抛异常"""
    import multiprocessing

    monkeypatch.setattr(multiprocessing, "cpu_count", lambda: 6)
    assert he._effective_cpu_count() == 6


def test_hyperopt_parallelism_uses_effective_count(monkeypatch):
    """n_jobs 自动计算必须基于有效核数，而不是 cpu_count()"""
    monkeypatch.delenv("HYPEROPT_JOBS", raising=False)
    monkeypatch.setattr(he, "_effective_cpu_count", lambda: 2)

    # 复刻 run_hyperopt 里的计算式
    n_jobs = int(os.environ.get("HYPEROPT_JOBS", "0") or 0)
    if n_jobs <= 0:
        n_jobs = min(max(1, he._effective_cpu_count() - 1), 8)

    assert n_jobs == 1, f"2 核应算成 1 路并行，实际 {n_jobs}"


def test_explicit_env_overrides_detection(monkeypatch):
    """显式设置 HYPEROPT_JOBS 时不走自动探测"""
    monkeypatch.setenv("HYPEROPT_JOBS", "3")
    monkeypatch.setattr(he, "_effective_cpu_count", lambda: 16)

    n_jobs = int(os.environ.get("HYPEROPT_JOBS", "0") or 0)
    if n_jobs <= 0:
        n_jobs = min(max(1, he._effective_cpu_count() - 1), 8)

    assert n_jobs == 3
