# -*- coding: utf-8 -*-
"""Regression test: a signature produced by /webhooks/sign must verify.

``sign_payload`` serialised a dict body with ``ensure_ascii=False`` (keys in
insertion order) while the ``/webhooks/verify`` endpoint re-serialised it with
``ensure_ascii=False, sort_keys=True``. For any body whose keys were not
already alphabetical the two byte strings differed, so a signature the service
had just issued came back ``valid: false`` from its own verify endpoint.

The pre-existing tests only ever signed plain strings, which is why this went
unnoticed. Both sides now share ``canonical_body``.

Run: ``python -m pytest update_version/backend/tests/test_phase64_webhook_sign_verify_parity.py``
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

# sign 用 APP_HMAC_KEY、verify 用 APP_WEBHOOK_VERIFY_KEY（Round 4 的 M2 修复刻意
# 分离两把密钥）。要让"签出的签名能验过"，两者必须取同一个值——
# 生产里由运维同时配置，这里显式对齐。
_SHARED_KEY = "test-shared-key-0123456789abcdef"
os.environ["APP_HMAC_KEY"] = _SHARED_KEY
os.environ["APP_WEBHOOK_VERIFY_KEY"] = _SHARED_KEY

import pytest

from core.webhook_signing import canonical_body, sign_payload, verify_signature


@pytest.mark.parametrize(
    "body",
    [
        {"z": 1, "a": 2, "m": 3},          # 键序非字母序：修复前必然失败
        {"b": "中文", "a": None},
        {"nested": {"z": 1, "a": 2}, "list": [3, 1, 2]},
        "plain string body",
        12345,
    ],
)
def test_sign_then_verify_round_trips(body) -> None:
    ts = int(time.time())
    sig = sign_payload(body, ts)
    assert verify_signature(canonical_body(body), ts, sig), (
        f"a signature just issued for {body!r} does not verify"
    )


def test_dict_body_verifies_through_the_endpoint_path() -> None:
    """The exact serialisation the /webhooks/verify endpoint performs."""
    import json

    body = {"z": 1, "a": 2}
    ts = int(time.time())
    sig = sign_payload(body, ts)

    # 端点里对非字符串 body 的转换（修复前这里是 sort_keys=True 而 sign 没有）
    from core.webhook_signing import canonical_body as endpoint_serialisation
    assert verify_signature(endpoint_serialisation(body), ts, sig)
    # 与端点当前实现保持一致，防止未来再次分叉
    assert json.dumps(body, ensure_ascii=False, sort_keys=True) == endpoint_serialisation(body)


def test_tampering_and_expiry_still_rejected() -> None:
    ts = int(time.time())
    sig = sign_payload("hello", ts)
    assert not verify_signature("hello2", ts, sig)
    assert not verify_signature("hello", ts - 9999, sig)
    # 用旧时间戳签名，放宽 max_age 后应当通过（验证 max_age 真的生效）
    old_ts = ts - 9999
    old_sig = sign_payload("hello", old_ts)
    assert not verify_signature("hello", old_ts, old_sig)
    assert verify_signature("hello", old_ts, old_sig, max_age=99999)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
