# -*- coding: utf-8 -*-
"""
APScheduler 持久化 cron 调度器
- 替代之前 /latest 那种"用户打开页面才跑"的模式
- 任务持久化到 SQLite（重启不丢）
- 内置错峰：避免任务堆在同一时刻
- 支持 manual_run / pause / resume
"""
import json
import time
import logging
import asyncio
import traceback
from typing import Optional, Callable, Dict, Any, List

logger = logging.getLogger(__name__)

# 尝试 import APScheduler
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
    APS_AVAILABLE = True
except ImportError:
    APS_AVAILABLE = False
    logger.warning("APScheduler not installed; cron features disabled")

from db.migrate import get_conn


_scheduler: Optional[Any] = None
DB_PATH = None  # 由 init() 注入


def init(db_path: str = "v2_dev.db") -> Optional[Any]:
    """初始化调度器（必须在 app 启动时调用一次）"""
    global _scheduler, DB_PATH
    DB_PATH = db_path
    if not APS_AVAILABLE:
        logger.warning("APScheduler not available, skipping scheduler init")
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
def _register_builtin_jobs():
    """注册每天自动跑的核心任务"""
    if not _scheduler:
        return
    # 1. 大盘复盘：每天 16:00 自动生成（收盘后）
    _schedule_job(
        job_id="builtin_market_review_daily",
        func=run_market_review_daily,
        cron="0 16 * * mon-fri",   # 周一到周五 16:00
        description="每天收盘后自动生成大盘复盘",
    )
    # 2. 异动扫描：每 5 分钟（交易时段 9:30-11:30, 13:00-15:00）
    _schedule_job(
        job_id="builtin_alert_scan",
        func=run_alert_scan,
        cron="*/5 9-15 * * mon-fri",  # 工作日每 5 分钟（9-15 点）
        description="每 5 分钟扫描自选股异动",
    )
    # 3. 模拟盘日终结算：每天 15:30
    _schedule_job(
        job_id="builtin_paper_settle",
        func=run_paper_settle,
        cron="30 15 * * mon-fri",
        description="每天收盘后模拟盘日终结算",
    )
    # 4. 社区榜单：每天 15:35
    _schedule_job(
        job_id="builtin_community_rank",
        func=run_community_rank,
        cron="35 15 * * mon-fri",
        description="每天收盘后生成全网最热榜单",
    )


def _schedule_job(job_id: str, func: Callable, cron: str, description: str = "") -> None:
    """添加/更新一个 cron 任务（幂等）"""
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
            id=job_id,
            name=description or job_id,
            replace_existing=True,
        )
        # 注册监听器（job 执行后写日志）
        _attach_listener_if_needed()
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
        except Exception:
            pass
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
    """大盘复盘日跑"""
    logger.info("[cron] running daily market review")
    try:
        from modules.market_review_v2 import generate_daily_review
        generate_daily_review(target_date=None)
    except Exception as e:
        logger.exception("daily market review failed: %s", e)


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