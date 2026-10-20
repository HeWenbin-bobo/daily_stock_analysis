# -*- coding: utf-8 -*-
"""Regression test: audit redaction must catch credential key variants.

``_redact_body`` matched the field name exactly against a small set, so the
docstring's promise ("avoid /audit/recent leaking webhook URLs / SMTP passwords
/ API keys") only held for those spellings. Verified before the fix:
``api_keys``, ``access_token``, ``smtp_password``, ``apiKey``, ``private_key``
all came through verbatim.

The point is to be broad without becoming noise: ``keyboard``, ``monkey`` and
ordinary business fields must stay readable.

Run: ``python -m pytest update_version/backend/tests/test_phase70_audit_redaction.py``
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from core.audit import _key_is_sensitive, _redact_body


@pytest.mark.parametrize(
    "key",
    [
        "password", "passwd", "pwd", "token", "secret",
        "api_key", "api_keys", "access_token", "refresh_token",
        "smtp_password", "private_key", "secretKey",
        "apiKey", "X-Api-Key", "Authorization", "auth", "credentials",
        "webhook_url", "target",
    ],
)
def test_credential_keys_are_redacted(key: str) -> None:
    assert _key_is_sensitive(key), f"{key!r} should be treated as a credential"
    assert _redact_body({key: "s3cr3t"})[key] == "***"


@pytest.mark.parametrize(
    "key",
    ["symbol", "quantity", "cost", "user_id", "name", "rule_type", "keyboard", "monkey", "channel_type"],
)
def test_ordinary_keys_stay_readable(key: str) -> None:
    assert not _key_is_sensitive(key), f"{key!r} is not a credential"
    assert _redact_body({key: "value"})[key] == "value"


def test_nested_and_listed_bodies_are_redacted() -> None:
    body = {
        "user_id": "u1",
        "channel": {"apiKey": "sk-live", "type": "wechat_work"},
        "attempts": [{"token": "t1"}, {"token": "t2"}],
    }
    out = _redact_body(body)
    assert out["user_id"] == "u1"
    assert out["channel"]["apiKey"] == "***"
    assert out["channel"]["type"] == "wechat_work"
    assert all(item["token"] == "***" for item in out["attempts"])


def test_redaction_does_not_mutate_the_input() -> None:
    body = {"password": "p", "symbol": "600519"}
    snapshot = dict(body)
    _redact_body(body)
    assert body == snapshot, "_redact_body must not mutate the caller's dict"


def test_non_string_keys_do_not_crash() -> None:
    """JSON bodies can carry int keys; the old code called .lower() on them."""
    out = _redact_body({1: "a", "token": "b"})
    assert out[1] == "a"
    assert out["token"] == "***"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
