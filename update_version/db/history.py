# -*- coding: utf-8 -*-
"""
分析历史记录持久化 —— SQLite 存储
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 数据库路径：update_version/data/history.db
# ★ 探索期修复(数据损坏事故)：支持 V4_HISTORY_PATH 覆盖。
#   默认路径位于容器的 bind mount 目录，宿主机跑测试时若不重定向，
#   会与运行中的容器并发写同一 SQLite 文件而写坏库。
_DB_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_PATH = Path(os.environ.get("V4_HISTORY_PATH") or (_DB_DIR / "history.db"))


def _ensure_db_dir() -> None:
    """确保数据库目录存在。

    ★ 探索期修复：V4_HISTORY_PATH 可指向任意位置，此时要创建的是
      `_DB_PATH.parent` 而不是默认的 `_DB_DIR`。旧写法在覆盖路径指向
      尚不存在的子目录时，`sqlite3.connect` 抛 `unable to open database file`。
    """
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _DB_DIR != _DB_PATH.parent:
        _DB_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def _get_conn() -> Generator[sqlite3.Connection, None, None]:
    """获取数据库连接（自动提交/关闭）。"""
    _ensure_db_dir()
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """初始化数据库表结构（幂等）。"""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS analysis_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'ok',
                hot_lists_summary TEXT DEFAULT '{}',
                hot_lists_status TEXT DEFAULT '{}',
                hot_lists_notes TEXT DEFAULT '{}',
                raw_intersection_count INTEGER DEFAULT 0,
                filtered_intersection_count INTEGER DEFAULT 0,
                analyzed_stock_count INTEGER DEFAULT 0,
                passed_stock_count INTEGER DEFAULT 0,
                filter_config TEXT DEFAULT '{}',
                filtered_stocks TEXT DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS analysis_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                in_sources TEXT DEFAULT '[]',
                analyses TEXT DEFAULT '[]',
                pass_reason TEXT DEFAULT '',
                avg_score REAL DEFAULT 0.0,
                final_advice TEXT DEFAULT '',
                FOREIGN KEY (run_id) REFERENCES analysis_runs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_results_run_id ON analysis_results(run_id);
            CREATE INDEX IF NOT EXISTS idx_runs_created_at ON analysis_runs(created_at DESC);

            -- 自选股跟踪
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            -- 分析模板（过滤规则预设）
            CREATE TABLE IF NOT EXISTS analysis_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT DEFAULT '',
                config TEXT NOT NULL DEFAULT '{}',
                is_default INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            -- 定时任务
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                cron_expr TEXT NOT NULL,
                config TEXT NOT NULL DEFAULT '{}',
                last_run_at TEXT,
                last_status TEXT,
                last_result_id INTEGER,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            -- 大盘复盘（每日报告）
            CREATE TABLE IF NOT EXISTS market_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_date TEXT NOT NULL UNIQUE,
                region TEXT NOT NULL DEFAULT 'cn',
                status TEXT NOT NULL DEFAULT 'ok',
                summary TEXT DEFAULT '',
                breadth_json TEXT DEFAULT '{}',
                indices_json TEXT DEFAULT '{}',
                sectors_json TEXT DEFAULT '{}',
                signals_json TEXT DEFAULT '{}',
                report_markdown TEXT DEFAULT '',
                duration_seconds REAL DEFAULT 0.0,
                error TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE INDEX IF NOT EXISTS idx_watchlist_code ON watchlist(code);
            CREATE INDEX IF NOT EXISTS idx_templates_name ON analysis_templates(name);
            CREATE INDEX IF NOT EXISTS idx_market_reviews_date ON market_reviews(review_date DESC);
        """)
        # ★ 迭代1：为已存在的 analysis_runs 表补 filtered_stocks 列（CREATE IF NOT EXISTS 不改旧表）
        try:
            conn.execute("ALTER TABLE analysis_runs ADD COLUMN filtered_stocks TEXT DEFAULT '[]'")
        except Exception:
            pass  # 列已存在
    logger.info("数据库已初始化: %s", _DB_PATH)


def save_analysis_run(
    *,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
    status: str,
    hot_lists_summary: Dict[str, int],
    hot_lists_status: Dict[str, str],
    hot_lists_notes: Dict[str, str],
    raw_intersection_count: int,
    filtered_intersection_count: int,
    analyzed_stock_count: int,
    passed_stock_count: int,
    filter_config: Dict[str, Any],
    results: List[Dict[str, Any]],
    filtered_stocks: Optional[List[Dict[str, Any]]] = None,
) -> int:
    """
    保存一次完整的分析记录到数据库。

    Returns:
        新记录的 ID
    """
    with _get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO analysis_runs (
                started_at, finished_at, duration_seconds, status,
                hot_lists_summary, hot_lists_status, hot_lists_notes,
                raw_intersection_count, filtered_intersection_count,
                analyzed_stock_count, passed_stock_count, filter_config,
                filtered_stocks
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                finished_at,
                duration_seconds,
                status,
                json.dumps(hot_lists_summary, ensure_ascii=False),
                json.dumps(hot_lists_status, ensure_ascii=False),
                json.dumps(hot_lists_notes, ensure_ascii=False),
                raw_intersection_count,
                filtered_intersection_count,
                analyzed_stock_count,
                passed_stock_count,
                json.dumps(filter_config, ensure_ascii=False),
                json.dumps(filtered_stocks or [], ensure_ascii=False),
            ),
        )
        run_id = cursor.lastrowid

        # 批量插入结果
        if results:
            conn.executemany(
                """
                INSERT INTO analysis_results (
                    run_id, code, name, in_sources, analyses,
                    pass_reason, avg_score, final_advice
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        r.get("code", ""),
                        r.get("name", ""),
                        json.dumps(r.get("in_sources", []), ensure_ascii=False),
                        json.dumps(r.get("analyses", []), ensure_ascii=False),
                        r.get("pass_reason", ""),
                        r.get("avg_score", 0.0),
                        r.get("final_advice", ""),
                    )
                    for r in results
                ],
            )

    logger.info("分析记录已保存: run_id=%d, 结果 %d 条", run_id, len(results))
    return run_id


def _row_to_run_dict(row: sqlite3.Row, results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """将 analysis_runs 行 + 该次 results 拼装为标准字典。"""
    try:
        filtered_stocks = json.loads(row["filtered_stocks"] or "[]")
    except (KeyError, TypeError, json.JSONDecodeError):
        filtered_stocks = []
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "duration_seconds": row["duration_seconds"],
        "status": row["status"],
        "hot_lists_summary": json.loads(row["hot_lists_summary"] or "{}"),
        "hot_lists_status": json.loads(row["hot_lists_status"] or "{}"),
        "hot_lists_notes": json.loads(row["hot_lists_notes"] or "{}"),
        "raw_intersection_count": row["raw_intersection_count"],
        "filtered_intersection_count": row["filtered_intersection_count"],
        "analyzed_stock_count": row["analyzed_stock_count"],
        "passed_stock_count": row["passed_stock_count"],
        "filter_config": json.loads(row["filter_config"] or "{}"),
        "results": results,
        # ★ 迭代1：持久化"未通过过滤"股票，历史/对比/PDF 也能展示
        "filtered_stocks": filtered_stocks,
    }


def _build_results(result_rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    """从 analysis_results 行构造结果列表。"""
    return [
        {
            "code": rr["code"],
            "name": rr["name"],
            "in_sources": json.loads(rr["in_sources"] or "[]"),
            "analyses": json.loads(rr["analyses"] or "[]"),
            "pass_reason": rr["pass_reason"],
            "avg_score": rr["avg_score"],
            "final_advice": rr["final_advice"],
        }
        for rr in result_rows
    ]


def get_analysis_history(
    *,
    page: int = 1,
    page_size: int = 20,
) -> Tuple[List[Dict[str, Any]], int]:
    """
    查询分析历史记录（分页）。

    Returns:
        (items, total)

    Performance: 单页 results 用一次 `IN (...)` 批查取代之前对每行单查，
    消除 N+1 问题。

    排序：created_at DESC，加 id DESC 作为决胜排序（同秒插入多行时避免不确定）。
    """
    with _get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM analysis_runs").fetchone()[0]
        offset = (page - 1) * page_size
        rows = conn.execute(
            """
            SELECT * FROM analysis_runs
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (page_size, offset),
        ).fetchall()

        if not rows:
            return [], total

        # 单次 IN(...) 批查该页所有 run 的 results
        run_ids = [r["id"] for r in rows]
        placeholders = ",".join("?" for _ in run_ids)
        result_rows = conn.execute(
            f"""
            SELECT * FROM analysis_results
            WHERE run_id IN ({placeholders})
            ORDER BY avg_score DESC
            """,
            run_ids,
        ).fetchall()

        # 按 run_id 分组
        grouped: Dict[int, List[sqlite3.Row]] = {rid: [] for rid in run_ids}
        for rr in result_rows:
            grouped.setdefault(rr["run_id"], []).append(rr)

        items = [_row_to_run_dict(r, _build_results(grouped[r["id"]])) for r in rows]

    return items, total


def get_analysis_run(run_id: int) -> Optional[Dict[str, Any]]:
    """查询单次分析记录（含结果）。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM analysis_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            return None

        result_rows = conn.execute(
            "SELECT * FROM analysis_results WHERE run_id = ? ORDER BY avg_score DESC",
            (run_id,),
        ).fetchall()

        return _row_to_run_dict(row, _build_results(result_rows))


def delete_analysis_run(run_id: int) -> bool:
    """删除一次分析记录（级联删除结果）。"""
    with _get_conn() as conn:
        cursor = conn.execute("DELETE FROM analysis_runs WHERE id = ?", (run_id,))
        return cursor.rowcount > 0


def get_runs_for_compare(run_id_1: int, run_id_2: int) -> Tuple[Optional[Dict], Optional[Dict]]:
    """获取两次分析记录用于对比。"""
    return get_analysis_run(run_id_1), get_analysis_run(run_id_2)


# ============================================================
# 自选股 CRUD
# ============================================================
def get_watchlist() -> List[Dict[str, Any]]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM watchlist ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

def add_to_watchlist(code: str, name: str = "", notes: str = "") -> int:
    # ★ 修复：保留原 id 与 created_at，只更新 name/notes；返回原 id
    with _get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO watchlist (code, name, notes) VALUES (?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET name=excluded.name, notes=excluded.notes
            """,
            (code, name, notes),
        )
        # ON CONFLICT DO UPDATE 不更新时 lastrowid 可能为 0，回查真实 id
        if cursor.lastrowid:
            return cursor.lastrowid
        row = conn.execute("SELECT id FROM watchlist WHERE code = ?", (code,)).fetchone()
        return int(row["id"]) if row else 0

def remove_from_watchlist(code: str) -> bool:
    with _get_conn() as conn:
        cursor = conn.execute("DELETE FROM watchlist WHERE code = ?", (code,))
        return cursor.rowcount > 0

def is_in_watchlist(code: str) -> bool:
    with _get_conn() as conn:
        row = conn.execute("SELECT 1 FROM watchlist WHERE code = ?", (code,)).fetchone()
        return row is not None


# ============================================================
# 分析模板 CRUD
# ============================================================
def get_templates() -> List[Dict[str, Any]]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM analysis_templates ORDER BY is_default DESC, name").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["config"] = json.loads(d.get("config") or "{}")
            result.append(d)
        return result

def get_template(template_id: int) -> Optional[Dict[str, Any]]:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM analysis_templates WHERE id = ?", (template_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["config"] = json.loads(d.get("config") or "{}")
        return d

def save_template(name: str, config: Dict[str, Any], description: str = "", is_default: bool = False) -> int:
    with _get_conn() as conn:
        if is_default:
            conn.execute("UPDATE analysis_templates SET is_default = 0")
        cursor = conn.execute(
            """INSERT INTO analysis_templates (name, description, config, is_default, created_at, updated_at)
               VALUES (?, ?, ?, ?, datetime('now', 'localtime'), datetime('now', 'localtime'))
               ON CONFLICT(name) DO UPDATE SET
                 description=excluded.description, config=excluded.config,
                 is_default=excluded.is_default, updated_at=datetime('now', 'localtime')""",
            (name, description, json.dumps(config, ensure_ascii=False), 1 if is_default else 0),
        )
        return cursor.lastrowid

def delete_template(template_id: int) -> bool:
    with _get_conn() as conn:
        cursor = conn.execute("DELETE FROM analysis_templates WHERE id = ?", (template_id,))
        return cursor.rowcount > 0


# ============================================================
# 定时任务 CRUD
# ============================================================
def get_scheduled_tasks() -> List[Dict[str, Any]]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM scheduled_tasks ORDER BY created_at DESC").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["config"] = json.loads(d.get("config") or "{}")
            result.append(d)
        return result

def get_scheduled_task(task_id: int) -> Optional[Dict[str, Any]]:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["config"] = json.loads(d.get("config") or "{}")
        return d

def save_scheduled_task(name: str, cron_expr: str, config: Dict[str, Any], enabled: bool = True, task_id: Optional[int] = None) -> int:
    with _get_conn() as conn:
        if task_id:
            cur = conn.execute(
                "UPDATE scheduled_tasks SET name=?, cron_expr=?, config=?, enabled=?, updated_at=datetime('now','localtime') WHERE id=?",
                (name, cron_expr, json.dumps(config, ensure_ascii=False), 1 if enabled else 0, task_id),
            )
            # ★ Round 21 修复(L10)：UPDATE 0 行说明 task_id 不存在，不再静默"假成功"
            if cur.rowcount == 0:
                raise ValueError(f"定时任务 {task_id} 不存在，无法更新")
            return task_id
        else:
            cursor = conn.execute(
                "INSERT INTO scheduled_tasks (name, cron_expr, config, enabled) VALUES (?, ?, ?, ?)",
                (name, cron_expr, json.dumps(config, ensure_ascii=False), 1 if enabled else 0),
            )
            return cursor.lastrowid

def update_task_run(task_id: int, status: str, result_id: Optional[int] = None) -> None:
    with _get_conn() as conn:
        if result_id is not None:
            conn.execute(
                "UPDATE scheduled_tasks SET last_run_at=datetime('now','localtime'), last_status=?, last_result_id=?, updated_at=datetime('now','localtime') WHERE id=?",
                (status, result_id, task_id),
            )
        else:
            conn.execute(
                "UPDATE scheduled_tasks SET last_run_at=datetime('now','localtime'), last_status=?, updated_at=datetime('now','localtime') WHERE id=?",
                (status, task_id),
            )

def reset_stuck_running_tasks() -> int:
    """把 last_status='running' 的定时任务重置为 'stale'。

    ★ 修复(重启悬挂)：手动触发定时任务后，落库 watcher 是 _spawn_scheduled_watcher
      启动的守护线程，随旧进程消失；后端重启后该任务 last_status 永远卡在 'running'
      （无任何恢复机制，UI 显示"运行中"但实际早已中断）。
      应用启动时调用本函数，把所有遗留 running 标记为 stale（中断/未完成），
      让用户看到真实状态并可重新执行。
    """
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE scheduled_tasks SET last_status='stale', updated_at=datetime('now','localtime') WHERE last_status='running'"
        )
        return cur.rowcount

def delete_scheduled_task(task_id: int) -> bool:
    with _get_conn() as conn:
        cursor = conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
        return cursor.rowcount > 0


# ============================================================
# 大盘复盘 CRUD
# ============================================================
def save_market_review(
    *,
    review_date: str,
    region: str = "cn",
    status: str = "ok",
    summary: str = "",
    breadth: Dict[str, Any],
    indices: Dict[str, Any],
    sectors: Dict[str, Any],
    signals: Dict[str, Any],
    report_markdown: str = "",
    duration_seconds: float = 0.0,
    error: str = "",
) -> int:
    """
    保存大盘复盘一日记录（同 review_date 走 upsert）。

    Returns:
        记录 ID（新增或更新后）
    """
    with _get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO market_reviews (
                review_date, region, status, summary,
                breadth_json, indices_json, sectors_json, signals_json,
                report_markdown, duration_seconds, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(review_date) DO UPDATE SET
                region=excluded.region,
                status=excluded.status,
                summary=excluded.summary,
                breadth_json=excluded.breadth_json,
                indices_json=excluded.indices_json,
                sectors_json=excluded.sectors_json,
                signals_json=excluded.signals_json,
                report_markdown=excluded.report_markdown,
                duration_seconds=excluded.duration_seconds,
                error=excluded.error
            """,
            (
                review_date,
                region,
                status,
                summary,
                json.dumps(breadth, ensure_ascii=False),
                json.dumps(indices, ensure_ascii=False),
                json.dumps(sectors, ensure_ascii=False),
                json.dumps(signals, ensure_ascii=False),
                report_markdown,
                duration_seconds,
                error,
            ),
        )
        if cursor.lastrowid:
            return cursor.lastrowid
        row = conn.execute(
            "SELECT id FROM market_reviews WHERE review_date = ?",
            (review_date,),
        ).fetchone()
        return int(row["id"]) if row else 0


def _row_to_review_dict(row: sqlite3.Row) -> Dict[str, Any]:
    """market_reviews 行 → 标准字典。"""
    return {
        "id": row["id"],
        "review_date": row["review_date"],
        "region": row["region"],
        "status": row["status"],
        "summary": row["summary"],
        "breadth": json.loads(row["breadth_json"] or "{}"),
        "indices": json.loads(row["indices_json"] or "{}"),
        "sectors": json.loads(row["sectors_json"] or "{}"),
        "signals": json.loads(row["signals_json"] or "{}"),
        "report_markdown": row["report_markdown"],
        "duration_seconds": row["duration_seconds"],
        "error": row["error"],
        "created_at": row["created_at"],
    }


def get_market_review(review_date: str) -> Optional[Dict[str, Any]]:
    """按 review_date 获取单日复盘记录。"""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM market_reviews WHERE review_date = ?", (review_date,)
        ).fetchone()
        return _row_to_review_dict(row) if row else None


def get_latest_market_review(region: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """获取最近一次复盘记录（按 review_date DESC）。"""
    with _get_conn() as conn:
        if region:
            row = conn.execute(
                "SELECT * FROM market_reviews WHERE region = ? ORDER BY review_date DESC, id DESC LIMIT 1",
                (region,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM market_reviews ORDER BY review_date DESC, id DESC LIMIT 1"
            ).fetchone()
        return _row_to_review_dict(row) if row else None


def get_market_review_list(
    *,
    page: int = 1,
    page_size: int = 20,
    region: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """分页获取复盘记录列表（按 review_date DESC, id DESC）。"""
    with _get_conn() as conn:
        if region:
            total = conn.execute(
                "SELECT COUNT(*) FROM market_reviews WHERE region = ?", (region,)
            ).fetchone()[0]
            offset = (page - 1) * page_size
            rows = conn.execute(
                """
                SELECT * FROM market_reviews
                WHERE region = ?
                ORDER BY review_date DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (region, page_size, offset),
            ).fetchall()
        else:
            total = conn.execute("SELECT COUNT(*) FROM market_reviews").fetchone()[0]
            offset = (page - 1) * page_size
            rows = conn.execute(
                """
                SELECT * FROM market_reviews
                ORDER BY review_date DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            ).fetchall()
        items = [_row_to_review_dict(r) for r in rows]
        return items, total


def delete_market_review(review_date: str) -> bool:
    """按 review_date 删除一次复盘记录。"""
    with _get_conn() as conn:
        cursor = conn.execute(
            "DELETE FROM market_reviews WHERE review_date = ?", (review_date,)
        )
        return cursor.rowcount > 0


# 模块加载时自动初始化表结构
init_db()
