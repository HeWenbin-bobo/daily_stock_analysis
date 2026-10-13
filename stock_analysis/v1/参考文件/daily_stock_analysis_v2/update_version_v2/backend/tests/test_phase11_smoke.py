# -*- coding: utf-8 -*-
"""Phase 11 smoke test: 加密 / 审计 / 限流 / 风险提示"""
import time
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.migrate import migrate
from core.crypto import encrypt_str, decrypt_str, mask_sensitive
from core.audit import record as audit_record, list_recent, cleanup_old
from core.ratelimit import check_limit, reset_limit
from core.security_headers import append_disclaimer


def test_migrate_creates_16_tables():
    n = migrate()
    assert n >= 16, f"Expected at least 16 tables, got {n}"
    print(f"OK migrate: {n} tables")


def test_encrypt_decrypt_roundtrip():
    plain = "webhook_url=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc123-def456"
    enc = encrypt_str(plain)
    dec = decrypt_str(enc)
    assert dec == plain, f"Roundtrip failed: {dec!r} != {plain!r}"
    assert enc != plain, "Encrypted should differ from plain"
    print(f"OK encrypt roundtrip ({len(plain)} -> {len(enc)} chars)")


def test_decrypt_invalid_returns_empty():
    dec = decrypt_str("not-a-valid-fernet-token")
    assert dec == "", f"Invalid token should return empty, got {dec!r}"
    print("OK decrypt invalid returns empty")


def test_mask_sensitive():
    assert mask_sensitive("600519") == "***"
    masked = mask_sensitive("webhook_token_abc123def456ghi789jkl")
    assert masked.startswith("web") and masked.endswith("jkl") and "*" in masked
    print(f"OK mask: {masked}")


def test_audit_record_and_list():
    audit_record(
        action="test_smoke",
        user_id="test_user",
        resource_type="test",
        resource_id="abc",
        ip_address="127.0.0.1",
        request_body={"foo": "bar", "n": 42},
    )
    recent = list_recent(user_id="test_user", limit=5)
    assert len(recent) >= 1
    assert any(r["action"] == "test_smoke" for r in recent)
    print(f"OK audit: recorded + listed {len(recent)} entries")


def test_audit_with_failure():
    audit_record(
        action="test_failure",
        user_id="test_user",
        status="failure",
        error_message="simulated error",
    )
    recent = list_recent(user_id="test_user", limit=10)
    failures = [r for r in recent if r["status"] == "failure"]
    assert len(failures) >= 1
    print(f"OK audit failure: {len(failures)} failure entries")


def test_ratelimit_allows_under_limit():
    reset_limit("127.0.0.1", "/api/v2/test")
    for _ in range(5):
        ok, retry = check_limit("127.0.0.1", "/api/v2/test")
        assert ok, f"Should allow under limit"
    print("OK ratelimit: allows 5/60")


def test_ratelimit_blocks_over_limit():
    reset_limit("127.0.0.1", "/api/v2/notifications/test")
    last_ok = True
    last_retry = 0
    for i in range(10):
        ok, retry = check_limit("127.0.0.1", "/api/v2/notifications/test")
        if i < 5:
            assert ok, f"Should allow first 5"
        else:
            if not ok:
                last_ok = False
                last_retry = retry
                break
    assert not last_ok, f"Should block after 5 (limit is 5/60s)"
    assert 1 <= last_retry <= 62, f"Retry should be in window (allow up to 62 for rounding), got {last_retry}"
    print(f"OK ratelimit: blocks after 5 (retry_after={last_retry}s)")


def test_disclaimer_appended():
    out = append_disclaimer("AI 报告内容")
    assert "本报告" in out and "不构成" in out and "投资建议" in out
    print("OK disclaimer: appended to plain content")


def test_disclaimer_not_duplicated():
    with_disclaimer = "分析报告。\n\n---\n⚠️ **风险提示**：本报告由 AI 模型生成，仅供研究参考，不构成任何投资建议。投资有风险，入市需谨慎。过往业绩不代表未来表现。"
    out = append_disclaimer(with_disclaimer)
    assert out == with_disclaimer, f"Should not double-append, got: {out[:80]}..."
    print("OK disclaimer: no duplicate")


if __name__ == "__main__":
    test_migrate_creates_16_tables()
    test_encrypt_decrypt_roundtrip()
    test_decrypt_invalid_returns_empty()
    test_mask_sensitive()
    test_audit_record_and_list()
    test_audit_with_failure()
    test_ratelimit_allows_under_limit()
    test_ratelimit_blocks_over_limit()
    test_disclaimer_appended()
    test_disclaimer_not_duplicated()
    print("\n=== Phase 11 smoke test: ALL 10 PASSED ===")