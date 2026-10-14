# -*- coding: utf-8 -*-
"""Phase 2 smoke test: 通知推送（模板 + 通道 + 加密 + quiet hours + 重试）"""
import time
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrate import get_conn
from core import notifier as nf
from core.crypto import decrypt_str


def _reset_tables():
    conn = get_conn()
    conn.execute("DELETE FROM notification_channel")
    conn.execute("DELETE FROM notification_log")


def test_migrate_tables_exist():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('notification_channel','notification_log')")
    rows = [r["name"] for r in cur.fetchall()]
    assert "notification_channel" in rows
    assert "notification_log" in rows
    print("OK tables exist: notification_channel, notification_log")


def test_template_render_basic():
    rendered = nf.render_template("market_review_daily", {
        "date": "2026-07-30",
        "signal_score": 86,
        "signal_label": "偏暖",
        "up_count": 3430, "down_count": 2000,
        "limit_up": 71, "limit_down": 5,
        "amount": 150.5,
        "top_industries": "1. 电子 5.2%\n2. 通信 3.1%",
        "summary": "市场情绪偏暖",
    })
    assert "2026-07-30" in rendered["title"]
    assert "86" in rendered["content"] and "偏暖" in rendered["content"]
    assert "3430" in rendered["content"] and "2000" in rendered["content"]
    assert "不构成投资建议" in rendered["content"]
    print(f"OK template: {rendered['title']}")


def test_template_render_alert():
    rendered = nf.render_template("alert_triggered", {
        "symbol": "600519", "name": "贵州茅台",
        "rule_name": "开盘30分钟涨幅>3%",
        "metric_value": "3.5%", "threshold_value": "3.0%",
        "trigger_time": "2026-07-30 09:45",
        "commentary": "放量突破，建议关注",
    })
    assert "600519" in rendered["title"]
    assert "贵州茅台" in rendered["content"]
    print(f"OK alert template: {rendered['title']}")


def test_template_render_backtest():
    rendered = nf.render_template("backtest_done", {
        "strategy_name": "MACD 金叉策略",
        "symbol": "600519",
        "start_date": "2024-01-01", "end_date": "2026-07-30",
        "total_return_pct": 45.2, "annualized_return_pct": 18.5,
        "sharpe_ratio": 1.85, "max_drawdown_pct": 12.3,
        "win_rate": 62.5, "total_trades": 48,
    })
    assert "MACD 金叉策略" in rendered["title"]
    assert "45.2%" in rendered["content"]
    assert "1.85" in rendered["content"]
    print(f"OK backtest template: {rendered['title']}")


def test_template_missing_key_does_not_crash():
    rendered = nf.render_template("market_review_daily", {"date": "2026-07-30"})
    assert "2026-07-30" in rendered["title"], f"Title should have date, got {rendered['title']!r}"
    assert "未填字段" in rendered["content"], f"Content should mark missing fields, got {rendered['content'][:80]!r}"
    print(f"OK template missing key: graceful fallback")


def test_add_channel_encrypts_target():
    _reset_tables()
    ch_id = nf.add_channel(
        user_id="u1",
        channel_type="wechat_work",
        target="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123-secret",
    )
    assert ch_id > 0
    # 验证 target 已加密（明文不应在 DB 里）
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT target, target_encrypted FROM notification_channel WHERE id=?", (ch_id,))
    row = cur.fetchone()
    assert "abc123-secret" not in row["target_encrypted"], "target should be encrypted in DB"
    print(f"OK add_channel: id={ch_id}, encrypted={row['target_encrypted'][:40]}...")


def test_add_channel_unique_constraint_update():
    _reset_tables()
    id1 = nf.add_channel("u1", "dingtalk", "https://oapi.dingtalk.com/robot/send?access_token=first")
    id2 = nf.add_channel("u1", "dingtalk", "https://oapi.dingtalk.com/robot/send?access_token=second")
    assert id1 == id2, f"UNIQUE update should return same id, got {id1} vs {id2}"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM notification_channel WHERE user_id='u1'")
    assert cur.fetchone()["cnt"] == 1, f"Should have 1 channel, got {cur.fetchone()['cnt']}"
    print(f"OK add_channel update: id={id1}")


def test_list_channels_masks_target():
    _reset_tables()
    secret = "SCT123456ABCDEFG-secret_key_xyz"
    nf.add_channel("u2", "serverchan", secret)
    channels = nf.list_channels(user_id="u2")
    assert len(channels) == 1
    # ★ Round 4 修复后：明文 target 列不再存储，所以 target 必不包含明文 secret；
    #   掩码函数仍然有效（返回 "" 或 ***+尾段），二者皆可。
    assert secret not in channels[0]["target"], f"Plaintext secret leaked: {channels[0]['target']!r}"
    assert channels[0]["target_encrypted"] == "[ENCRYPTED]"
    print(f"OK list: target={channels[0]['target']!r} (no plaintext leak)")


def test_send_to_disabled_channel():
    _reset_tables()
    ch_id = nf.add_channel("u3", "wechat_work", "https://example.com/webhook", enabled=False)
    result = nf.send(ch_id, title="test", content="hello")
    assert not result.success, f"Should fail for disabled channel"
    assert "disabled" in (result.error_message or "")
    print(f"OK disabled channel blocked: {result.error_message}")


def test_send_unknown_channel():
    result = nf.send(99999, title="x", content="y")
    assert not result.success
    assert "不存在" in (result.error_message or "")
    print(f"OK unknown channel blocked")


def test_send_to_unreachable_webhook_records_failure():
    """推送到不可达 URL，应记录 failed 状态（3 次重试）"""
    _reset_tables()
    # ★ Round 20：add_channel 现在有 SSRF 校验，loopback 目标会被拒（正确）。
    #   本测试聚焦"发送失败 + 重试 + 日志"管线，直接插 DB 构造不可达通道。
    from core.notifier import encrypt_str
    conn = get_conn()
    now = time.time()
    conn.execute(
        """INSERT INTO notification_channel
           (user_id, channel_type, target, target_encrypted, enabled,
            quiet_hours_start, quiet_hours_end, created_at, updated_at)
           VALUES (?, ?, '', ?, 1, NULL, NULL, ?, ?)""",
        ("u4", "wechat_work", encrypt_str("https://127.0.0.1:1/nonexistent"), now, now),
    )
    ch_id = conn.execute("SELECT id FROM notification_channel WHERE user_id='u4'").fetchone()["id"]
    result = nf.send(ch_id, title="test", content="hello", max_retries=2)
    assert not result.success
    assert result.retry_count >= 1, f"Should have retried, got {result.retry_count}"
    # 验证 notification_log 写入了 failed
    logs = nf.list_logs(user_id="u4")
    assert len(logs) == 1
    assert logs[0]["status"] == "failed"
    assert "127.0.0.1" not in logs[0]["title"]  # 不应泄露 URL
    print(f"OK unreachable: retries={result.retry_count}, log_status={logs[0]['status']}")


def test_send_missing_title_content_returns_error():
    _reset_tables()
    ch_id = nf.add_channel("u5", "dingtalk", "https://example.com/hook")
    result = nf.send(ch_id)  # 没有 title/content/template_name
    assert not result.success
    assert "缺失" in (result.error_message or "")
    print(f"OK missing title blocked: {result.error_message}")


def test_quiet_hours_skips_push():
    _reset_tables()
    # 设置 00:00-23:59 全天静默
    ch_id = nf.add_channel(
        "u6", "wechat_work", "https://example.com/hook",
        quiet_hours_start="00:00", quiet_hours_end="23:59",
    )
    result = nf.send(ch_id, title="t", content="c")
    assert not result.success
    assert "quiet" in (result.error_message or "")
    # 验证日志记录 status=skipped
    logs = nf.list_logs(user_id="u6")
    assert logs[0]["status"] == "skipped"
    print(f"OK quiet hours: skipped (status={logs[0]['status']})")


def test_delete_channel():
    _reset_tables()
    ch_id = nf.add_channel("u7", "serverchan", "SCTkey123")
    ok = nf.delete_channel(ch_id)
    assert ok
    assert nf.delete_channel(ch_id) is False
    print(f"OK delete: id={ch_id}")


def test_log_limit_and_status_filter():
    _reset_tables()
    # ★ Round 20：SSRF 校验拒绝 loopback 通道 → 直接插 DB 测试发送日志管线
    from core.notifier import encrypt_str
    conn = get_conn()
    now = time.time()
    conn.execute(
        """INSERT INTO notification_channel
           (user_id, channel_type, target, target_encrypted, enabled,
            quiet_hours_start, quiet_hours_end, created_at, updated_at)
           VALUES (?, ?, '', ?, 1, NULL, NULL, ?, ?)""",
        ("u8", "wechat_work", encrypt_str("https://127.0.0.1:1/hook"), now, now),
    )
    ch_id = conn.execute("SELECT id FROM notification_channel WHERE user_id='u8'").fetchone()["id"]
    for _ in range(5):
        nf.send(ch_id, title="t", content="c", max_retries=1)
    all_logs = nf.list_logs(user_id="u8", limit=10)
    failed_logs = nf.list_logs(user_id="u8", status="failed")
    assert len(all_logs) == 5
    assert len(failed_logs) == 5
    assert all(l["status"] == "failed" for l in failed_logs)
    print(f"OK logs: all={len(all_logs)}, failed={len(failed_logs)}")


def test_template_alert_renders_all_required_fields():
    rendered = nf.render_template("paper_settle", {
        "date": "2026-07-30",
        "account_name": "测试账户",
        "total_equity": 1050000,
        "positions_value": 850000,
        "cash": 200000,
        "day_pnl_pct": 1.5,
    })
    assert "测试账户" in rendered["content"]
    assert "1,050,000" in rendered["content"] or "1050000" in rendered["content"]
    print(f"OK paper_settle template: {rendered['title']}")


if __name__ == "__main__":
    test_migrate_tables_exist()
    test_template_render_basic()
    test_template_render_alert()
    test_template_render_backtest()
    test_template_missing_key_does_not_crash()
    test_add_channel_encrypts_target()
    test_add_channel_unique_constraint_update()
    test_list_channels_masks_target()
    test_send_to_disabled_channel()
    test_send_unknown_channel()
    test_send_to_unreachable_webhook_records_failure()
    test_send_missing_title_content_returns_error()
    test_quiet_hours_skips_push()
    test_delete_channel()
    test_log_limit_and_status_filter()
    test_template_alert_renders_all_required_fields()
    print("\n=== Phase 2 smoke test: ALL 16 PASSED ===")