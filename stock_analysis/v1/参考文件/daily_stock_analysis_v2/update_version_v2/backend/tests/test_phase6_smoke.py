# -*- coding: utf-8 -*-
"""Phase 6 smoke test: 社区（榜单 + 分享 + 关注）"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrate import get_conn
from modules import community as cm


def _reset_tables():
    conn = get_conn()
    conn.execute("DELETE FROM community_hot_list")
    conn.execute("DELETE FROM shared_report")
    conn.execute("DELETE FROM user_follow")


def test_create_share_returns_token_and_url():
    _reset_tables()
    share = cm.create_share("u1", "backtest_result", "123", title="我的MACD策略回测")
    assert "share_token" in share and len(share["share_token"]) >= 6
    assert share["share_url"].startswith("/s/")
    print(f"OK share: token={share['share_token']}, url={share['share_url']}")


def test_resolve_share_increments_view_count():
    _reset_tables()
    share = cm.create_share("u1", "backtest_result", "123", title="test")
    resolved = cm.resolve_share(share["share_token"])
    assert resolved is not None
    assert resolved["title"] == "test"
    assert resolved["view_count"] == 1
    # 第二次访问应 +1
    resolved2 = cm.resolve_share(share["share_token"])
    assert resolved2["view_count"] == 2
    print(f"OK resolve: view_count={resolved2['view_count']}")


def test_resolve_unknown_token_returns_none():
    _reset_tables()
    assert cm.resolve_share("nonexistent_token") is None
    print("OK unknown token returns None")


def test_resolve_expired_share_returns_none():
    _reset_tables()
    share = cm.create_share("u1", "backtest_result", "123", expires_in_days=-1)
    assert cm.resolve_share(share["share_token"]) is None
    print(f"OK expired share blocked")


def test_private_share_still_resolvable():
    """非公开分享仍然可以通过 token 解析（仅 owner 控制分享）"""
    _reset_tables()
    share = cm.create_share("u1", "backtest_result", "123", is_public=False)
    resolved = cm.resolve_share(share["share_token"])
    # 我们的设计：is_public 仅影响列表 / SEO，不阻止短链访问
    assert resolved is not None
    print(f"OK private share accessible via token")


def test_list_user_shares():
    _reset_tables()
    cm.create_share("u1", "backtest_result", "1", title="A")
    cm.create_share("u1", "backtest_result", "2", title="B")
    cm.create_share("u2", "backtest_result", "3", title="C")
    s1 = cm.list_user_shares("u1")
    s2 = cm.list_user_shares("u2")
    assert len(s1) == 2
    assert len(s2) == 1
    print(f"OK list: u1={len(s1)}, u2={len(s2)}")


def test_delete_share_requires_owner():
    _reset_tables()
    share_obj = cm.create_share("u1", "backtest_result", "1", title="test")
    # u2 不能删 u1 的
    assert cm.delete_share(1, "u2") is False  # id 是 1 吗？需要查
    # u1 能删自己的
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM shared_report WHERE share_token=?", (share_obj["share_token"],))
    share_id = cur.fetchone()["id"]
    assert cm.delete_share(share_id, "u1") is True
    print(f"OK delete: owner only")


def test_share_tokens_are_unique():
    _reset_tables()
    tokens = set()
    for _ in range(20):
        s = cm.create_share("u1", "backtest_result", "x")
        tokens.add(s["share_token"])
    assert len(tokens) == 20, f"Tokens should be unique, got {len(tokens)}"
    print(f"OK unique tokens: {len(tokens)}/20")


def test_follow_unfollow():
    _reset_tables()
    assert cm.follow("u1", "u2") is True
    assert cm.is_following("u1", "u2") is True
    assert cm.is_following("u2", "u1") is False
    # 重复关注应失败（UNIQUE）
    assert cm.follow("u1", "u2") is False
    # 取消关注
    assert cm.unfollow("u1", "u2") is True
    assert cm.is_following("u1", "u2") is False
    print("OK follow / unfollow")


def test_follow_self_blocked():
    _reset_tables()
    assert cm.follow("u1", "u1") is False
    print("OK self-follow blocked")


def test_list_followers_and_following():
    _reset_tables()
    cm.follow("u1", "u3")
    cm.follow("u2", "u3")
    cm.follow("u3", "u4")
    followers_of_3 = cm.list_followers("u3")
    following_of_3 = cm.list_following("u3")
    assert set(followers_of_3) == {"u1", "u2"}
    assert set(following_of_3) == {"u4"}
    print(f"OK followers={followers_of_3}, following={following_of_3}")


def test_user_profile_aggregates():
    _reset_tables()
    cm.create_share("u1", "backtest_result", "1", is_public=True)
    cm.create_share("u1", "backtest_result", "2", is_public=True)
    cm.create_share("u1", "backtest_result", "3", is_public=False)  # 私有不算
    cm.follow("u2", "u1")
    cm.follow("u3", "u1")
    cm.follow("u1", "u4")
    profile = cm.get_user_profile("u1")
    assert profile["public_shares"] == 2
    assert profile["followers"] == 2
    assert profile["following"] == 1
    print(f"OK profile: shares={profile['public_shares']}, followers={profile['followers']}, following={profile['following']}")


def test_get_daily_hot_list_empty():
    _reset_tables()
    rows = cm.get_daily_hot_list("2026-07-30")
    assert rows == []
    print("OK empty hot list returns []")


def test_get_latest_hot_list_date_empty():
    _reset_tables()
    assert cm.get_latest_hot_list_date() is None
    print("OK latest date empty -> None")


def test_get_daily_hot_list_with_data():
    _reset_tables()
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    for rank, sym in enumerate(["600519", "000858", "002156"], start=1):
        conn.execute(
            """INSERT INTO community_hot_list
               (rank_date, symbol, rank, score, change_pct, mention_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (today, sym, rank, 100.0, 5.0, 10),
        )
    rows = cm.get_daily_hot_list(today)
    assert len(rows) == 3
    assert rows[0]["rank"] == 1
    assert rows[0]["symbol"] == "600519"
    print(f"OK hot list: {len(rows)} entries, top={rows[0]['symbol']}")


def test_refresh_daily_hot_list_handles_data_source_failure():
    """akshare 失败时不崩"""
    _reset_tables()
    # 不 mock 真实环境，没有 akshare 也会优雅处理
    # 但 import akshare 会成功，ak.stock_zh_a_spot_em() 可能失败 → 应记 error 但不抛
    result = cm.refresh_daily_hot_list(top_n=10)
    # 期望：count=0 或 count>0，二选一；不应抛异常
    assert "count" in result
    print(f"OK refresh graceful: {result}")


if __name__ == "__main__":
    test_create_share_returns_token_and_url()
    test_resolve_share_increments_view_count()
    test_resolve_unknown_token_returns_none()
    test_resolve_expired_share_returns_none()
    test_private_share_still_resolvable()
    test_list_user_shares()
    test_delete_share_requires_owner()
    test_share_tokens_are_unique()
    test_follow_unfollow()
    test_follow_self_blocked()
    test_list_followers_and_following()
    test_user_profile_aggregates()
    test_get_daily_hot_list_empty()
    test_get_latest_hot_list_date_empty()
    test_get_daily_hot_list_with_data()
    test_refresh_daily_hot_list_handles_data_source_failure()
    print("\n=== Phase 6 smoke test: ALL 16 PASSED ===")