"""Security helpers / admin auth regression tests.

覆盖:
  - services.security 输入校验函数
  - admin 鉴权 dependency (默认 localhost 放行, ADMIN_API_KEY 后强制 Bearer)
  - 端点级集成: 拒超长/非法输入, 错误响应不泄露 key 内容
  - Pydantic schema 上对 stock_code / extra_prompt 的约束
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

# 在 import app 前确保 FORCE_MOCK
os.environ.setdefault("FORCE_MOCK", "1")

import app as app_module  # noqa: E402
from middlewares.rate_limit import rate_limiter  # noqa: E402
from schemas.stocks import LLMInsightRequest, PeakTroughRequest  # noqa: E402
from security import (  # noqa: E402
    LLM_EXTRA_PROMPT_MAX_LENGTH,
    SEARCH_Q_MAX_LENGTH,
    STOCK_CODE_MAX_LENGTH,
    normalize_and_validate_stock_code,
    require_admin,
    validate_extra_prompt,
    validate_search_q,
)


# ---------------------------------------------------------------------------
# 输入校验函数
# ---------------------------------------------------------------------------

class TestValidateStockCode:
    def test_valid_cn(self):
        assert normalize_and_validate_stock_code("600519") == "600519"

    def test_valid_cn_with_sh_suffix(self):
        assert normalize_and_validate_stock_code("600519.SS") == "600519.SS"

    def test_valid_cn_with_sz_suffix(self):
        assert normalize_and_validate_stock_code("000001.SZ") == "000001.SZ"

    def test_valid_hk_short(self):
        assert normalize_and_validate_stock_code("00700") == "00700"

    def test_valid_hk_with_hk_suffix(self):
        assert normalize_and_validate_stock_code("00700.HK") == "00700.HK"

    def test_valid_us(self):
        assert normalize_and_validate_stock_code("AAPL") == "AAPL"

    def test_valid_brk_b(self):
        assert normalize_and_validate_stock_code("BRK-B") == "BRK-B"

    def test_lowercase_normalized(self):
        assert normalize_and_validate_stock_code("aapl") == "AAPL"

    def test_stripped(self):
        assert normalize_and_validate_stock_code("  600519  ") == "600519"

    def test_empty_rejected(self):
        with pytest.raises(HTTPException) as exc:
            normalize_and_validate_stock_code("")
        assert exc.value.status_code == 400

    def test_whitespace_only_rejected(self):
        with pytest.raises(HTTPException) as exc:
            normalize_and_validate_stock_code("   ")
        assert exc.value.status_code == 400

    def test_too_long_rejected(self):
        long_code = "A" * (STOCK_CODE_MAX_LENGTH + 1)
        with pytest.raises(HTTPException) as exc:
            normalize_and_validate_stock_code(long_code)
        assert exc.value.status_code == 400
        assert "长度超限" in exc.value.detail

    def test_too_many_alphanum_rejected(self):
        # 21 个字母数字, 超过 18 限制 + 总长度超过 20
        with pytest.raises(HTTPException) as exc:
            normalize_and_validate_stock_code("A" * 21)
        assert exc.value.status_code == 400
        assert "长度超限" in exc.value.detail

    def test_control_char_rejected(self):
        with pytest.raises(HTTPException) as exc:
            normalize_and_validate_stock_code("600519\x00")
        assert exc.value.status_code == 400

    def test_special_char_rejected(self):
        with pytest.raises(HTTPException) as exc:
            normalize_and_validate_stock_code("600519;DROP")
        assert exc.value.status_code == 400

    def test_internal_whitespace_rejected(self):
        with pytest.raises(HTTPException) as exc:
            normalize_and_validate_stock_code("600 519")
        assert exc.value.status_code == 400


class TestValidateSearchQ:
    def test_empty_allowed(self):
        assert validate_search_q("") == ""

    def test_none_allowed(self):
        assert validate_search_q(None) == ""

    def test_normal_text(self):
        assert validate_search_q("600519") == "600519"

    def test_chinese(self):
        assert validate_search_q("贵州茅台") == "贵州茅台"

    def test_stripped(self):
        assert validate_search_q("  苹果  ") == "苹果"

    def test_too_long_rejected(self):
        long_q = "x" * (SEARCH_Q_MAX_LENGTH + 1)
        with pytest.raises(HTTPException) as exc:
            validate_search_q(long_q)
        assert exc.value.status_code == 400

    def test_control_char_rejected(self):
        with pytest.raises(HTTPException) as exc:
            validate_search_q("abc\x00def")
        assert exc.value.status_code == 400


class TestValidateExtraPrompt:
    def test_none(self):
        assert validate_extra_prompt(None) is None

    def test_empty_string(self):
        assert validate_extra_prompt("") is None

    def test_whitespace_only(self):
        assert validate_extra_prompt("   \n  ") is None

    def test_normal_text(self):
        assert validate_extra_prompt("关注 MACD") == "关注 MACD"

    def test_too_long_rejected(self):
        long_p = "x" * (LLM_EXTRA_PROMPT_MAX_LENGTH + 1)
        with pytest.raises(HTTPException) as exc:
            validate_extra_prompt(long_p)
        assert exc.value.status_code == 400

    def test_control_char_rejected(self):
        with pytest.raises(HTTPException) as exc:
            validate_extra_prompt("hello\x00world")
        assert exc.value.status_code == 400

    def test_newline_allowed(self):
        assert validate_extra_prompt("line1\nline2") == "line1\nline2"


# ---------------------------------------------------------------------------
# Schema 校验
# ---------------------------------------------------------------------------

class TestPeakTroughRequestSchema:
    def test_valid_stock_code(self):
        req = PeakTroughRequest(stock_code="600519")
        assert req.stock_code == "600519"

    def test_invalid_stock_code_rejected(self):
        with pytest.raises(ValidationError):
            PeakTroughRequest(stock_code="600519;DROP")

    def test_too_long_stock_code_rejected(self):
        with pytest.raises(ValidationError):
            PeakTroughRequest(stock_code="A" * (STOCK_CODE_MAX_LENGTH + 1))


class TestLLMInsightRequestSchema:
    def test_extra_prompt_too_long_rejected(self):
        with pytest.raises(ValidationError):
            LLMInsightRequest(
                stock_code="600519",
                extra_prompt="x" * (LLM_EXTRA_PROMPT_MAX_LENGTH + 1),
            )

    def test_extra_prompt_at_boundary(self):
        prompt = "x" * LLM_EXTRA_PROMPT_MAX_LENGTH
        req = LLMInsightRequest(stock_code="600519", extra_prompt=prompt)
        assert len(req.extra_prompt) == LLM_EXTRA_PROMPT_MAX_LENGTH

    def test_extra_prompt_with_control_char_rejected(self):
        with pytest.raises(ValidationError):
            LLMInsightRequest(stock_code="600519", extra_prompt="hi\x00")


# ---------------------------------------------------------------------------
# Admin 鉴权 - dependency 层
# ---------------------------------------------------------------------------

class TestRequireAdminDependency:
    """直接测试 require_admin 函数 (dependency)。"""

    def _make_request(self, host: str = "127.0.0.1", auth: str | None = None):
        from starlette.requests import Request

        headers = []
        if auth is not None:
            headers.append((b"authorization", auth.encode()))
        scope = {
            "type": "http",
            "headers": headers,
            "client": (host, 12345),
            "method": "GET",
            "path": "/admin/stats",
            "query_string": b"",
        }
        return Request(scope)

    def test_loopback_allowed_when_no_key(self, monkeypatch):
        monkeypatch.delenv("ADMIN_API_KEY", raising=False)
        # 不应抛异常
        require_admin(self._make_request(host="127.0.0.1"))

    def test_ipv6_loopback_allowed(self, monkeypatch):
        monkeypatch.delenv("ADMIN_API_KEY", raising=False)
        require_admin(self._make_request(host="::1"))

    def test_non_loopback_denied_when_no_key(self, monkeypatch):
        monkeypatch.delenv("ADMIN_API_KEY", raising=False)
        with pytest.raises(HTTPException) as exc:
            require_admin(self._make_request(host="10.0.0.5"))
        assert exc.value.status_code == 403

    def test_key_configured_requires_bearer(self, monkeypatch):
        monkeypatch.setenv("ADMIN_API_KEY", "secret-token-123")
        try:
            with pytest.raises(HTTPException) as exc:
                require_admin(self._make_request(host="127.0.0.1"))
            assert exc.value.status_code == 401
        finally:
            monkeypatch.delenv("ADMIN_API_KEY", raising=False)

    def test_key_configured_wrong_bearer(self, monkeypatch):
        monkeypatch.setenv("ADMIN_API_KEY", "secret-token-123")
        try:
            with pytest.raises(HTTPException) as exc:
                require_admin(self._make_request(host="127.0.0.1", auth="Bearer wrong"))
            assert exc.value.status_code == 401
            # 关键: 错误响应不泄露 key 任何内容
            assert "secret-token-123" not in exc.value.detail
        finally:
            monkeypatch.delenv("ADMIN_API_KEY", raising=False)

    def test_key_configured_correct_bearer(self, monkeypatch):
        monkeypatch.setenv("ADMIN_API_KEY", "secret-token-123")
        try:
            # 不应抛异常
            require_admin(self._make_request(host="127.0.0.1", auth="Bearer secret-token-123"))
        finally:
            monkeypatch.delenv("ADMIN_API_KEY", raising=False)

    def test_key_configured_non_loopback_still_denied_without_bearer(self, monkeypatch):
        monkeypatch.setenv("ADMIN_API_KEY", "secret-token-123")
        try:
            with pytest.raises(HTTPException) as exc:
                require_admin(self._make_request(host="10.0.0.5"))
            assert exc.value.status_code == 401
        finally:
            monkeypatch.delenv("ADMIN_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# 端点级集成测试
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    rate_limiter.reset()
    return TestClient(app_module.app)


class TestInputValidationEndpoints:
    def test_search_too_long_returns_422(self, client):
        # FastAPI Query(max_length=...) 在 Pydantic 校验阶段就拦下, 返回 422
        long_q = "x" * (SEARCH_Q_MAX_LENGTH + 1)
        r = client.get(f"/api/v1/stocks/search?q={long_q}")
        assert r.status_code == 422

    def test_search_chinese_normal(self, client):
        r = client.get("/api/v1/stocks/search?q=茅台")
        assert r.status_code == 200

    def test_history_invalid_stock_code_returns_400(self, client):
        # 含特殊字符 (;DROP) → 格式不合法
        r = client.get("/api/v1/stocks/600519%3BDROP/history?period=daily&days=60")
        assert r.status_code == 400

    def test_history_stock_code_too_long_returns_400(self, client):
        long_code = "A" * (STOCK_CODE_MAX_LENGTH + 1)
        r = client.get(f"/api/v1/stocks/{long_code}/history?period=daily&days=60")
        # FastAPI path 长度限制先于我们的校验 → 404
        assert r.status_code in (400, 404)

    def test_quote_invalid_stock_code_returns_400(self, client):
        r = client.get("/api/v1/stocks/600519%3BDROP/quote")
        assert r.status_code == 400

    def test_indicators_invalid_stock_code_returns_400(self, client):
        r = client.get("/api/v1/stocks/600519%3BDROP/indicators/MACD")
        assert r.status_code == 400

    def test_peak_trough_invalid_stock_code_returns_422(self, client):
        r = client.post(
            "/api/v1/analysis/peak-trough",
            json={"stock_code": "600519;DROP", "period": "daily", "days": 60},
        )
        assert r.status_code == 422

    def test_llm_insight_extra_prompt_too_long_returns_422(self, client):
        rate_limiter.reset()
        long_p = "x" * (LLM_EXTRA_PROMPT_MAX_LENGTH + 1)
        r = client.post(
            "/api/v1/analysis/llm-insight",
            json={"stock_code": "600519", "extra_prompt": long_p},
        )
        assert r.status_code == 422


class TestAdminEndpointsLocalhost:
    """TestClient 默认使用 localhost/127.0.0.1, 因此默认放行。"""

    def test_admin_stats_works(self, client):
        client.get("/api/v1/stocks/600519/history?period=daily&days=30")
        r = client.get("/admin/stats")
        assert r.status_code == 200

    def test_admin_stats_reset_works(self, client):
        r = client.post("/admin/stats/reset")
        assert r.status_code == 200

    def test_llm_status_works(self, client):
        r = client.get("/api/v1/analysis/llm-status")
        assert r.status_code == 200

    def test_llm_insight_works(self, client):
        rate_limiter.reset()
        r = client.post(
            "/api/v1/analysis/llm-insight",
            json={"stock_code": "600519"},
        )
        assert r.status_code == 200


class TestAdminEndpointsWithKey:
    """配置 ADMIN_API_KEY 后, 敏感端点必须 Bearer。"""

    def test_no_bearer_returns_401(self, monkeypatch, client):
        monkeypatch.setenv("ADMIN_API_KEY", "k1")
        r = client.get("/admin/stats")
        assert r.status_code == 401
        # 响应不泄露 key 内容
        assert "k1" not in r.text

    def test_wrong_bearer_returns_401(self, monkeypatch, client):
        monkeypatch.setenv("ADMIN_API_KEY", "k1")
        r = client.get("/admin/stats", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
        assert "k1" not in r.text

    def test_correct_bearer_succeeds(self, monkeypatch, client):
        monkeypatch.setenv("ADMIN_API_KEY", "k1")
        r = client.get("/admin/stats", headers={"Authorization": "Bearer k1"})
        assert r.status_code == 200

    def test_no_bearer_llm_status_401(self, monkeypatch, client):
        monkeypatch.setenv("ADMIN_API_KEY", "k1")
        r = client.get("/api/v1/analysis/llm-status")
        assert r.status_code == 401

    def test_correct_bearer_llm_insight_200(self, monkeypatch, client):
        monkeypatch.setenv("ADMIN_API_KEY", "k1")
        rate_limiter.reset()
        r = client.post(
            "/api/v1/analysis/llm-insight",
            json={"stock_code": "600519"},
            headers={"Authorization": "Bearer k1"},
        )
        assert r.status_code == 200

    def test_non_bearer_scheme_rejected(self, monkeypatch, client):
        monkeypatch.setenv("ADMIN_API_KEY", "k1")
        r = client.get("/admin/stats", headers={"Authorization": "Basic Zm9vOmJhcg=="})
        assert r.status_code == 401

    def test_admin_reset_requires_bearer(self, monkeypatch, client):
        monkeypatch.setenv("ADMIN_API_KEY", "k1")
        r = client.post("/admin/stats/reset")
        assert r.status_code == 401
        r2 = client.post("/admin/stats/reset", headers={"Authorization": "Bearer k1"})
        assert r2.status_code == 200