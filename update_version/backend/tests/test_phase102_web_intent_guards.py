"""Phase 102: /web-intent/resolve 必须限流 + 限制 message 长度

这是个会触发 LLM 的端点，两处防护都缺失：

1. **限流配置未接线**：`core/ratelimit.LIMITS` 里早就配了
   `"/api/v2/web-intent/resolve": (30, 60)`，但端点从未调用 `check_limit`。
   容器内实测连打 35 次全部 200（限额 30/min，第 31 次起应 429）。
   这类"配置存在但永不生效"最危险 —— 看代码以为已限流。

2. **message 无长度上限**：实测 10 万字符的消息返回 200 并走完整解析流程。
   单条请求就能把极长文本喂进模型，配合限流缺失可用来刷 LLM 配额。

修复后：超限返回 429，超长返回 400（且 400 先于限流判定更友好 ——
这里限流在前，因为限流是全局成本保护，应在解析 payload 之前）。
"""
import os
import sys

import pytest

BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


@pytest.fixture()
def client():
    from db.migrate import migrate

    migrate()
    from fastapi.testclient import TestClient
    from app import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_buckets():
    from core import ratelimit

    with ratelimit._buckets_lock:
        ratelimit._buckets.clear()
    yield
    with ratelimit._buckets_lock:
        ratelimit._buckets.clear()


def test_rate_limit_is_actually_enforced(client):
    """超过 30/min 后必须 429，而不是一直 200"""
    codes = []
    for _ in range(35):
        r = client.post(
            "/api/v2/web-intent/resolve",
            json={"message": "茅台"},
            headers={"X-User-Id": "u"},
        )
        codes.append(r.status_code)

    assert 429 in codes, (
        f"35 次请求全部通过，限流未生效。状态码分布: {sorted(set(codes))}"
    )
    # 前 30 次应放行，之后开始拒绝
    assert codes[:30].count(200) >= 25, f"限流过严，前 30 次状态: {codes[:30]}"
    assert codes[30:].count(429) >= 3, f"第 31 次之后应大量 429，实际: {codes[30:]}"


def test_oversized_message_is_rejected(client):
    """超长 message 应 400，不能进解析流程"""
    from api.endpoints_hot import _WEB_INTENT_MAX_MESSAGE_CHARS

    too_long = "茅" * (_WEB_INTENT_MAX_MESSAGE_CHARS + 1)
    r = client.post(
        "/api/v2/web-intent/resolve",
        json={"message": too_long},
        headers={"X-User-Id": "u"},
    )
    assert r.status_code == 400, f"超长消息应 400，实际 {r.status_code}"
    assert "过长" in r.json()["detail"] or "length" in str(r.json()).lower()


def test_normal_message_still_accepted(client):
    """正常长度的消息不受影响"""
    r = client.post(
        "/api/v2/web-intent/resolve",
        json={"message": "帮我看看茅台最近怎么样"},
        headers={"X-User-Id": "u"},
    )
    assert r.status_code in (200, 500), (
        f"正常消息不应被 400 拦下，实际 {r.status_code}: {r.text[:120]}"
    )
    assert r.status_code != 400


def test_boundary_length_is_allowed(client):
    """恰好等于上限应放行（边界不误杀）"""
    from api.endpoints_hot import _WEB_INTENT_MAX_MESSAGE_CHARS

    exact = "茅" * _WEB_INTENT_MAX_MESSAGE_CHARS
    r = client.post(
        "/api/v2/web-intent/resolve",
        json={"message": exact},
        headers={"X-User-Id": "u"},
    )
    assert r.status_code != 400, f"边界长度被误判为超长: {r.text[:150]}"


def test_non_string_message_still_400(client):
    """原有类型校验不能被破坏"""
    for bad in (123, [1], {"a": 1}, None):
        r = client.post(
            "/api/v2/web-intent/resolve",
            json={"message": bad},
            headers={"X-User-Id": "u"},
        )
        assert r.status_code == 400, f"{bad!r} 应 400，实际 {r.status_code}"
