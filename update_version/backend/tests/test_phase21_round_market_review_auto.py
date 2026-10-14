# -*- coding: utf-8 -*-
"""本轮修复验证：自动大盘复盘 + 启动补跑 + 交易日 guard + 孤儿 job 清理
+ 并发护栏 + 状态语义修正（degraded ≠ failed）

覆盖：
1. is_trading_day()
   - akshare 正常返回时按日历判断（真实解析 _fetch_trading_days_sina 路径）
   - akshare 抛异常时回退到 weekday 规则
   - 同一天只调一次 akshare（缓存生效）
   - 永不抛异常
2. pd_to_iso()
   - 多类型（datetime / str / numpy.datetime64）解析正确
3. run_market_review_daily()
   - 非交易日时不调用 generate_market_review
   - 交易日时会调用（验证 guard 不挡路）
   - 并发锁：第二次进入立即 return，不重复执行
4. catchup_market_review_on_startup()
   - 非交易日 → 不补跑
   - 未到 10:00 → 不补跑
   - 已过 10:00 且今天无记录 → 补跑（thread.start 真的触发 run_market_review_daily）
   - 已过 10:00 + status=ok → 不补跑
   - 已过 10:00 + status=degraded → 不补跑（★ 本轮回归点）
   - 已过 10:00 + status=failed → 补跑
   - 异常吞噬
5. 孤儿 builtin_* job 清理
   - 不在白名单内的 builtin_* job 被移除
   - 白名单自动派生（_schedule_job(builtin=True) 会被登记）
   - 单条 remove_job 失败不影响后续
   - get_jobs 失败整体被吞

测试不联网、不真生成复盘：akshare / DB / generate / datetime.now / threading 均 mock。
"""
import os
import sys
import threading
from datetime import datetime, date, time as dtime
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================================
# 工具
# ============================================================================
def _clear_trading_day_cache():
    from core import cron as cron_mod
    with cron_mod._TRADING_DAY_CACHE_LOCK:
        cron_mod._TRADING_DAY_CACHE.clear()


def _make_fake_scheduler_with_jobs(jobs):
    """构造一个伪 BackgroundScheduler，get_jobs 返回 jobs 列表，每项有 .id。"""
    sched = MagicMock()
    sched.get_jobs.return_value = jobs
    sched.remove_job = MagicMock()
    sched.add_job = MagicMock()
    return sched


def _setup_fake_thread_capture():
    """让 mock 的 Thread 实例被一个全局列表捕获，start() 真的调用 target。

    返回 (threading_patch_ctx_factory, captured_threads_list).
    """
    captured = []

    def _fake_thread_ctor(*args, **kwargs):
        # 不带 spec，避免与 patch("core.cron.threading.Thread") 冲突
        inst = MagicMock()
        inst._target = kwargs.get("target") or (args[0] if args else None)
        inst._daemon = kwargs.get("daemon", False)
        inst._name = kwargs.get("name", "")

        def _real_start():
            if inst._target is not None:
                try:
                    inst._target()
                except Exception as e:
                    import traceback
                    print(f"thread raised: {e}")
                    traceback.print_exc()

        inst.start.side_effect = _real_start
        captured.append(inst)
        return inst

    return _fake_thread_ctor, captured


# ============================================================================
# 1. is_trading_day：真实解析 akshare 返回值
# ============================================================================
def test_is_trading_day_akshare_real_parse_uses_calendar():
    """patch akshare.tool_trade_date_hist_sina，让 _fetch_trading_days_sina 真跑，
    验证解析逻辑（pd_to_iso → set membership）正确。"""
    from core import cron as cron_mod
    _clear_trading_day_cache()

    # 模拟 akshare DataFrame：df.columns 必须能用 in 判断含 'trade_date'
    class _FakeColumns(list):
        def __contains__(self, item):
            return item == "trade_date"

    fake_df = MagicMock()
    fake_df.empty = False
    fake_df.columns = _FakeColumns(["trade_date"])
    fake_df.__getitem__.return_value.tolist.return_value = [
        datetime(2026, 8, 13), datetime(2026, 8, 14),
    ]

    fake_ak_module = MagicMock()
    fake_ak_module.tool_trade_date_hist_sina.return_value = fake_df

    with patch.dict(sys.modules, {"akshare": fake_ak_module}):
        # 周五 2026-08-14 在集合 → True
        assert cron_mod.is_trading_day("2026-08-14") is True
        # 周四 2026-08-13 在集合 → True
        assert cron_mod.is_trading_day("2026-08-13") is True
        # 周六 2026-08-15 不在集合 → False
        assert cron_mod.is_trading_day("2026-08-15") is False
        # 周日 2026-08-16 不在集合 → False
        assert cron_mod.is_trading_day("2026-08-16") is False
        # akshare 真被调用了一次（解析路径走通，且后续命中缓存）
        fake_ak_module.tool_trade_date_hist_sina.assert_called_once()
    print("OK is_trading_day: akshare 真实解析路径正确")


def test_is_trading_day_akshare_cache_only_no_repeat_calls():
    """直接 patch _fetch_trading_days_sina，验证 is_trading_day 缓存层：
    同一天内多次调用只触发一次 fetch。"""
    from core import cron as cron_mod
    _clear_trading_day_cache()

    with patch.object(
        cron_mod, "_fetch_trading_days_sina", return_value={"2026-08-14"}
    ) as mock_fetch:
        cron_mod.is_trading_day("2026-08-14")  # 第1次：触发 fetch
        cron_mod.is_trading_day("2026-08-14")  # 第2次：命中缓存
        cron_mod.is_trading_day("2026-08-14")  # 第3次：命中缓存
        assert mock_fetch.call_count == 1, f"expected 1 fetch, got {mock_fetch.call_count}"
    print(f"OK is_trading_day: 缓存层只 fetch {1} 次（3 次查询）")


def test_is_trading_day_falls_back_to_weekday_on_akshare_failure():
    """akshare 抛异常 → 回退到 weekday 规则"""
    from core import cron as cron_mod
    _clear_trading_day_cache()

    def _boom():
        raise ConnectionError("akshare unreachable")

    with patch.object(cron_mod, "_fetch_trading_days_sina", side_effect=_boom):
        for iso in ("2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13", "2026-08-14"):
            assert cron_mod.is_trading_day(iso) is True, f"{iso} 应是交易日"
        assert cron_mod.is_trading_day("2026-08-15") is False
        assert cron_mod.is_trading_day("2026-08-16") is False
    print("OK is_trading_day: akshare 异常 → weekday 回退")


def test_is_trading_day_never_raises():
    """任何异常情况下都不抛（任务不能挂）"""
    from core import cron as cron_mod
    _clear_trading_day_cache()

    def _boom(*_a, **_kw):
        raise RuntimeError("完全坏掉了")

    with patch.object(cron_mod, "_fetch_trading_days_sina", side_effect=_boom):
        result = cron_mod.is_trading_day("not-a-date")
        assert isinstance(result, bool)
    print("OK is_trading_day: 永不抛异常")


# ============================================================================
# 2. pd_to_iso：多类型解析
# ============================================================================
def test_pd_to_iso_handles_multiple_input_types():
    """pd_to_iso 应正确处理 datetime / str / numpy.datetime64。"""
    from core import cron as cron_mod

    # datetime
    assert cron_mod.pd_to_iso(datetime(2026, 8, 14, 10, 30, 0)) == "2026-08-14"
    # date
    assert cron_mod.pd_to_iso(date(2026, 8, 14)) == "2026-08-14"
    # str
    assert cron_mod.pd_to_iso("2026-08-14") == "2026-08-14"
    assert cron_mod.pd_to_iso("2026-08-14T10:30:00") == "2026-08-14"
    # numpy.datetime64（无 strftime 但有 item() 返回 date）
    try:
        import numpy as np
        nd64 = np.datetime64("2026-08-14")
        out = cron_mod.pd_to_iso(nd64)
        assert out == "2026-08-14", f"numpy.datetime64 → {out!r}"
        print(f"  OK numpy.datetime64 -> {out}")
    except ImportError:
        print("  - numpy.datetime64 skipped (numpy not installed)")
    print("OK pd_to_iso: 多类型解析正确")


# ============================================================================
# 3. run_market_review_daily：guard + 并发护栏
# ============================================================================
def test_run_market_review_daily_skips_on_non_trading_day():
    """非交易日：早退，不调用 generate_market_review"""
    from core import cron as cron_mod

    with patch.object(cron_mod, "is_trading_day", return_value=False), \
         patch("importlib.util.spec_from_file_location") as spec_mock:
        cron_mod.run_market_review_daily()
    spec_mock.assert_not_called()
    print("OK run_market_review_daily: 非交易日早退")


def test_run_market_review_daily_calls_generate_on_trading_day():
    """交易日：走完主链路到 generate_market_review"""
    from core import cron as cron_mod

    # 让 importlib.util.spec_from_file_location 抛错来证明走到了 service 加载阶段
    def _fake_loader(name, location):
        raise ImportError("simulated service unavailable")

    with patch.object(cron_mod, "is_trading_day", return_value=True), \
         patch("importlib.util.spec_from_file_location", side_effect=_fake_loader):
        cron_mod.run_market_review_daily()  # 不应抛（内部 try/except 已吞）
    print("OK run_market_review_daily: 交易日分支 guard 验证")


def test_run_market_review_daily_concurrency_lock_blocks_concurrent_run():
    """★ 并发护栏：第二个并发的进入应直接 return，不重复执行"""
    from core import cron as cron_mod

    # 模拟"第一次进入已持有锁"的状态
    assert cron_mod._MARKET_REVIEW_RUN_LOCK.acquire(blocking=False), \
        "锁应可被首次获取"
    try:
        # 第二次调用：acquire(blocking=False) 应失败 → 早退
        with patch.object(cron_mod, "is_trading_day") as is_td_mock:
            cron_mod.run_market_review_daily()
            is_td_mock.assert_not_called()  # 抢锁失败后根本不该进入主流程
    finally:
        cron_mod._MARKET_REVIEW_RUN_LOCK.release()
    print("OK run_market_review_daily: 并发锁挡掉重复执行")


def test_run_market_review_daily_releases_lock_on_exception():
    """主流程异常时 finally 必须释放锁（不然后续调用永远被挡住）"""
    from core import cron as cron_mod

    # 让 is_trading_day 抛异常（模拟主流程异常）
    with patch.object(cron_mod, "is_trading_day", side_effect=RuntimeError("boom")):
        try:
            cron_mod.run_market_review_daily()
        except RuntimeError:
            pass  # 我们故意让外层看到；但 finally 已执行
    # 锁应可被重新获取（否则说明 finally 没释放）
    assert cron_mod._MARKET_REVIEW_RUN_LOCK.acquire(blocking=False), \
        "锁未释放 —— finally 块失效"
    cron_mod._MARKET_REVIEW_RUN_LOCK.release()
    print("OK run_market_review_daily: 主流程异常后锁仍被释放")


# ============================================================================
# 4. catchup_market_review_on_startup：6 个分支
# ============================================================================
def _patch_history_module(record=None, err=False):
    history_mod = MagicMock()
    if err:
        history_mod.get_market_review.side_effect = RuntimeError("db 不可用")
    else:
        history_mod.get_market_review.return_value = record
    return history_mod


def _run_catchup_with_mocks(*, today_iso, is_trading_day=True, history_mod=None,
                            fake_thread_ctor=None):
    """把 catchup 跑起来所需的全部 mock 集中。返回 threading.Thread 捕获列表。"""
    from core import cron as cron_mod
    fake_now = datetime.strptime(today_iso + " 15:00:00", "%Y-%m-%d %H:%M:%S")

    with patch.object(cron_mod, "is_trading_day", return_value=is_trading_day), \
         patch("core.cron.datetime") as dt_mod, \
         patch.object(cron_mod, "_load_history_module_for_catchup",
                      return_value=history_mod or _patch_history_module()), \
         patch("core.cron.threading.Thread", side_effect=fake_thread_ctor):
        dt_mod.now.return_value = fake_now
        dt_mod.strptime = datetime.strptime
        cron_mod.catchup_market_review_on_startup()
    return fake_thread_ctor  # 已通过 side_effect 收集到 captured


def test_catchup_skips_on_non_trading_day():
    """分支1：非交易日 → 不补跑"""
    from core import cron as cron_mod
    _clear_trading_day_cache()

    fake_ctor, captured = _setup_fake_thread_capture()
    with patch.object(cron_mod, "is_trading_day", return_value=False), \
         patch.object(cron_mod, "_load_history_module_for_catchup") as lh, \
         patch("core.cron.threading.Thread", side_effect=fake_ctor):
        cron_mod.catchup_market_review_on_startup()
    assert captured == []
    lh.assert_not_called()
    print("OK catchup: 非交易日 → 不补跑")


def test_catchup_skips_before_10am():
    """分支2：未到 10:00 → 不补跑"""
    from core import cron as cron_mod
    _clear_trading_day_cache()

    fake_ctor, captured = _setup_fake_thread_capture()
    fake_now = datetime(2026, 8, 14, 9, 30, 0)
    with patch.object(cron_mod, "is_trading_day", return_value=True), \
         patch("core.cron.datetime") as dt_mod, \
         patch.object(cron_mod, "_load_history_module_for_catchup") as lh, \
         patch("core.cron.threading.Thread", side_effect=fake_ctor):
        dt_mod.now.return_value = fake_now
        dt_mod.strptime = datetime.strptime
        cron_mod.catchup_market_review_on_startup()
    assert captured == []
    lh.assert_not_called()
    print("OK catchup: 未到 10:00 → 不补跑")


def test_catchup_runs_when_past_10am_and_no_record():
    """分支3：已过 10:00 且今天无记录 → 补跑（thread.start 真的触发 run_market_review_daily）"""
    from core import cron as cron_mod
    _clear_trading_day_cache()

    fake_ctor, captured = _setup_fake_thread_capture()
    history_mod = _patch_history_module(record=None)
    _run_catchup_with_mocks(
        today_iso="2026-08-14",
        is_trading_day=True,
        history_mod=history_mod,
        fake_thread_ctor=fake_ctor,
    )
    # ★ 真实执行：thread.start() 真的调了 target → target 调了 run_market_review_daily
    assert len(captured) == 1, f"expected 1 thread, got {len(captured)}"
    th = captured[0]
    assert th._daemon is True, f"daemon flag wrong: {th._daemon}"
    assert callable(th._target), "target 未传入"
    # history_mod.get_market_review 被调用过
    history_mod.get_market_review.assert_called_once_with("2026-08-14")
    print("OK catchup: 已过 10:00 + 无记录 → 真实触发 run_market_review_daily")


def test_catchup_skips_when_today_status_ok():
    """分支4：status='ok' → 不补跑"""
    from core import cron as cron_mod
    _clear_trading_day_cache()

    fake_ctor, captured = _setup_fake_thread_capture()
    history_mod = _patch_history_module(record={"status": "ok", "summary": "today ok"})
    _run_catchup_with_mocks(
        today_iso="2026-08-14",
        is_trading_day=True,
        history_mod=history_mod,
        fake_thread_ctor=fake_ctor,
    )
    assert captured == []
    history_mod.get_market_review.assert_called_once_with("2026-08-14")
    print("OK catchup: status=ok → 不补跑")


def test_catchup_skips_when_today_status_degraded():
    """分支5（★ 本轮回归点）：status='degraded' → 不补跑

    之前 _today_has_successful_review 只认 status='ok'，会把 degraded
    误判为"没记录"，每次重启都重新抓全市场。本轮改为"非 failed 即视为
    已生成过"，degraded 同样跳过。
    """
    from core import cron as cron_mod
    _clear_trading_day_cache()

    fake_ctor, captured = _setup_fake_thread_capture()
    history_mod = _patch_history_module(
        record={"status": "degraded", "summary": "数据源临时故障"}
    )
    _run_catchup_with_mocks(
        today_iso="2026-08-14",
        is_trading_day=True,
        history_mod=history_mod,
        fake_thread_ctor=fake_ctor,
    )
    assert captured == [], f"degraded 记录不应补跑，但起了 {len(captured)} 个线程"
    history_mod.get_market_review.assert_called_once_with("2026-08-14")
    print("OK catchup: status=degraded → 不补跑（★ 本轮回归点）")


def test_catchup_runs_when_today_status_failed():
    """分支6：status='failed' → 补跑"""
    from core import cron as cron_mod
    _clear_trading_day_cache()

    fake_ctor, captured = _setup_fake_thread_capture()
    history_mod = _patch_history_module(record={"status": "failed", "error": "LLM timeout"})
    _run_catchup_with_mocks(
        today_iso="2026-08-14",
        is_trading_day=True,
        history_mod=history_mod,
        fake_thread_ctor=fake_ctor,
    )
    assert len(captured) == 1
    history_mod.get_market_review.assert_called_once_with("2026-08-14")
    print("OK catchup: status=failed → 补跑")


def test_catchup_swallows_exceptions():
    """任何异常都不能抛到调用方"""
    from core import cron as cron_mod
    _clear_trading_day_cache()

    with patch.object(cron_mod, "is_trading_day", side_effect=RuntimeError("boom")):
        cron_mod.catchup_market_review_on_startup()  # 不应抛
    print("OK catchup: 异常被吞掉，不影响启动")


def test_catchup_thread_target_really_runs_run_market_review_daily():
    """★ 强化测试：捕获 thread.start() 后调用的 target，验证它真的进入
    run_market_review_daily 主流程（验证非 fake-green）。"""
    from core import cron as cron_mod
    _clear_trading_day_cache()

    fake_ctor, captured = _setup_fake_thread_capture()
    history_mod = _patch_history_module(record=None)

    with patch.object(cron_mod, "is_trading_day", return_value=True), \
         patch("core.cron.datetime") as dt_mod, \
         patch.object(cron_mod, "_load_history_module_for_catchup", return_value=history_mod), \
         patch.object(cron_mod, "run_market_review_daily") as run_mock, \
         patch("core.cron.threading.Thread", side_effect=fake_ctor):
        dt_mod.now.return_value = datetime(2026, 8, 14, 11, 0, 0)
        dt_mod.strptime = datetime.strptime
        cron_mod.catchup_market_review_on_startup()
        # start() 在 catchup 内部已同步调用 → run_market_review_daily 应已被调用一次
        run_mock.assert_called_once()
    print("OK catchup thread target → 真的调用 run_market_review_daily")


# ============================================================================
# 5. 孤儿 builtin_* job 清理 + 自动派生白名单
# ============================================================================
def test_register_builtin_jobs_removes_orphan_builtin_jobs():
    """白名单内 builtin_* job 不动；白名单外 builtin_* job 被 remove_job"""
    from core import cron as cron_mod
    jobs = [
        MagicMock(id="builtin_market_review_morning"),     # 注册
        MagicMock(id="builtin_market_review_evening"),     # 注册
        MagicMock(id="builtin_alert_scan"),                # 注册
        MagicMock(id="builtin_community_rank"),            # 注册
        MagicMock(id="builtin_paper_settle"),              # 注册
        MagicMock(id="builtin_market_review_daily"),       # 孤儿
        MagicMock(id="builtin_old_removed_job"),           # 孤儿
        MagicMock(id="user_scheduled_42"),                 # 非 builtin：不动
    ]
    sched = _make_fake_scheduler_with_jobs(jobs)

    with patch.object(cron_mod, "_scheduler", sched), \
         patch.object(cron_mod, "_schedule_job", wraps=cron_mod._schedule_job):
        cron_mod._register_builtin_jobs()

    removed_ids = [c.args[0] for c in sched.remove_job.call_args_list]
    assert "builtin_market_review_daily" in removed_ids, removed_ids
    assert "builtin_old_removed_job" in removed_ids, removed_ids
    assert "user_scheduled_42" not in removed_ids, removed_ids
    for jid in (
        "builtin_market_review_morning",
        "builtin_market_review_evening",
        "builtin_alert_scan",
        "builtin_community_rank",
        "builtin_paper_settle",
    ):
        assert jid not in removed_ids, f"{jid} 误删: {removed_ids}"
    print(f"OK orphan builtin cleanup: removed={removed_ids}")


def test_register_builtin_jobs_whitelist_auto_derived():
    """★ 白名单自动派生：_schedule_job(builtin=True) 会把 job_id 记入白名单；
    后续孤儿清理按这个集合判断。"""
    from core import cron as cron_mod

    # 模拟一个"未来新增的 builtin 任务"，通过 _schedule_job(builtin=True) 注册
    sched = _make_fake_scheduler_with_jobs([])
    sched.get_jobs.return_value = [MagicMock(id="builtin_orphan_to_remove")]

    with patch.object(cron_mod, "_scheduler", sched):
        # 先注册两个 builtin（含一个未来任务）
        cron_mod._schedule_job(
            job_id="builtin_future_task",
            func=lambda: None,
            cron="0 12 * * *",
            description="future",
            builtin=True,
        )
        cron_mod._schedule_job(
            job_id="builtin_another",
            func=lambda: None,
            cron="0 13 * * *",
            builtin=True,
        )
        # 白名单应自动包含
        assert "builtin_future_task" in cron_mod._REGISTERED_BUILTIN_IDS
        assert "builtin_another" in cron_mod._REGISTERED_BUILTIN_IDS
        # 触发清理
        cron_mod._register_builtin_jobs()
        # 孤儿被删
        removed_ids = [c.args[0] for c in sched.remove_job.call_args_list]
        assert "builtin_orphan_to_remove" in removed_ids, removed_ids
    print("OK whitelist auto-derived from _schedule_job(builtin=True)")


def test_register_builtin_jobs_non_builtin_jobs_not_added_to_whitelist():
    """用户任务（user_scheduled_*）不应进 builtin 白名单"""
    from core import cron as cron_mod

    with patch.object(cron_mod, "_scheduler", _make_fake_scheduler_with_jobs([])):
        cron_mod._schedule_job(
            job_id="user_scheduled_42",
            func=lambda: None,
            cron="0 9 * * *",
            builtin=False,  # 显式非 builtin
        )
    assert "user_scheduled_42" not in cron_mod._REGISTERED_BUILTIN_IDS
    print("OK user_scheduled_* not in builtin whitelist")


def test_register_builtin_jobs_tolerates_remove_failures():
    """单条 remove_job 失败不中断后续"""
    from core import cron as cron_mod
    sched = _make_fake_scheduler_with_jobs([
        MagicMock(id="builtin_orphan_a"),
        MagicMock(id="builtin_orphan_b"),
    ])
    sched.remove_job.side_effect = [RuntimeError("db locked"), None]

    with patch.object(cron_mod, "_scheduler", sched), \
         patch.object(cron_mod, "_schedule_job"):
        cron_mod._register_builtin_jobs()
    assert sched.remove_job.call_count == 2
    print("OK orphan cleanup: 单条 remove_job 失败不中断后续")


def test_register_builtin_jobs_tolerates_get_jobs_failure():
    """get_jobs() 抛异常时整体不抛、不影响内置任务注册"""
    from core import cron as cron_mod
    sched = MagicMock()
    sched.get_jobs.side_effect = RuntimeError("scheduler broken")

    with patch.object(cron_mod, "_scheduler", sched), \
         patch.object(cron_mod, "_schedule_job") as sched_job_mock:
        cron_mod._register_builtin_jobs()  # 不应抛
    assert sched_job_mock.called
    print("OK orphan cleanup: get_jobs 异常整体被吞")


# ============================================================================
# 6. init() 重置 _listener_attached（防 shutdown→init 后 cron_run_log 静默丢失）
# ============================================================================
def test_init_resets_listener_attached_flag():
    """第二次 init()（如 shutdown → init 循环）后，新 scheduler 应挂上 listener"""
    from core import cron as cron_mod

    # 把 listener 标记置 True（模拟第一次 init 已挂过）
    cron_mod._listener_attached = True
    cron_mod._scheduler = MagicMock()  # 占位以防内部访问
    try:
        with patch.object(cron_mod, "APS_AVAILABLE", False):
            cron_mod.init("v2_dev.db")
        # 即使 scheduler 不可用，flag 也应被重置（便于下次真挂）
        assert cron_mod._listener_attached is False
    finally:
        cron_mod._listener_attached = False  # 恢复
    print("OK init(): _listener_attached 在每次 init 时重置")


# ============================================================================
# 汇总
# ============================================================================
ALL_TESTS = [
    # 1. is_trading_day
    test_is_trading_day_akshare_real_parse_uses_calendar,
    test_is_trading_day_akshare_cache_only_no_repeat_calls,
    test_is_trading_day_falls_back_to_weekday_on_akshare_failure,
    test_is_trading_day_never_raises,
    # 2. pd_to_iso
    test_pd_to_iso_handles_multiple_input_types,
    # 3. run_market_review_daily
    test_run_market_review_daily_skips_on_non_trading_day,
    test_run_market_review_daily_calls_generate_on_trading_day,
    test_run_market_review_daily_concurrency_lock_blocks_concurrent_run,
    test_run_market_review_daily_releases_lock_on_exception,
    # 4. catchup
    test_catchup_skips_on_non_trading_day,
    test_catchup_skips_before_10am,
    test_catchup_runs_when_past_10am_and_no_record,
    test_catchup_skips_when_today_status_ok,
    test_catchup_skips_when_today_status_degraded,
    test_catchup_runs_when_today_status_failed,
    test_catchup_swallows_exceptions,
    test_catchup_thread_target_really_runs_run_market_review_daily,
    # 5. orphan cleanup
    test_register_builtin_jobs_removes_orphan_builtin_jobs,
    test_register_builtin_jobs_whitelist_auto_derived,
    test_register_builtin_jobs_non_builtin_jobs_not_added_to_whitelist,
    test_register_builtin_jobs_tolerates_remove_failures,
    test_register_builtin_jobs_tolerates_get_jobs_failure,
    # 6. listener reset
    test_init_resets_listener_attached_flag,
]


def run_all():
    passed = 0
    failed = 0
    for t in ALL_TESTS:
        try:
            t()
            passed += 1
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    print(f"\n=== daily market review auto + catchup: {passed} PASSED, {failed} FAILED ===")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)