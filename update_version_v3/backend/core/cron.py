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
from datetime import datetime, time as dtime
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


def _fetch_trading_days_sina() -> Set[str]:
    """从新浪交易日历拉取所有交易日，返回 set of 'YYYY-MM-DD' 字符串。失败抛异常。"""
    import akshare as ak
    df = ak.tool_trade_date_hist_sina()
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
    # 缓存命中：当日已拉取过则直接返回（fetch 一次返回全量历史 ≈8K 行，足够覆盖任意 target）
    with _TRADING_DAY_CACHE_LOCK:
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
        return _scheduler
    except Exception as e:
        logger.exception("Failed to init scheduler: %s", e)
        return None


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None


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
    """APScheduler 定时触发入口：委托 endpoints_hot 的定时任务 run（run-async + 真实进度 + 落库）"""
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
    for t in tasks:
        try:
            tid = t.get("id")
            cron_expr = t.get("cron_expr")
            enabled = t.get("enabled", 1)
            if not tid or not cron_expr or not enabled:
                continue
            _schedule_job(
                job_id=f"user_scheduled_{tid}",
                func=_run_user_scheduled_task,
                cron=str(cron_expr),
                description=t.get("name") or f"user_scheduled_{tid}",
                args=(int(tid),),
            )
            registered += 1
        except Exception as e:
            logger.exception("register user scheduled task %s failed: %s", t.get("id"), e)
    logger.info("Registered %d user scheduled tasks", registered)
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
            conn.execute(
                """INSERT INTO cron_run_log
                   (job_id, started_at, finished_at, status, error_message, duration_seconds)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (job_id, event.scheduled_run_time.timestamp() if event.scheduled_run_time else time.time(),
                 time.time(), status, err, duration),
            )
        except Exception as e:
            logger.warning("Failed to log cron event: %s", e)

    _scheduler.add_listener(on_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
    _listener_attached = True


def _wrap_with_log(job_id: str, func: Callable) -> Callable:
    """保留旧 API（用于非持久化场景）"""
    def wrapped():
        start = time.time()
        run_id = None
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO cron_run_log (job_id, started_at, status) VALUES (?, ?, ?)",
                (job_id, start, "running"),
            )
            run_id = cur.lastrowid
        except Exception as e:
            logger.warning("cron run_log insert failed for job_id=%s: %s", job_id, e)
        try:
            result = func()
            duration = time.time() - start
            if run_id:
                conn = get_conn()
                conn.execute(
                    "UPDATE cron_run_log SET finished_at=?, status=?, duration_seconds=? WHERE id=?",
                    (time.time(), "success", duration, run_id),
                )
            return result
        except Exception as e:
            duration = time.time() - start
            if run_id:
                conn = get_conn()
                conn.execute(
                    "UPDATE cron_run_log SET finished_at=?, status=?, error_message=?, duration_seconds=? WHERE id=?",
                    (time.time(), "failed", str(e)[:500], duration, run_id),
                )
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
    """按路径加载 update_version_v3/db/history.py（避开 backend/db 包冲突）。"""
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
    logger.info("[cron] scanning alerts")
    try:
        from modules.alerts_engine import scan_all_alerts
        scan_all_alerts()
    except Exception as e:
        logger.exception("alert scan failed: %s", e)


def run_paper_settle():
    """模拟盘日终"""
    logger.info("[cron] paper trading settle")
    try:
        from modules.paper_trading import settle_daily
        settle_daily()
    except Exception as e:
        logger.exception("paper settle failed: %s", e)


def run_community_rank():
    """社区榜单生成"""
    logger.info("[cron] community rank")
    try:
        from modules.community import refresh_daily_hot_list
        refresh_daily_hot_list()
    except Exception as e:
        logger.exception("community rank failed: %s", e)


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
    """
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
    # 2. 写 SQLite 元数据
    try:
        conn = get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO cron_user_jobs
               (job_id, user_id, func_name, cron_expr, args_json, description, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            (job_id, user_id, func_name, cron_expr,
             json.dumps(args or {}), description, time.time()),
        )
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
        cur = conn.cursor()
        cur.execute("DELETE FROM cron_user_jobs WHERE job_id=?", (job_id,))
        deleted = cur.rowcount > 0
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
    cur = conn.cursor()
    if user_id:
        cur.execute(
            "SELECT * FROM cron_user_jobs WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        )
    else:
        cur.execute("SELECT * FROM cron_user_jobs ORDER BY created_at DESC")
    return [dict(r) for r in cur.fetchall()]


def list_run_log(job_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """查看 cron 任务的执行日志"""
    conn = get_conn()
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