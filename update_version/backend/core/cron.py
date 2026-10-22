# -*- coding: utf-8 -*-
"""
APScheduler 持久化 cron 调度器
- 替代之前 /latest 那种"用户打开页面才跑"的模式
- 任务持久化到 SQLite（重启不丢）
- 内置错峰：避免任务堆在同一时刻
- 支持 manual_run / pause / resume
"""
import json
import sys
import time
import logging
import asyncio
import threading
from datetime import datetime, timedelta, time as dtime
from typing import Optional, Callable, Dict, Any, List, Set

logger = logging.getLogger(__name__)

# 尝试 import APScheduler
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
    APS_AVAILABLE = True
except Exception as _aps_err:  # 可能是 sqlalchemy 缺失或其他依赖问题，捕获一切以利排查
    APS_AVAILABLE = False
    logger.error(
        "APScheduler 未就绪 (%s: %s)；所有定时任务（含每日大盘复盘）将不会执行",
        type(_aps_err).__name__, _aps_err,
    )

from db.migrate import get_conn


_scheduler: Optional[Any] = None
DB_PATH = None  # 由 init() 注入


# ============================================================================
# 大盘复盘并发护栏：catchup daemon 线程与 APScheduler 触发的 morning job
#   可能同时进入 run_market_review_daily。max_instances=1 只约束
#   APScheduler 自己触发的同一 job，管不住 catchup 线程。
#   用一个非阻塞锁挡住重复执行。
# ============================================================================
_MARKET_REVIEW_RUN_LOCK: threading.Lock = threading.Lock()


# ============================================================================
# 交易日判断（含进程内缓存；akshare 不可用时回退到 weekday 规则）
# ============================================================================
# 缓存结构：_TRADING_DAY_CACHE = {cache_date_iso: value}
#   成功时：cache_date_iso → 全量交易日集合（akshare 一次返回约 8K 行，够覆盖任意 target）
#   失败时：cache_date_iso → _WEEKDAY_FALLBACK 哨兵（当日后续查询直接按 weekday 规则，
#           不再反复重试网络）。两种值都按"自然日"缓存，进程重启会重新拉一次。
_TRADING_DAY_CACHE: Dict[str, Any] = {}
_TRADING_DAY_CACHE_LOCK: threading.Lock = threading.Lock()
# akshare 不可用时的缓存哨兵值：表示"当日已确认 akshare 失败，后续查询走 weekday 规则"
_WEEKDAY_FALLBACK: Any = object()
# ★ 探索期修复(定时任务被挂起的上游拖死)：交易日历拉取硬超时（秒）。
#   akshare 无内部超时，这条链路挡在 10:00 / 18:00 大盘复盘的最前面。
_TRADING_CALENDAR_TIMEOUT = 20.0


def _fetch_trading_days_sina() -> Set[str]:
    """从新浪交易日历拉取所有交易日，返回 set of 'YYYY-MM-DD' 字符串。失败抛异常。

    ★ 探索期修复(定时任务被挂起的上游拖死)：akshare 无内部超时，这个调用在
      `run_market_review_daily`（10:00 / 18:00）和启动补跑线程的最前面。
      一旦 socket 挂住，任务永不返回，APScheduler max_instances=1 会把之后
      每一次触发都判为 "maximum number of running instances reached" 跳过，
      大盘复盘从此不再生成，日志里只有 WARNING。
      封顶后超时按普通失败处理，走已有的 weekday 回退分支。
    """
    from core.data_source import call_with_timeout

    def _fetch():
        import akshare as ak
        return ak.tool_trade_date_hist_sina()

    df = call_with_timeout(_fetch, _TRADING_CALENDAR_TIMEOUT, "tool_trade_date_hist_sina")
    if df is None or df.empty or "trade_date" not in df.columns:
        raise RuntimeError("akshare tool_trade_date_hist_sina returned empty/invalid result")
    # trade_date 可能是 Timestamp 或 str，统一转成 'YYYY-MM-DD'
    out: Set[str] = set()
    for v in df["trade_date"].tolist():
        try:
            out.add(pd_to_iso(v))
        except Exception:
            continue
    if not out:
        raise RuntimeError("akshare trade date list parsed to empty set")
    return out


def pd_to_iso(v: Any) -> str:
    """把 pandas Timestamp / datetime / str / numpy.datetime64 → 'YYYY-MM-DD' 字符串。"""
    # numpy.datetime64 有 item() → datetime
    if hasattr(v, "item") and not hasattr(v, "strftime"):
        try:
            v = v.item()
        except Exception:
            pass
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    if len(s) >= 10:
        return s[:10]
    return s


def _weekday_fallback(d: datetime) -> bool:
    """akshare 不可用时的回退：周一到周五视为交易日。"""
    return d.weekday() < 5


def is_trading_day(date_str: Optional[str] = None) -> bool:
    """判断给定日期（默认今天，本地时间）是否为交易日。

    - 优先用 akshare 新浪交易日历（进程内缓存，每自然日最多取一次网络）。
    - akshare 任何异常 → 回退到 weekday() < 5，并 log warning。
    - 永不抛异常（任务不能因为日历取不到就挂掉）。

    ★ 修复(L6)：akshare 失败时把"weekday 回退结果"也缓存起来（当日有效），
    否则每 5 分钟 cron（异动扫描/复盘补跑）都会重试网络。旧实现失败分支直接
    return，不写缓存 → 每次调用都触发一次网络请求，网络故障期持续空转。

    ★ 本轮修复(L6-regression)：L6 在失败分支缓存的是"只含本次 target 的单元素集合"
    {target}（或空集）——同一天内先查 A 再查 B，第二次直接命中这个残缺集合，
    B 永远不在集合里 → 恒 False。即使 akshare 故障退化为 weekday 规则，一个
    周二被误判为非交易日，会让当天的定时任务（复盘/异动扫描）全部跳过。
    改为失败时缓存哨兵 _WEEKDAY_FALLBACK，命中后当场按 weekday 规则计算，
    不再有"集合只含一个日期"的歧义；成功分支仍然缓存全量交易日集合。

    Args:
        date_str: 'YYYY-MM-DD' 或 None（今天）。

    Returns:
        bool：交易日为 True。
    """
    target = date_str or datetime.now().strftime("%Y-%m-%d")
    cache_key = datetime.now().strftime("%Y-%m-%d")
    # ★ 探索期修复(内存)：缓存 key 是"今天"，但从不淘汰旧 key —— 成功分支存的是
    #   全量交易日集合（≈8K 个字符串，约 500KB），每天留一份，一年就是 ~180MB。
    #   每天只保留当天的 entries，写入前先清掉其它日期。
    with _TRADING_DAY_CACHE_LOCK:
        for stale_key in [k for k in _TRADING_DAY_CACHE if k != cache_key]:
            _TRADING_DAY_CACHE.pop(stale_key, None)
        cached = _TRADING_DAY_CACHE.get(cache_key)
    if cached is not None:
        if cached is _WEEKDAY_FALLBACK:
            try:
                d = datetime.strptime(target, "%Y-%m-%d")
            except Exception:
                d = datetime.now()
            return _weekday_fallback(d)
        return target in cached
    # 尝试拉取（仅当日第一次会真正联网）
    try:
        trading_set = _fetch_trading_days_sina()
    except Exception as e:
        logger.warning(
            "is_trading_day: akshare 取交易日历失败（%s: %s），回退到 weekday 规则",
            type(e).__name__, e,
        )
        try:
            d = datetime.strptime(target, "%Y-%m-%d")
        except Exception:
            d = datetime.now()
        result = _weekday_fallback(d)
        # ★ 本轮修复：缓存哨兵而非"只含 target 的集合"，见函数 docstring。
        with _TRADING_DAY_CACHE_LOCK:
            if _TRADING_DAY_CACHE.get(cache_key) is None:
                _TRADING_DAY_CACHE[cache_key] = _WEEKDAY_FALLBACK
        return result
    with _TRADING_DAY_CACHE_LOCK:
        # 双重检查：拿到锁期间可能其他线程已经写入了
        if _TRADING_DAY_CACHE.get(cache_key) is None:
            _TRADING_DAY_CACHE[cache_key] = trading_set
    return target in trading_set


def init(db_path: str = "v2_dev.db") -> Optional[Any]:
    """初始化调度器（必须在 app 启动时调用一次）"""
    global _scheduler, DB_PATH, _listener_attached
    DB_PATH = db_path
    # ★ 本轮修复：重置 listener 标记，支持 shutdown → init() 循环后挂上新的 listener；
    #   否则新 scheduler 不会写 cron_run_log。
    _listener_attached = False
    if not APS_AVAILABLE:
        logger.error(
            "Cron 调度器不可用（APScheduler 未就绪）；所有定时任务（含每日大盘复盘）将不会执行。"
            " 检查 apscheduler / sqlalchemy 是否安装。"
        )
        return None

    try:
        _scheduler = BackgroundScheduler(
            jobstores={
                "default": SQLAlchemyJobStore(url=f"sqlite:///{db_path}")
            },
            job_defaults={
                "coalesce": True,         # 合并多次错过
                "max_instances": 1,       # 同一任务同一时刻只跑一次
                "misfire_grace_time": 300,  # 5 分钟内错过可补
            },
        )
        _scheduler.start()
        logger.info("Cron scheduler started with db=%s", db_path)

        # 注册内置任务
        _register_builtin_jobs()
        # ★ 探索期修复(重启失效)：重放 cron_user_jobs 里的用户任务。
        #   放在 builtin 之后，对账时才不会把内置任务当成 stale user job 删掉
        #   （对账逻辑里已排除 builtin_/user_scheduled_ 前缀，这里是双保险）。
        try:
            register_user_cron_jobs()
        except Exception as e:
            logger.warning("register_user_cron_jobs failed: %s", e)
        return _scheduler
    except Exception as e:
        logger.exception("Failed to init scheduler: %s", e)
        return None


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        # ★ 探索期修复(fd 不释放)：`shutdown(wait=False)` 只停调度器，
        #   SQLAlchemyJobStore 内部的 Engine 连接池不会跟着释放 —— 实测
        #   shutdown 后仍有 3 个 v2_dev.db 句柄（db + wal + shm 那套），
        #   要等 gc.collect() 才归 0（Engine 与 Pool 之间有循环引用）。
        #
        #   ★ 顺序很关键：必须在 `shutdown()` **之后** dispose。scheduler.shutdown()
        #     自身会写 jobstore 状态，会重新从池里取连接；先 dispose 会被它覆盖
        #     （实测：先 dispose 归 0，shutdown 后又回到 3）。
        scheduler = _scheduler
        try:
            scheduler.shutdown(wait=False)
        finally:
            _dispose_jobstore_engines(scheduler)
            _scheduler = None


def _dispose_jobstore_engines(scheduler) -> None:
    """尽量释放 jobstore 持有的数据库连接池。

    只在能安全拿到 engine 时操作；任何异常都吞掉——shutdown 路径不该因为
    清理失败而抛错（那会让进程关停失败，是更严重的问题）。
    """
    try:
        for store in (getattr(scheduler, "_jobstores", None) or {}).values():
            engine = getattr(store, "engine", None)
            if engine is not None:
                try:
                    engine.dispose()
                except Exception as e:  # noqa: BLE001
                    logger.debug("jobstore engine.dispose failed: %s", e)
    except Exception as e:  # noqa: BLE001
        logger.debug("dispose jobstore engines failed: %s", e)


# ============================================================================
# 内置任务：注册到调度器
# ============================================================================
# 本轮注册的全部 builtin_* job_id（用于清理 jobstore 里的孤儿任务）。
# APScheduler 的 jobstore 不会自动删除改名/删名后的旧任务，所以每次启动
# 拿这个白名单去对账，丢掉不在白名单里的 builtin_* 任务。
#
# ★ 本轮修复：白名单**自动派生**自 _schedule_job() 调用 —— 新增 builtin
#   任务只要走 builtin=True 的入口就不可能被误删，杜绝手写两份字面量漂移。
_REGISTERED_BUILTIN_IDS: Set[str] = set()


def _register_builtin_jobs():
    """注册每天自动跑的核心任务"""
    if not _scheduler:
        return
    # 重置本次注册的 builtin job_id 集合（如果 _schedule_job 失败则不会加入，
    # 避免错误地把失败的任务纳入白名单）
    _REGISTERED_BUILTIN_IDS.clear()
    # 1. 大盘复盘：工作日 10:00 / 18:00 各一次（避免用户没手动触发当天复盘缺失）
    #   此前 16:00 一次 + 类调用 bug 实际一直失败 → 修成两个模块级函数调用
    _schedule_job(
        job_id="builtin_market_review_morning",
        func=run_market_review_daily,
        cron="0 10 * * mon-fri",  # 周一到周五 10:00
        description="工作日上午 10:00 自动生成大盘复盘",
        builtin=True,
    )
    _schedule_job(
        job_id="builtin_market_review_evening",
        func=run_market_review_daily,
        cron="0 18 * * mon-fri",  # 周一到周五 18:00
        description="工作日傍晚 18:00 自动生成大盘复盘",
        builtin=True,
    )
    # 2. 异动扫描：每 5 分钟（交易时段 9:30-11:30, 13:00-15:00）
    _schedule_job(
        job_id="builtin_alert_scan",
        func=run_alert_scan,
        cron="*/5 9-15 * * mon-fri",  # 工作日每 5 分钟（9-15 点）
        description="每 5 分钟扫描自选股异动",
        builtin=True,
    )
    # 3. 模拟盘日终结算：每天 15:30
    _schedule_job(
        job_id="builtin_paper_settle",
        func=run_paper_settle,
        cron="30 15 * * mon-fri",
        description="每天收盘后模拟盘日终结算",
        builtin=True,
    )
    # 4. 社区榜单：每天 15:35
    _schedule_job(
        job_id="builtin_community_rank",
        func=run_community_rank,
        cron="35 15 * * mon-fri",
        description="每天收盘后生成全网最热榜单",
        builtin=True,
    )

    # 5. 数据保留清理：每天 03:10（低峰，避免与复盘/结算抢 SQLite 写锁）
    _schedule_job(
        job_id="builtin_retention_cleanup",
        func=run_retention_cleanup,
        cron="10 3 * * *",
        description="每天凌晨清理过期审计/运行/推送日志",
        builtin=True,
    )

    # ★ 本轮修复：清理 jobstore 中遗留的孤儿 builtin_* 任务
    #   APScheduler 持久化 jobstore 不会因代码改名/删名自动清除旧记录，
    #   导致删掉的 job_id 仍按旧 next_run_time 触发（例如 builtin_market_review_daily）。
    # ⚠️ 以下为 destructive 操作：从 jobstore 中真实删除不属于本次白名单的 builtin_* 任务。
    try:
        existing = list(_scheduler.get_jobs())
        for job in existing:
            jid = getattr(job, "id", None)
            if not jid or not jid.startswith("builtin_"):
                continue
            if jid in _REGISTERED_BUILTIN_IDS:
                continue
            # 孤儿 builtin 任务：单条 try/except 包住，失败不中断后续清理
            try:
                _scheduler.remove_job(jid)
                logger.info("removed orphan builtin job: %s", jid)
            except Exception as e:
                logger.warning("failed to remove orphan builtin job %s: %s", jid, e)
    except Exception as e:
        logger.warning("orphan builtin job cleanup failed: %s", e)


# ============================================================================
# ★ Round 20 升级：用户"定时任务"(scheduled_tasks 表) 接入 APScheduler 自动执行
#   此前该表只被 endpoints_hot CRUD + 手动触发，从不按 cron 自动跑 → 与文档宣称不符。
# ============================================================================
def _load_scheduled_tasks_db() -> List[Dict[str, Any]]:
    """读取 history.db 的 scheduled_tasks（用路径导入避免与 backend/db 包冲突）"""
    import importlib.util
    from pathlib import Path
    _v2_root = Path(__file__).resolve().parent.parent.parent
    spec = importlib.util.spec_from_file_location(
        "v2_root_db_history_sched", _v2_root / "db" / "history.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["v2_root_db_history_sched"] = mod
    spec.loader.exec_module(mod)
    return mod.get_scheduled_tasks()


def _load_scheduled_tasks_db_reset_stuck() -> int:
    """调用 history.py 的 reset_stuck_running_tasks（复用同路径加载，避免重复 exec）。

    返回被重置为 'stale' 的悬挂任务数（0 表示无悬挂）。
    """
    import importlib.util
    from pathlib import Path
    _v2_root = Path(__file__).resolve().parent.parent.parent
    spec = importlib.util.spec_from_file_location(
        "v2_root_db_history_sched_reset", _v2_root / "db" / "history.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["v2_root_db_history_sched_reset"] = mod
    spec.loader.exec_module(mod)
    return int(mod.reset_stuck_running_tasks() or 0)


def _run_user_scheduled_task(task_id: int) -> None:
    """APScheduler 定时触发入口：委托 endpoints_hot 的定时任务 run（run-async + 真实进度 + 落库）

    ★ 探索期修复(Critical)：旧实现用 `new_event_loop()` + `run_until_complete()`
      然后**立刻 `loop.close()`**。但 `hot_analysis_scheduled_tasks_run` 内部调用
      主项目 `run_async_analysis()`，后者用 `asyncio.create_task(...)` 把真正的
      分析协程挂到**当前 loop** 上然后立即 return。外壳协程一返回，
      `loop.close()` 就把尚未执行的 background task 销毁 ——
      `Task was destroyed but it is pending!`，分析从未真正跑起来，
      定时任务的 last_status 也永远停在 running。

      修复：先 run_until_complete 外壳，再让同一个 loop 把剩余 task
      （即后台分析）跑完，最后才关闭 loop。这样 APScheduler 的 worker 线程
      会被占用到分析结束——正是期望行为（job_defaults max_instances=1）。
    """
    try:
        import importlib.util
        from pathlib import Path
        _backend_dir = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "v2_endpoints_hot_sched", _backend_dir / "api" / "endpoints_hot.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["v2_endpoints_hot_sched"] = mod
        spec.loader.exec_module(mod)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(mod.hot_analysis_scheduled_tasks_run(task_id))
            # 外壳返回后，后台分析仍是本 loop 上的 pending task —— 等它结束。
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            if pending:
                logger.info(
                    "user scheduled task %s: waiting for %d background task(s)",
                    task_id, len(pending),
                )
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            # 关闭前再兜一次，避免残留 task 被 "destroyed but pending" 警告吞掉
            try:
                leftover = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in leftover:
                    t.cancel()
                if leftover:
                    loop.run_until_complete(asyncio.gather(*leftover, return_exceptions=True))
            finally:
                loop.close()
        logger.info("user scheduled task %s triggered ok", task_id)
    except Exception as e:
        logger.exception("user scheduled task %s failed: %s", task_id, e)


def register_user_scheduled_tasks() -> int:
    """把 history.db 中 enabled=1 的定时任务注册到调度器。返回注册数。"""
    if not _scheduler:
        return 0
    registered = 0
    try:
        # ★ 修复(重启悬挂)：应用启动时重置上次进程遗留的 last_status='running'
        #   任务为 'stale' —— 手动触发后的落库 watcher 是守护线程，随旧进程消失，
        #   重启后状态永久卡在 running，无恢复机制（见 history.reset_stuck_running_tasks）
        try:
            stuck = _load_scheduled_tasks_db_reset_stuck()
            if stuck:
                logger.info("Reset %d stuck 'running' scheduled tasks to 'stale' on startup", stuck)
        except Exception as e:
            logger.warning("reset stuck running tasks failed: %s", e)
        tasks = _load_scheduled_tasks_db()
    except Exception as e:
        logger.exception("load scheduled tasks failed: %s", e)
        return 0
    live_ids: Set[str] = set()
    for t in tasks:
        try:
            tid = t.get("id")
            cron_expr = t.get("cron_expr")
            enabled = t.get("enabled", 1)
            if not tid or not cron_expr or not enabled:
                continue
            job_id = f"user_scheduled_{tid}"
            _schedule_job(
                job_id=job_id,
                func=_run_user_scheduled_task,
                cron=str(cron_expr),
                description=t.get("name") or job_id,
                args=(int(tid),),
            )
            live_ids.add(job_id)
            registered += 1
        except Exception as e:
            logger.exception("register user scheduled task %s failed: %s", t.get("id"), e)
    # ★ 探索期修复：清理 jobstore 里的孤儿 user_scheduled_* 任务。
    #   APScheduler 的 SQLAlchemyJobStore 不会因为 DB 行被删而自动移除任务，
    #   而 `resync_user_scheduled_tasks()` 只在"通过 API 保存/删除"时才会清理。
    #   若任务行是直接删库、或旧版本删除时 resync 失败过，jobstore 里会残留
    #   任务 —— 实测 `scheduled_tasks` 已空，但 jobstore 仍有 4 个
    #   `user_scheduled_1..4`，到点触发后 `_run_user_scheduled_task` 找不到
    #   任务行而反复报错。这里在启动注册后按"当前 DB 中有效的 job_id"对账删除。
    try:
        for job in list(_scheduler.get_jobs()):
            jid = getattr(job, "id", None)
            if not jid or not jid.startswith("user_scheduled_"):
                continue
            if jid in live_ids:
                continue
            try:
                _scheduler.remove_job(jid)
                logger.info("removed orphan user scheduled job: %s", jid)
            except Exception as e:
                logger.warning("failed to remove orphan user job %s: %s", jid, e)
    except Exception as e:
        logger.warning("orphan user job cleanup failed: %s", e)
    logger.info("Registered %d user scheduled tasks", registered)
    return registered


def _func_accepts_positional(func: Callable) -> bool:
    """该可调用对象是否接受至少一个位置参数（用于判断能否透传 args）"""
    try:
        import inspect

        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    for p in sig.parameters.values():
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL):
            return True
    return False


def register_user_cron_jobs() -> int:
    """把 `cron_user_jobs` 里 enabled=1 的任务注册到调度器。返回注册数。

    ★ 探索期修复(重启失效)：`add_user_job()` 的 docstring 一直承诺"调度器就绪后
      下次 init() 启动会重新登记到 APScheduler"，但**从来没有人实现这一步** ——
      `init()` 只调了 `_register_builtin_jobs()` 和 `register_user_scheduled_tasks()`，
      而后者读的是 **history.db 的 scheduled_tasks**（完全另一套机制）。
      schema 里 `idx_user_jobs_enabled` 的注释"scheduler 重启时按 enabled 过滤"
      也印证了这是漏实现。

      症状：用户通过 `/cron/user-jobs` 建的任务，重启后元数据还在（前端照常列出），
      却没有对应 job → 永不触发，且没有任何提示。另外 `enabled=0` 的停用也形同虚设
      —— jobstore 会把旧 job 按新的进程状态重新拉起，绕过了 enabled 和新的 cron_expr
      （用户改了表达式，重启后仍按旧时间跑）。

      行为约定：
      - enabled=1 且 (func_name, cron_expr) 合法的行 → 注册；
      - 非法行（未知名函数 / 非法 cron）→ 跳过并告警，不影响其它任务与启动；
      - jobstore 里存在但库中已删/已停用的 `job_id` → 一并清除，
        否则 SQLAlchemyJobStore 会让它继续按旧计划触发。
    """
    if not _scheduler:
        return 0

    try:
        conn = get_conn()
        try:
            rows = conn.execute(
                "SELECT job_id, func_name, cron_expr, args_json, description, enabled "
                "FROM cron_user_jobs"
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("register_user_cron_jobs: 读取 cron_user_jobs 失败: %s", e)
        return 0

    live_ids: Set[str] = set()
    registered = 0
    for row in rows:
        job_id = row["job_id"]
        if not row["enabled"]:
            continue
        func_name = row["func_name"]
        cron_expr = row["cron_expr"]
        try:
            func = _resolve_func(func_name)
        except ValueError as e:
            logger.warning("register_user_cron_jobs: 跳过 %r: %s", job_id, e)
            continue
        try:
            from apscheduler.triggers.cron import CronTrigger
            CronTrigger.from_crontab(str(cron_expr))
        except Exception as e:
            logger.warning("register_user_cron_jobs: 跳过 %r（cron 非法 %r）: %s", job_id, cron_expr, e)
            continue

        try:
            raw_args = row["args_json"]
            kwargs = json.loads(raw_args) if raw_args else {}
            if not isinstance(kwargs, dict):
                kwargs = {}
        except Exception:
            kwargs = {}

        # ★ 白名单里的四个任务（run_market_review_daily / run_alert_scan /
        #   run_paper_settle / run_community_rank）签名都不带参数。API 却接受
        #   `args`（默认 {}），一旦用户传了非空 args，注册出来的 job 每次触发都会
        #   抛 TypeError —— 而重启时正是静默重放它的时机。这里明确跳过并告警，
        #   比注册一个必然失败的 job 更容易排查。
        if kwargs:
            if not _func_accepts_positional(func):
                logger.warning(
                    "register_user_cron_jobs: 跳过 %r —— 该任务携带 args=%r，"
                    "但 %s 不接受参数（每次触发都会 TypeError）",
                    job_id, kwargs, func_name,
                )
                continue
            args: Optional[tuple] = tuple(kwargs.values())
        else:
            args = None

        try:
            _schedule_job(
                job_id=job_id,
                func=func,
                cron=str(cron_expr),
                description=row["description"] or job_id,
                args=args,
            )
            live_ids.add(job_id)
            registered += 1
        except Exception as e:
            logger.warning("register_user_cron_jobs: 注册 %r 失败: %s", job_id, e)

    # 对账：清掉库中已不存在或已停用的 user job（非 builtin_ 前缀）
    try:
        for job in list(_scheduler.get_jobs()):
            jid = getattr(job, "id", None)
            if not jid or jid.startswith("builtin_") or jid.startswith("user_scheduled_"):
                continue
            if jid in live_ids:
                continue
            try:
                _scheduler.remove_job(jid)
                logger.info("removed stale user cron job: %s", jid)
            except Exception as e:
                logger.warning("failed to remove stale user cron job %s: %s", jid, e)
    except Exception as e:
        logger.warning("user cron job reconciliation failed: %s", e)

    if registered:
        logger.info("Registered %d user cron jobs from cron_user_jobs", registered)
    return registered


def resync_user_scheduled_tasks() -> int:
    """移除所有 user_scheduled_* 任务后重新注册（保存/删除定时任务后调用，保证增量生效）"""
    if _scheduler:
        for job in list(_scheduler.get_jobs()):
            if job.id and job.id.startswith("user_scheduled_"):
                try:
                    _scheduler.remove_job(job.id)
                except Exception:
                    pass
    return register_user_scheduled_tasks()


def _schedule_job(
    job_id: str,
    func: Callable,
    cron: str,
    description: str = "",
    args: Optional[tuple] = None,
    builtin: bool = False,
) -> None:
    """添加/更新一个 cron 任务（幂等）

    Args:
        builtin: 是否为内置任务。若为 True 且 job_id 以 "builtin_" 开头，
            注册成功后会自动加入 _REGISTERED_BUILTIN_IDS 白名单，
            用于启动时清理 jobstore 中遗留的孤儿 builtin_* 任务。

    ★ Round 20：支持 args 透传（用户定时任务需要携带 task_id）
    ★ 本轮修复：builtin=True 让白名单自动派生，新增 builtin 任务不会漂移。
    """
    if not _scheduler:
        return
    try:
        # 用 module:function 字符串引用（持久化要求）
        if hasattr(func, "__module__") and hasattr(func, "__name__"):
            ref = f"{func.__module__}:{func.__name__}"
        else:
            ref = func
        _scheduler.add_job(
            ref,
            CronTrigger.from_crontab(cron),
            args=args or (),
            id=job_id,
            name=description or job_id,
            replace_existing=True,
            # 内置扫描类任务单次执行可能超过调度间隔（外部数据源挂起时尤甚）。
            # max_instances=1 保证同一任务不会并发堆积；coalesce=True 让错过的
            # 多次触发合并为一次，misfire_grace_time 给迟到执行留出余量。
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        # 注册监听器（job 执行后写日志）
        _attach_listener_if_needed()
        # builtin 任务 → 登记到白名单（仅当 add_job 实际成功后）
        if builtin and job_id.startswith("builtin_"):
            _REGISTERED_BUILTIN_IDS.add(job_id)
        logger.info("Scheduled job: %s (%s)", job_id, cron)
    except Exception as e:
        logger.exception("Failed to schedule %s: %s", job_id, e)


_listener_attached = False


def _attach_listener_if_needed():
    """挂一个全局 listener，捕获所有 job 执行结果写日志"""
    global _listener_attached
    if _listener_attached or not _scheduler:
        return
    def on_event(event):
        if event.exception:
            status = "failed"
            err = str(event.exception)[:500]
        else:
            status = "success"
            err = None
        try:
            job_id = event.job_id
            duration = time.time() - event.scheduled_run_time.timestamp() if event.scheduled_run_time else 0
            conn = get_conn()
            try:
                conn.execute(
                    """INSERT INTO cron_run_log
                       (job_id, started_at, finished_at, status, error_message, duration_seconds)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (job_id, event.scheduled_run_time.timestamp() if event.scheduled_run_time else time.time(),
                     time.time(), status, err, duration),
                )
            finally:
                conn.close()
        except Exception as e:
            logger.warning("Failed to log cron event: %s", e)

    _scheduler.add_listener(on_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    _listener_attached = True


def _wrap_with_log(job_id: str, func: Callable) -> Callable:
    """保留旧 API（用于非持久化场景）"""
    def _finish_run(run_id: int, status: str, duration: float, err: Optional[str] = None) -> None:
        """收尾写 cron_run_log；连接必须关闭（每条未关闭的 SQLite 连接占 2 个 fd）。"""
        try:
            conn = get_conn()
            try:
                conn.execute(
                    "UPDATE cron_run_log SET finished_at=?, status=?, error_message=?, duration_seconds=? WHERE id=?",
                    (time.time(), status, err, duration, run_id),
                )
            finally:
                conn.close()
        except Exception as e:
            logger.warning("cron run_log update failed for job_id=%s: %s", job_id, e)

    def wrapped():
        start = time.time()
        run_id = None
        try:
            conn = get_conn()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO cron_run_log (job_id, started_at, status) VALUES (?, ?, ?)",
                    (job_id, start, "running"),
                )
                run_id = cur.lastrowid
            finally:
                conn.close()
        except Exception as e:
            logger.warning("cron run_log insert failed for job_id=%s: %s", job_id, e)
        try:
            result = func()
            if run_id:
                _finish_run(run_id, "success", time.time() - start)
            return result
        except Exception as e:
            if run_id:
                _finish_run(run_id, "failed", time.time() - start, str(e)[:500])
            logger.exception("Cron job %s failed: %s", job_id, e)
            raise
    return wrapped


# ============================================================================
# 内置任务实现（异步 / 同步兼容）
# ============================================================================
def run_market_review_daily():
    """大盘复盘日跑（工作日 10:00 / 18:00 自动生成）

    ★ 本轮修复 1：加交易日 guard —— 非交易日直接 return，不生成复盘。
       cron 表达式已经限定 mon-fri，但周末调休补班/节假日挪到工作日时不应自动生成。
    ★ 本轮修复 2：加并发护栏 —— catchup daemon 线程与 APScheduler 触发的
       morning job 可能同时进入；用模块级非阻塞锁挡住重复执行，
       抢不到锁则直接 log info + return，不重复抓全市场数据。
    """
    # 并发护栏：APScheduler 的 max_instances=1 只约束自身触发的同一 job，
    # 管不住 catchup 线程。模块级非阻塞锁保证两条链路至多一条进入主流程。
    if not _MARKET_REVIEW_RUN_LOCK.acquire(blocking=False):
        logger.info(
            "[cron] skip daily market review: another run is already in progress"
        )
        return
    try:
        # 交易日判断：节假日（非周末的工作日）/ 周末 / 数据源失败 → 不生成
        if not is_trading_day():
            logger.info("[cron] skip daily market review: today is not a trading day")
            return
        logger.info("[cron] running daily market review")
        try:
            # ★ Round 19 修复(H1)：旧代码 import modules.market_review_v2（不存在）→ 任务恒失败。
            #   复用与 /market-review/generate 端点一致的 services/market_review_service.py。
            # ★ 本轮修复：market_review_service 只有模块级 generate_market_review 函数，
            #   此前调用 MarketReviewService() 类（不存在）→ 任务仍失败。改为直接调模块级函数。
            import importlib.util
            from pathlib import Path
            _v2_root = Path(__file__).resolve().parent.parent.parent
            spec = importlib.util.spec_from_file_location(
                "v2_root_market_review_service", _v2_root / "services" / "market_review_service.py"
            )
            svc_mod = importlib.util.module_from_spec(spec)
            sys.modules["v2_root_market_review_service"] = svc_mod
            spec.loader.exec_module(svc_mod)
            result = svc_mod.generate_market_review(review_date=None, region="cn")
            logger.info("daily market review done: %s", (result or {}).get("status"))
        except Exception as e:
            logger.exception("daily market review failed: %s", e)
    finally:
        _MARKET_REVIEW_RUN_LOCK.release()


# ============================================================================
# ★ 本轮新增：启动补跑 —— 解决"应用没在 10:00 / 18:00 那一刻运行时当天复盘缺失"
# ============================================================================
def _today_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load_history_module_for_catchup():
    """按路径加载 update_version/db/history.py（避开 backend/db 包冲突）。"""
    import importlib.util
    from pathlib import Path
    _v2_root = Path(__file__).resolve().parent.parent.parent
    spec = importlib.util.spec_from_file_location(
        "v2_root_db_history_catchup", _v2_root / "db" / "history.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["v2_root_db_history_catchup"] = mod
    spec.loader.exec_module(mod)
    return mod


def _today_has_non_failed_review(history_mod: Any, today: str) -> bool:
    """今天是否已有"非失败"的复盘记录。

    语义：判断"今天是否已经生成过"，而非"数据好不好看"。
      - 无记录 → False（需要补跑）
      - 记录存在 + status='failed' → False（需要补跑：上次失败了再试一次）
      - 记录存在 + status='ok' / 'degraded' / 其他非 failed → True（不要重跑）

    ★ 本轮修复：之前只认 status='ok'，会把 status='degraded'（数据源临时故障
      时 service 自动写的状态）误判为"没记录"，导致应用每次重启都重新抓
      全市场（15s+）。data 不好不等于没生成过。
    """
    try:
        rec = history_mod.get_market_review(today)
    except Exception as e:
        logger.warning("catchup: get_market_review(%s) failed: %s", today, e)
        return False
    if not rec:
        return False
    status = (rec.get("status") or "").lower()
    return status != "failed"


def catchup_market_review_on_startup() -> None:
    """应用启动时补跑当日大盘复盘（异步、不阻塞启动）。

    触发条件（全部满足）：
      1. 今天是交易日（is_trading_day()）
      2. 当前本地时间已过 10:00（早于 10:00 → 等定时任务触发）
      3. 今天没有"非失败"的复盘记录：无记录 OR status='failed' 才补跑；
         status='ok' / 'degraded' / 任何非 failed 值都视为"今天已生成过"。

    说明：
      - 10:00 / 18:00 共用同一条"今天有没有生成过记录"的判断；
        18:00 任务是 upsert 覆盖，无需在 18:00 之前做"过期"判断。
      - 异步执行（daemon Thread）：主线程立即返回，不阻塞应用启动。
        进程退出时该线程会被直接终止，不做 join（避免拖慢关停）。
      - 任何异常只 log，绝不抛到调用方（不能让应用启动失败）。
    """
    def _runner():
        try:
            run_market_review_daily()
        except Exception as e:
            logger.exception("catchup runner failed: %s", e)

    try:
        # 固化今天字符串一次：避免跨午夜时前后两次 datetime.now() 取到不同日期
        today = _today_iso()
        if not is_trading_day():
            logger.info("catchup: skip, today is not a trading day")
            return
        now = datetime.now().time()
        cutoff = dtime(10, 0, 0)
        if now < cutoff:
            logger.info("catchup: skip, before 10:00 (now=%s)", now.strftime("%H:%M:%S"))
            return
        try:
            history_mod = _load_history_module_for_catchup()
        except Exception as e:
            logger.warning("catchup: load history module failed: %s", e)
            return
        if _today_has_non_failed_review(history_mod, today):
            logger.info(
                "catchup: skip, today's market review already exists (status != 'failed')"
            )
            return
        logger.info(
            "catchup: launching async market review (today=%s, now=%s)",
            today, now.strftime("%H:%M:%S"),
        )
        th = threading.Thread(target=_runner, daemon=True, name="market-review-catchup")
        th.start()
    except Exception as e:
        logger.exception("catchup_market_review_on_startup failed: %s", e)


def run_alert_scan():
    """异动扫描"""
    # ★ 探索期修复(非交易日扫描)：cron 表达式只限到 mon-fri，法定节假日
    #   （工作日但休市）依然会触发。此时行情是**上一交易日收盘的陈旧值**，
    #   价格不动但"量比/放量"这类规则仍可能对陈旧数据报警。
    #   同文件的 run_market_review_daily 早就有这个 guard，这三个任务漏了。
    #   注意 is_trading_day() 按自然日缓存，每 5 分钟调用不会重复打网络。
    try:
        if not is_trading_day():
            logger.info("[cron] skip alert scan: today is not a trading day")
            return
    except Exception as e:
        logger.warning("[cron] trading-day check failed, scanning anyway: %s", e)
    logger.info("[cron] scanning alerts")
    try:
        from modules.alerts_engine import scan_all_alerts
        scan_all_alerts()
    except Exception as e:
        logger.exception("alert scan failed: %s", e)


def run_paper_settle():
    """模拟盘日终"""
    # 非交易日不结算：settle_daily 会写 paper_snapshot，而快照是排行榜权益
    # 与权益曲线的唯一数据源。节假日写入的快照价格与上一交易日完全相同，
    # 等于往收益序列里塞了一个 0 收益日 —— 会压低波动率、让 sharpe 失真。
    try:
        if not is_trading_day():
            logger.info("[cron] skip paper settle: today is not a trading day")
            return
    except Exception as e:
        logger.warning("[cron] trading-day check failed, settling anyway: %s", e)
    logger.info("[cron] paper trading settle")
    try:
        from modules.paper_trading import settle_daily
        settle_daily()
    except Exception as e:
        logger.exception("paper settle failed: %s", e)


def run_community_rank():
    """社区榜单生成"""
    # 榜单反映的是当日盘面热度，非交易日刷新只会把上一交易日的陈旧行情
    # 重新写一遍，并在 community_hot_list 里多出一份重复日期的数据。
    try:
        if not is_trading_day():
            logger.info("[cron] skip community rank: today is not a trading day")
            return
    except Exception as e:
        logger.warning("[cron] trading-day check failed, refreshing anyway: %s", e)
    logger.info("[cron] community rank")
    try:
        from modules.community import refresh_daily_hot_list
        refresh_daily_hot_list()
    except Exception as e:
        logger.exception("community rank failed: %s", e)


def run_retention_cleanup() -> Dict[str, int]:
    """定期清理只增不减的表。

    ★ 探索期修复：`core.audit.cleanup_old()` 早就实现了但**从未被任何地方调用**
      （死代码），而 audit_log / cron_run_log / notification_log 都是只写不清：
      - 异动扫描每 5 分钟一次 → cron_run_log 每天 ~100 行；
      - 每个写操作都进 audit_log；
      - 每次推送都进 notification_log。
      长期运行的容器里这些表会无限增长，最终拖慢 `audit/recent`、
      `notifications/logs` 这类列表查询（它们都带 ORDER BY created_at LIMIT）。
      这里按保留期清理；失败只记日志，不影响其它 cron 任务。
    """
    result: Dict[str, int] = {}
    # 审计日志保留 90 天（与 cleanup_old 的默认值一致）
    try:
        from core import audit as _audit
        result["audit_log"] = _audit.cleanup_old(days=90)
    except Exception as e:
        logger.warning("retention: audit cleanup failed: %s", e)
    # 运行日志/推送日志保留 30 天（排障够用，且量级远大于审计）
    # ★ 注意各表的时间列不同名：cron_run_log 是 started_at，其余是 created_at。
    for table, days, time_col in (
        ("cron_run_log", 30, "started_at"),
        ("notification_log", 30, "created_at"),
        ("llm_usage", 180, "created_at"),
    ):
        try:
            conn = get_conn()
            try:
                cutoff = time.time() - days * 86400
                cur = conn.execute(f"DELETE FROM {table} WHERE {time_col} < ?", (cutoff,))
                result[table] = cur.rowcount
            finally:
                conn.close()
        except Exception as e:
            logger.warning("retention: %s cleanup failed: %s", table, e)
    # 社区热榜：每天 top_n(默认 50) 行 × 每日刷新，且**从不清**。
    #   ★ 探索期修复(只写不清)：本表的时间列是 TEXT 日期（'YYYY-MM-DD'），
    #     不能套上面那套 unix 时间戳的循环，必须单独按字符串比较。
    #     榜单只服务"最近几天"的展示，保留 90 天足够。
    try:
        conn = get_conn()
        try:
            cutoff_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
            cur = conn.execute(
                "DELETE FROM community_hot_list WHERE rank_date < ?", (cutoff_date,)
            )
            result["community_hot_list"] = cur.rowcount
        finally:
            conn.close()
    except Exception as e:
        logger.warning("retention: community_hot_list cleanup failed: %s", e)
    logger.info("[cron] retention cleanup: %s", result)
    return result


# ============================================================================
# 用户自定义任务管理（API 层调用）
# ============================================================================
def add_user_job(
    user_id: str,
    job_id: str,
    func_name: str,
    cron_expr: str,
    args: Optional[dict] = None,
    description: str = "",
) -> bool:
    """添加用户自定义 cron 任务（持久化元数据 + 调度器登记）

    即使调度器未启动（测试/无 APScheduler 场景），元数据也会写入 SQLite，
    调度器就绪后下次 init() 启动会重新登记到 APScheduler。

    ★ 探索期修复(高危)：`job_id` 是调用方完全可控的字符串，而 `_schedule_job`
      用 `replace_existing=True` 登记、`cron_user_jobs` 表也用 job_id 作主键。
      实测 `POST /cron/user-jobs {"job_id":"builtin_market_review_morning",
      "func_name":"run_alert_scan","cron_expr":"*/5 * * * *"}` 返回 200，
      把内置的「工作日 10:00 大盘复盘」直接改成了「每 5 分钟跑异动扫描」——
      普通用户可静默劫持内置调度。这里拒绝 builtin_ 前缀。
    """
    if not isinstance(job_id, str) or not job_id.strip():
        logger.warning("add_user_job rejected: empty job_id")
        return False
    job_id = job_id.strip()
    if job_id.startswith("builtin_"):
        logger.warning("add_user_job rejected: %r 是内置任务前缀，不允许覆盖", job_id)
        return False
    if not isinstance(cron_expr, str) or not cron_expr.strip():
        logger.warning("add_user_job rejected: empty cron_expr")
        return False
    # 1. 校验 func_name 白名单 + cron 表达式
    try:
        _resolve_func(func_name)
    except ValueError as e:
        logger.warning("add_user_job rejected: %s", e)
        return False
    # cron 表达式也校验一下（用 APScheduler 的 from_crontab 解析）
    if _scheduler is not None:
        try:
            from apscheduler.triggers.cron import CronTrigger
            CronTrigger.from_crontab(cron_expr)
        except Exception as e:
            logger.warning("add_user_job: invalid cron expr %r: %s", cron_expr, e)
            return False
    # 2. 归属校验：job_id 是调用方完全可控的字符串，同时又是主键。
    #    ★ 探索期修复(越权覆盖)：DELETE 端点早就补了 _check_resource_owner，但
    #      写入端没有 —— `INSERT OR REPLACE` 会让 B 用同一个 job_id 直接接管
    #      A 的任务，owner/func/cron 全被改写。实测：
    #        alice 建 shared_job_probe → bob 用同 id 提交 → 记录变成
    #        user_id=bob, func=run_paper_settle, cron='0 3 * * *'。
    #      bob 于是可以借 alice 的任务名安排任意白名单函数，alice 也失去自己
    #      的任务。非本人一律拒绝（而不是静默覆盖）。
    try:
        conn = get_conn()
        try:
            existing = conn.execute(
                "SELECT user_id FROM cron_user_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        finally:
            conn.close()
    except Exception as e:
        logger.exception("Failed to read user job meta: %s", e)
        return False
    if existing is not None and existing["user_id"] != user_id:
        logger.warning(
            "add_user_job rejected: %r belongs to another user", job_id
        )
        return False
    # 3. 写 SQLite 元数据
    try:
        conn = get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO cron_user_jobs
                   (job_id, user_id, func_name, cron_expr, args_json, description, enabled, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
                (job_id, user_id, func_name, cron_expr,
                 json.dumps(args or {}), description, time.time()),
            )
        finally:
            conn.close()
    except Exception as e:
        logger.exception("Failed to write user job meta: %s", e)
        return False
    # 3. 调度器可用时也登记
    if _scheduler is not None:
        try:
            _schedule_job(job_id, _resolve_func(func_name), cron_expr, description)
        except Exception as e:
            logger.warning("Schedule registration failed (meta persisted): %s", e)
    return True


def remove_user_job(job_id: str) -> bool:
    """删除用户自定义任务：先从调度器移除，再删 SQLite 元数据"""
    deleted = False
    # 1. 删 SQLite
    try:
        conn = get_conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM cron_user_jobs WHERE job_id=?", (job_id,))
            deleted = cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Delete user job meta failed: %s", e)
        return False
    # 2. 调度器可用时也移除
    if _scheduler is not None:
        try:
            _scheduler.remove_job(job_id)
        except Exception as e:
            logger.debug("remove_job from scheduler failed (may not be scheduled): %s", e)
    return deleted


def list_jobs() -> List[Dict[str, Any]]:
    if not _scheduler:
        return []
    jobs = []
    for job in _scheduler.get_jobs():
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": str(job.next_run_time) if job.next_run_time else None,
            "trigger": str(job.trigger),
        })
    return jobs


def list_user_jobs(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """列出用户自定义任务（来自 cron_user_jobs 表）"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        if user_id:
            cur.execute(
                "SELECT * FROM cron_user_jobs WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            )
        else:
            cur.execute("SELECT * FROM cron_user_jobs ORDER BY created_at DESC")
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def list_run_log(job_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """查看 cron 任务的执行日志"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        if job_id:
            cur.execute(
                "SELECT * FROM cron_run_log WHERE job_id=? ORDER BY started_at DESC LIMIT ?",
                (job_id, limit),
            )
        else:
            cur.execute(
                "SELECT * FROM cron_run_log ORDER BY started_at DESC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def trigger_now(job_id: str) -> bool:
    """立即触发一次（不等 cron）"""
    if not _scheduler:
        return False
    try:
        job = _scheduler.get_job(job_id)
        if job:
            _scheduler.modify_job(job_id, next_run_time=__import__("datetime").datetime.now())
            return True
        return False
    except Exception as e:
        logger.warning("trigger_now failed: %s", e)
        return False


def _resolve_func(func_name: str) -> Callable:
    """根据 func_name 字符串解析函数（允许的内置函数白名单）"""
    builtin = {
        "run_market_review_daily": run_market_review_daily,
        "run_alert_scan": run_alert_scan,
        "run_paper_settle": run_paper_settle,
        "run_community_rank": run_community_rank,
    }
    if func_name not in builtin:
        raise ValueError(f"Unknown func: {func_name}; must be one of {list(builtin)}")
    return builtin[func_name]