# -*- coding: utf-8 -*-
"""
社区 / 内容化
- 每日"全网最热 TOP 10" 公开榜单（社区/SEO 引流）
- 用户分享自己的分析报告（生成短链 + 公开预览页）
- 关注功能
"""
import time
import json
import sys
import logging
import secrets
from pathlib import Path
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

    ★ 探索期修复(数据丢失)：旧实现**先 DELETE 当日数据**再抓取。任一数据源
      失败时（实测：akshare 两个接口 + 回退全部网络异常）函数返回
      `count=0, error=...`，但当天榜单已被删空 —— 首页热榜从"有数据"变成
      "空白"，且无法自愈（cron 每天只跑一次）。
      改为：先把新榜单写进临时列表，全部成功后才在同一事务里替换旧数据。
    """
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    cur = conn.cursor()

    # 不在抓取前删除任何数据。抓取失败时旧榜单原样保留。
    rows: List[Dict[str, Any]] = []
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        df = df.sort_values("涨跌幅", ascending=False).head(top_n)
        for rank, (_, row) in enumerate(df.iterrows(), start=1):
            code = str(row.get("代码", ""))
            if not code:
                continue
            rows.append({
                "code": code,
                "rank": rank,
                "score": float(row.get("最新价", 0) or 0),
                "change_pct": float(row.get("涨跌幅", 0) or 0),
                "mention_count": max(1, top_n - rank + 1),
            })
    except Exception as e:
        # ★ Round 19 修复：stock_zh_a_spot_em 偶发 RemoteDisconnected 时，
        #   回退到主项目 hot_lists 的活跃榜（自带 retry + 缓存），保证热力图有数据
        logger.warning("refresh_daily_hot_list spot_em failed (%s), trying fallback", e)
        rows = _fetch_hot_list_fallback_rows(today, top_n)

    if not rows:
        logger.warning("refresh_daily_hot_list: 所有数据源失败，保留 %s 的旧榜单", today)
        return {"date": today, "count": 0, "error": "all hot-list sources failed"}

    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("DELETE FROM community_hot_list WHERE rank_date=?", (today,))
        for item in rows:
            cur.execute(
                """INSERT INTO community_hot_list
                   (rank_date, symbol, rank, score, change_pct, mention_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (today, item["code"], item["rank"], item["score"],
                 item["change_pct"], item["mention_count"]),
            )
        cur.execute("COMMIT")
    except Exception:
        try:
            cur.execute("ROLLBACK")
        except Exception:
            pass
        raise

    logger.info("Refreshed hot list: %d entries for %s", len(rows), today)
    return {"date": today, "count": len(rows)}


def _fetch_hot_list_fallback_rows(today: str, top_n: int = 50) -> List[Dict[str, Any]]:
    """主路径失败时的回退：优先主项目 fetch_full_market_hot，再试 ak.stock_zh_a_spot。

    ★ 探索期重构：旧签名是 `_refresh_hot_list_fallback(cur, today, top_n)`，
      直接拿调用方的 cursor 写库 —— 无法与"先抓取成功、再原子替换"配合。
      现在只负责**取数**，写库统一由 `refresh_daily_hot_list` 在一个事务里完成。
    """
    rows = None
    try:
        # 主项目 data_provider.hot_lists 自带重试 + 降级（同花顺/东方财富/全市场活跃）
        sys_path = sys.path[:]
        main_root = str(Path(__file__).resolve().parent.parent.parent.parent)
        if main_root not in sys.path:
            sys.path.insert(0, main_root)
        try:
            from data_provider.hot_lists import fetch_full_market_hot
            rows = fetch_full_market_hot(top_n)
        finally:
            sys.path[:] = sys_path
    except Exception as e:
        logger.warning("fallback fetch_full_market_hot failed: %s", e)

    if not rows:
        # 终极回退：直接调 ak.stock_zh_a_spot（列名兼容处理）
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot()
            if df is not None and not df.empty:
                sort_col = "换手率" if "换手率" in df.columns else ("涨跌幅" if "涨跌幅" in df.columns else None)
                if sort_col:
                    df = df.sort_values(sort_col, ascending=False).head(top_n)
                    rows = [
                        {"code": str(r.get("代码", "")), "name": str(r.get("名称", "")).strip()}
                        for _, r in df.iterrows() if str(r.get("代码", ""))
                    ]
        except Exception as e2:
            logger.warning("ultimate fallback stock_zh_a_spot failed: %s", e2)

    if not rows:
        return []

    # 用批量行情补 change_pct / price（避免 N+1）
    symbols = [str(r["code"]) for r in rows]
    try:
        quotes_map = fetch_quotes_bulk(symbols, parallel=True)
    except Exception as e:
        logger.warning("fallback quote enrich failed: %s", e)
        quotes_map = {}

    out: List[Dict[str, Any]] = []
    for rank, item in enumerate(rows, start=1):
        code = str(item.get("code") or "")
        if not code:
            continue
        q = quotes_map.get(code) or {}
        out.append({
            "code": code,
            "rank": rank,
            "score": float(q.get("price", 0) or 0),
            "change_pct": float(q.get("change_pct", 0) or 0),
            "mention_count": max(1, top_n - rank + 1),
        })
    return out


def get_daily_hot_list(
    rank_date: Optional[str] = None,
    limit: int = 50,
    quote_budget_seconds: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """取某日榜单（公开接口，无需登录）

    ★ 第三轮优化：批量并发拉取行情，避免 N+1
    ★ 本轮修复：可传 quote_budget_seconds 限制行情富化的总耗时。这是只读接口，
      榜单行本身已在库里；行情只是附加字段，不该让挂起的上游数据源拖垮调用方。
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
    quotes_map = fetch_quotes_bulk(symbols, parallel=True, budget_seconds=quote_budget_seconds)
    for d in raw_rows:
        q = quotes_map.get(d["symbol"])
        if q:
            d["name"] = q.get("name")
            d["current_price"] = q.get("price")
    return raw_rows


def get_latest_hot_list_date() -> Optional[str]:
    conn = get_conn()
    cur = conn.cursor()
    # ★ 修复：过滤未来日期。测试（test_phase13）曾注入 2099-12-31 模拟数据且未清理，
    #   MAX(rank_date) 会把这类测试残留当成"最新日期"返回给前端热力图/社区页，
    #   用户看到 2099 年的假榜单。加 rank_date <= 今天 约束，未来残留一律忽略。
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    cur.execute(
        "SELECT MAX(rank_date) as d FROM community_hot_list WHERE rank_date <= ?",
        (today,),
    )
    row = cur.fetchone()
    return row["d"] if row else None


# ============================================================================
# 分享（短链）
# ============================================================================
def _gen_token(length: int = 12) -> str:
    """生成短链 token（URL 安全）。★ 迭代3：8→12 字符，熵 ~72bit，防暴力枚举"""
    return secrets.token_urlsafe(length)[:length]


# 支持分享的资源类型 —— 必须与 _load_resource 的 DB_MAP 保持一致。
# ★ 探索期修复：create_share 此前不校验 resource_type，`resource_type="nope"`
#   会成功建分享（返回 share_token），但 resolve_share 时 _load_resource 返回
#   None → 用户拿到一条"分享成功但内容为空"的死链，且公开页仍渲染 200。
SUPPORTED_RESOURCE_TYPES = (
    "analysis_run",
    "backtest_result",
    "market_review",
    "paper_account",
    "watchlist",
)


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
    if resource_type not in SUPPORTED_RESOURCE_TYPES:
        raise ValueError(
            f"resource_type 必须是 {', '.join(SUPPORTED_RESOURCE_TYPES)} 之一，"
            f"got {resource_type!r}"
        )
    # ★ 探索期修复：expires_in_days 传字符串（前端把表单值当字符串发）时，
    #   `"3" <= 0` 抛 TypeError → 端点 500（实测）。统一转 int 再校验。
    if expires_in_days is not None:
        try:
            expires_in_days = int(expires_in_days)
        except (TypeError, ValueError):
            raise ValueError(f"expires_in_days 必须是整数，got {expires_in_days!r}")
        if expires_in_days <= 0:
            raise ValueError("expires_in_days 必须 > 0")
    conn = get_conn()
    cur = conn.cursor()
    now = time.time()
    expires_at = (now + expires_in_days * 86400) if expires_in_days else None
    # ★ 迭代3：token 碰撞重试（8→12 字符后碰撞概率极低，仍加 3 次重试防 UNIQUE 冲突 500）
    token = None
    for _ in range(3):
        candidate = _gen_token()
        try:
            cur.execute(
                """INSERT INTO shared_report
                   (share_token, user_id, resource_type, resource_id, title,
                    is_public, expires_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (candidate, user_id, resource_type, resource_id, title,
                 1 if is_public else 0, expires_at, now),
            )
            token = candidate
            break
        except Exception as e:
            if "UNIQUE" not in str(e):
                raise
            # token 碰撞 → 重试
    if token is None:
        raise ValueError("生成分享链接失败，请重试")
    return {
        "share_token": token,
        "share_url": f"/s/{token}",
        "expires_at": expires_at,
        "title": title,
    }


def get_share_owner(token: str) -> Optional[str]:
    """只取分享的 owner，不做公开/过期过滤（供撤销等归属校验使用）。

    ★ resolve_share() 会拒绝私有/过期分享（那是**公开访问**的正确语义），
      但撤销接口需要能校验私有分享的归属，不能用它判存在。
    """
    conn = get_conn()
    row = conn.execute(
        "SELECT user_id FROM shared_report WHERE share_token=?", (token,)
    ).fetchone()
    return row["user_id"] if row else None


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
    # ★ 修复(私有分享泄露)：create_share 支持 is_public=0（私有分享），
    #   但 resolve_share 从不校验 → 任何拿到 token 的人都能读取私有分享内容。
    #   私有分享仅允许创建者本人通过带 owner 校验的路径访问，公开短链拒绝。
    if not d.get("is_public"):
        return None  # 私有分享不通过公开短链解析
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
        # 与 update_version/db/history.py 一致的路径
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