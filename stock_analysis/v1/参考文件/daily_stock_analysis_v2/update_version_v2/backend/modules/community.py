# -*- coding: utf-8 -*-
"""
社区 / 内容化
- 每日"全网最热 TOP 10" 公开榜单（社区/SEO 引流）
- 用户分享自己的分析报告（生成短链 + 公开预览页）
- 关注功能
"""
import time
import json
import logging
import secrets
from typing import Dict, Any, List, Optional

from db.migrate import get_conn
from core.data_source import fetch_quote_with_fallback, fetch_quotes_bulk

logger = logging.getLogger(__name__)


# ============================================================================
# 公开榜单
# ============================================================================
def refresh_daily_hot_list(top_n: int = 50) -> Dict[str, Any]:
    """刷新每日公开榜单（被 cron 调用）
    - 数据源：akshare 全市场活跃度（简化版：用 change_pct 排序）
    - 写入 community_hot_list（按日期）
    """
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")

    # 清掉今天的旧数据
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM community_hot_list WHERE rank_date=?", (today,))

    # 从 akshare 取活跃股
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        df = df.sort_values("涨跌幅", ascending=False).head(top_n)
        inserted = 0
        for rank, (_, row) in enumerate(df.iterrows(), start=1):
            code = str(row.get("代码", ""))
            if not code:
                continue
            cur.execute(
                """INSERT INTO community_hot_list
                   (rank_date, symbol, rank, score, change_pct, mention_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (today, code, rank,
                 float(row.get("最新价", 0) or 0),
                 float(row.get("涨跌幅", 0) or 0),
                 max(1, top_n - rank + 1)),  # 简单反比
            )
            inserted += 1
        logger.info("Refreshed hot list: %d entries for %s", inserted, today)
        return {"date": today, "count": inserted}
    except Exception as e:
        logger.exception("refresh_daily_hot_list failed: %s", e)
        return {"date": today, "count": 0, "error": str(e)}


def get_daily_hot_list(rank_date: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """取某日榜单（公开接口，无需登录）

    ★ 第三轮优化：批量并发拉取行情，避免 N+1
    """
    if rank_date is None:
        from datetime import datetime
        rank_date = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT * FROM community_hot_list WHERE rank_date=?
           ORDER BY rank ASC LIMIT ?""",
        (rank_date, limit),
    )
    raw_rows = [dict(r) for r in cur.fetchall()]
    if not raw_rows:
        return []
    symbols = [r["symbol"] for r in raw_rows]
    quotes_map = fetch_quotes_bulk(symbols, parallel=True)
    for d in raw_rows:
        q = quotes_map.get(d["symbol"])
        if q:
            d["name"] = q.get("name")
            d["current_price"] = q.get("price")
    return raw_rows


def get_latest_hot_list_date() -> Optional[str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT MAX(rank_date) as d FROM community_hot_list")
    row = cur.fetchone()
    return row["d"] if row else None


# ============================================================================
# 分享（短链）
# ============================================================================
def _gen_token(length: int = 8) -> str:
    """生成短链 token（URL 安全）"""
    return secrets.token_urlsafe(length)[:length]


def create_share(
    user_id: str,
    resource_type: str,    # analysis_run / backtest_result / market_review
    resource_id: str,
    *,
    title: str = "",
    is_public: bool = True,
    expires_in_days: Optional[int] = None,
) -> Dict[str, Any]:
    """生成分享短链"""
    token = _gen_token()
    conn = get_conn()
    cur = conn.cursor()
    now = time.time()
    expires_at = (now + expires_in_days * 86400) if expires_in_days else None
    cur.execute(
        """INSERT INTO shared_report
           (share_token, user_id, resource_type, resource_id, title,
            is_public, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (token, user_id, resource_type, resource_id, title,
         1 if is_public else 0, expires_at, now),
    )
    return {
        "share_token": token,
        "share_url": f"/s/{token}",
        "expires_at": expires_at,
        "title": title,
    }


def resolve_share(token: str) -> Optional[Dict[str, Any]]:
    """解析短链 → 资源（公开访问）"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM shared_report WHERE share_token=?", (token,))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("expires_at") and d["expires_at"] < time.time():
        return None  # 过期
    # 增加 view_count
    cur.execute("UPDATE shared_report SET view_count=view_count+1 WHERE share_token=?", (token,))
    d["view_count"] = d.get("view_count", 0) + 1
    # 加载资源内容
    d["resource"] = _load_resource(d["resource_type"], d["resource_id"])
    return d


def _load_resource(resource_type: str, resource_id: str) -> Optional[Dict[str, Any]]:
    """根据类型加载资源内容

    v2 后端使用两个 DB：
      - backend/data/v2_dev.db: notifications/alerts/paper/portfolio/community 等
      - data/history.db: analysis_runs/watchlist/templates/market_reviews 等

    因此按 resource_type 选择正确的 DB 路径加载。
    """
    # ★ 资源 → (db_alias, table, id_col, caster)
    #   db_alias ∈ {"v2_dev", "history"} — 由 _open_conn 决定实际路径
    DB_MAP = {
        "backtest_result": ("v2_dev", "backtest_result", "id", int),
        "market_review": ("history", "market_reviews", "review_date", str),
        "paper_account": ("v2_dev", "paper_account", "id", int),
        "analysis_run": ("history", "analysis_runs", "id", int),
        "watchlist": ("history", "watchlist", "code", str),
    }
    if resource_type not in DB_MAP:
        return None
    db_alias, table, id_col, caster = DB_MAP[resource_type]
    conn = _open_conn(db_alias)
    if conn is None:
        return None
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {table} WHERE {id_col}=?", (caster(resource_id),))
        row = cur.fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.warning("_load_resource(%s, %s) failed: %s", resource_type, resource_id, e)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _open_conn(db_alias: str):
    """按 alias 打开对应数据库连接

    - v2_dev  : backend/data/v2_dev.db（通过 db.migrate.get_conn）
    - history : data/history.db（v2 根的 db/history.py，单独 import）
    """
    if db_alias == "v2_dev":
        from db.migrate import get_conn
        return get_conn()
    elif db_alias == "history":
        import sqlite3
        from pathlib import Path
        # 与 update_version_v2/db/history.py 一致的路径
        _DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "history.db"
        if not _DB_PATH.exists():
            logger.warning("history.db not found at %s", _DB_PATH)
            return None
        conn = sqlite3.connect(str(_DB_PATH))
        conn.row_factory = sqlite3.Row
        return conn
    return None


def list_user_shares(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """SELECT * FROM shared_report WHERE user_id=?
           ORDER BY created_at DESC LIMIT ?""",
        (user_id, limit),
    )
    return [dict(r) for r in cur.fetchall()]


def delete_share(share_id: int, user_id: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM shared_report WHERE id=? AND user_id=?", (share_id, user_id))
    return cur.rowcount > 0


def delete_share_by_token(token: str, user_id: str) -> bool:
    """通过 share_token + user_id 双重校验删除分享"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM shared_report WHERE share_token=? AND user_id=?", (token, user_id))
    return cur.rowcount > 0


# ============================================================================
# 关注
# ============================================================================
def follow(follower_id: str, followee_id: str) -> bool:
    if follower_id == followee_id:
        return False
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO user_follow (follower_id, followee_id, created_at) VALUES (?, ?, ?)",
            (follower_id, followee_id, time.time()),
        )
        return True
    except Exception as e:
        if "UNIQUE" in str(e):
            return False
        raise


def unfollow(follower_id: str, followee_id: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM user_follow WHERE follower_id=? AND followee_id=?",
        (follower_id, followee_id),
    )
    return cur.rowcount > 0


def list_followers(user_id: str, limit: int = 100) -> List[str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT follower_id FROM user_follow WHERE followee_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )
    return [r["follower_id"] for r in cur.fetchall()]


def list_following(user_id: str, limit: int = 100) -> List[str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT followee_id FROM user_follow WHERE follower_id=? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    )
    return [r["followee_id"] for r in cur.fetchall()]


def is_following(follower_id: str, followee_id: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM user_follow WHERE follower_id=? AND followee_id=?",
        (follower_id, followee_id),
    )
    return cur.fetchone() is not None


# ============================================================================
# 用户公开主页（聚合：分享数 / 关注数 / 粉丝数）
# ============================================================================
def get_user_profile(user_id: str) -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) as cnt FROM shared_report WHERE user_id=? AND is_public=1",
        (user_id,),
    )
    shares = cur.fetchone()["cnt"]
    cur.execute(
        "SELECT COUNT(*) as cnt FROM user_follow WHERE followee_id=?",
        (user_id,),
    )
    followers = cur.fetchone()["cnt"]
    cur.execute(
        "SELECT COUNT(*) as cnt FROM user_follow WHERE follower_id=?",
        (user_id,),
    )
    following = cur.fetchone()["cnt"]
    return {
        "user_id": user_id,
        "public_shares": shares,
        "followers": followers,
        "following": following,
    }