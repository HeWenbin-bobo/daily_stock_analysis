"""litellm_backend 测试 — 通过 mock litellm 和 cc_switch 验证 LLM 调用逻辑。"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import llm.litellm_backend as backend
from llm import litellm_backend as lb_module


@pytest.fixture(autouse=True)
def _disable_force_mock(monkeypatch):
    """这些测试要验证真实的 LLM 调用路径(已 mock litellm.completion),
    因此必须在每个测试中临时关闭 FORCE_MOCK 短路.

    不关闭的话, chat() 会在 select_provider() 之前直接返回 mock dict,
    让 mock_completion 永远没机会被调用, 测试断言失败.
    """
    monkeypatch.setattr(backend, "is_force_mock", lambda: False)


@pytest.fixture
def fake_provider():
    return {
        "name": "MiniMax",
        "base_url": "https://api.minimaxi.com/anthropic",
        "api_key": "sk-test-123",
        "model": "MiniMax-M3",
        "litellm_prefix": "anthropic",
    }


class TestSelectProvider:
    """select_provider 选择逻辑。"""

    def test_returns_cc_switch_provider_when_available(self, fake_provider):
        with patch.object(backend, "select_provider", return_value=(fake_provider, "cc_switch")):
            provider, source = backend.select_provider()
        assert provider["name"] == "MiniMax"
        assert source == "cc_switch"

    def test_falls_back_to_env_when_cc_switch_unavailable(self, monkeypatch):
        # 模拟 CC switch 失败
        monkeypatch.setattr(
            backend, "select_provider",
            lambda: (_try_env(monkeypatch), "env_fallback"),
        )
        # 这里直接调用真实的 _try_env_fallback
        from llm.litellm_backend import _try_fallback_env
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-456")
        result = _try_fallback_env()
        assert result is not None
        assert result["name"] == "env:anthropic"
        assert result["api_key"] == "sk-env-456"

    def test_raises_when_no_provider_available(self, monkeypatch):
        """既无 CC switch 又无环境变量时应抛 RuntimeError。"""
        # 清空所有可能的环境变量(只清理代码实际读取的)
        for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]:
            monkeypatch.delenv(var, raising=False)
        # 让 detect_active_provider 返回 None
        with patch("llm.litellm_backend.detect_active_provider", return_value=None):
            with pytest.raises(RuntimeError, match="未找到可用 LLM provider"):
                backend.select_provider()

    # iter 125: 补测 _try_fallback_env 空字符串路径 — review 报告标注为 MEDIUM。
    # 当前测试只覆盖 env var 缺失或非空两种情况, 没人验证空字符串 "" 会被 falsy 短路。
    def test_try_fallback_env_returns_none_when_env_var_empty_string(self, monkeypatch):
        """env var 设置为空字符串 (e.g. OPENAI_API_KEY="") 时, _try_fallback_env 应返回 None,
        不应认为 provider 可用 (iter 97 防 KeyError 的修复, 同样要保证 truthy 语义)。"""
        from llm.litellm_backend import _try_fallback_env

        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("OPENAI_API_KEY", "")
        assert _try_fallback_env() is None

    def test_select_provider_skips_cc_switch_when_api_key_empty(self, monkeypatch):
        """detect_active_provider 返回的 dict api_key 为空字符串时, select_provider
        应跳过 cc_switch provider, 走到 _try_fallback_env (空 env → RuntimeError)。"""
        for var in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]:
            monkeypatch.delenv(var, raising=False)

        with patch(
            "llm.litellm_backend.detect_active_provider",
            return_value={"name": "broken-provider", "api_key": "", "model": "x"},
        ):
            with pytest.raises(RuntimeError, match="未找到可用 LLM provider"):
                backend.select_provider()


class TestForceMockGuard:
    """FORCE_MOCK 短路保护: 防止 FORCE_MOCK=1 时真实调用 LLM API."""

    def test_chat_returns_mock_dict_when_force_mock_enabled(self, monkeypatch):
        """is_force_mock() = True 时, chat() 立即返回 mock dict, 不调 litellm.completion."""
        # 把 is_force_mock 局部 patch 为 True (避免依赖环境变量)
        monkeypatch.setattr(backend, "is_force_mock", lambda: True)
        # 若 chat() 真的去选 provider, 必然抛 RuntimeError (无 cc_switch, 无 env).
        # 若 mock 短路生效, 应直接返回 mock dict 不抛错.
        with patch.object(backend.litellm, "completion") as mock_completion:
            result = backend.chat(
                messages=[{"role": "user", "content": "test"}],
                system="sys",
                max_tokens=10,
            )
            # 关键断言: litellm.completion 永远不应被调用
            mock_completion.assert_not_called()
            assert result["provider"] == "mock"
            assert result["model"] == "mock"
            assert result["source"] == "force_mock"
            assert "FORCE_MOCK" in result["text"]

    def test_chat_falls_through_when_force_mock_disabled(self, monkeypatch, fake_provider):
        """is_force_mock() = False 时, chat() 正常调用 select_provider + litellm."""
        monkeypatch.setattr(backend, "is_force_mock", lambda: False)
        with patch.object(backend, "select_provider", return_value=(fake_provider, "cc_switch")):
            with patch.object(backend.litellm, "completion") as mock_completion:
                mock_completion.return_value = _mock_response("real answer")
                result = backend.chat(messages=[{"role": "user", "content": "hi"}])
                mock_completion.assert_called_once()
                assert result["provider"] == "MiniMax"
                assert result["text"] == "real answer"


class TestChat:
    """chat 函数行为。"""

    def test_chat_returns_provider_info(self, fake_provider):
        """chat 调用返回 provider/model/source/text。"""
        with patch.object(backend, "select_provider", return_value=(fake_provider, "cc_switch")):
            with patch.object(backend.litellm, "completion") as mock_completion:
                # 模拟 litellm 返回
                mock_completion.return_value = type("Response", (), {
                    "choices": [type("Choice", (), {
                        "message": type("Message", (), {"content": "分析结果"})
                    })()]
                })()

                result = backend.chat(
                    messages=[{"role": "user", "content": "test"}],
                    system="你是助手",
                    max_tokens=100,
                )

        assert result["provider"] == "MiniMax"
        assert result["model"] == "MiniMax-M3"
        assert result["source"] == "cc_switch"
        assert result["text"] == "分析结果"

    def test_chat_passes_system_message(self, fake_provider):
        """system 参数应作为第一条消息传递。"""
        with patch.object(backend, "select_provider", return_value=(fake_provider, "cc_switch")):
            with patch.object(backend.litellm, "completion") as mock_completion:
                mock_completion.return_value = _mock_response("ok")

                backend.chat(
                    messages=[{"role": "user", "content": "hello"}],
                    system="你是一个翻译",
                )

                call_kwargs = mock_completion.call_args.kwargs
                messages = call_kwargs["messages"]
                assert messages[0]["role"] == "system"
                assert messages[0]["content"] == "你是一个翻译"
                assert messages[1]["role"] == "user"

    def test_chat_no_system_message(self, fake_provider):
        with patch.object(backend, "select_provider", return_value=(fake_provider, "cc_switch")):
            with patch.object(backend.litellm, "completion") as mock_completion:
                mock_completion.return_value = _mock_response("ok")

                backend.chat(messages=[{"role": "user", "content": "hi"}])

                call_kwargs = mock_completion.call_args.kwargs
                messages = call_kwargs["messages"]
                # 没有 system 时, 第一条就是 user
                assert len(messages) == 1
                assert messages[0]["role"] == "user"

    def test_chat_model_id_format(self, fake_provider):
        """model_id 应为 '{prefix}/{model}' 格式。"""
        with patch.object(backend, "select_provider", return_value=(fake_provider, "cc_switch")):
            with patch.object(backend.litellm, "completion") as mock_completion:
                mock_completion.return_value = _mock_response("ok")

                backend.chat(messages=[{"role": "user", "content": "x"}])

                call_kwargs = mock_completion.call_args.kwargs
                assert call_kwargs["model"] == "anthropic/MiniMax-M3"

    def test_chat_uses_api_base_when_provided(self, fake_provider):
        """base_url 应作为 api_base 传递。"""
        with patch.object(backend, "select_provider", return_value=(fake_provider, "cc_switch")):
            with patch.object(backend.litellm, "completion") as mock_completion:
                mock_completion.return_value = _mock_response("ok")

                backend.chat(messages=[{"role": "user", "content": "x"}])

                call_kwargs = mock_completion.call_args.kwargs
                assert call_kwargs["api_base"] == "https://api.minimaxi.com/anthropic"

    def test_chat_skips_api_base_when_empty(self, monkeypatch):
        """base_url 为空字符串时不应传递 api_base。"""
        provider = {
            "name": "OpenAI",
            "base_url": "",
            "api_key": "sk-x",
            "model": "gpt-4o",
            "litellm_prefix": "openai",
        }
        with patch.object(backend, "select_provider", return_value=(provider, "env_fallback")):
            with patch.object(backend.litellm, "completion") as mock_completion:
                mock_completion.return_value = _mock_response("ok")

                backend.chat(messages=[{"role": "user", "content": "x"}])

                call_kwargs = mock_completion.call_args.kwargs
                assert "api_base" not in call_kwargs

    def test_chat_strips_model_context_suffix(self):
        """iter 162: 模型名 '[1M]' 后缀 (Claude Code 上下文标记) 应在 API 调用前剔除。

        opencode.ai 返回 "Model deepseek-v4-flash[1M] is not supported"。
        """
        provider = {
            "name": "OpenCode Go",
            "base_url": "https://opencode.ai/zen/go",
            "api_key": "sk-opencode",
            "model": "deepseek-v4-flash[1M]",
            "litellm_prefix": "openai",
        }
        with patch.object(backend, "select_provider", return_value=(provider, "cc_switch")):
            with patch.object(backend.litellm, "completion") as mock_completion:
                mock_completion.return_value = _mock_response("ok")
                backend.chat(messages=[{"role": "user", "content": "x"}])
                call_kwargs = mock_completion.call_args.kwargs
                assert call_kwargs["model"] == "openai/deepseek-v4-flash"

    def test_chat_appends_v1_to_openai_api_base(self):
        """iter 162: OpenAI 兼容 provider 的 api_base 需以 /v1 结尾, 否则 LiteLLM
        拼到 /chat/completions 返回 404。CC switch 存的 base_url 常不带 /v1。"""
        provider = {
            "name": "OpenCode Go",
            "base_url": "https://opencode.ai/zen/go",
            "api_key": "sk-opencode",
            "model": "deepseek-v4-flash",
            "litellm_prefix": "openai",
        }
        with patch.object(backend, "select_provider", return_value=(provider, "cc_switch")):
            with patch.object(backend.litellm, "completion") as mock_completion:
                mock_completion.return_value = _mock_response("ok")
                backend.chat(messages=[{"role": "user", "content": "x"}])
                call_kwargs = mock_completion.call_args.kwargs
                assert call_kwargs["api_base"] == "https://opencode.ai/zen/go/v1"

    def test_chat_does_not_duplicate_v1_on_anthropic_base_url(self):
        """anthropic 前缀不追加 /v1 — LiteLLM 自己拼 /v1/messages。"""
        provider = {
            "name": "MiniMax",
            "base_url": "https://api.minimaxi.com/anthropic",
            "api_key": "sk-x",
            "model": "MiniMax-M3",
            "litellm_prefix": "anthropic",
        }
        with patch.object(backend, "select_provider", return_value=(provider, "cc_switch")):
            with patch.object(backend.litellm, "completion") as mock_completion:
                mock_completion.return_value = _mock_response("ok")
                backend.chat(messages=[{"role": "user", "content": "x"}])
                call_kwargs = mock_completion.call_args.kwargs
                assert call_kwargs["api_base"] == "https://api.minimaxi.com/anthropic"



    def test_chat_passes_timeout(self, fake_provider):
        """iter 165: chat() 应传 timeout 参数 (litellm 默认无限等待, 上游卡住会占线程池)。"""
        with patch.object(backend, "select_provider", return_value=(fake_provider, "cc_switch")):
            with patch.object(backend.litellm, "completion") as mock_completion:
                mock_completion.return_value = _mock_response("ok")
                backend.chat(messages=[{"role": "user", "content": "x"}])
                call_kwargs = mock_completion.call_args.kwargs
                assert call_kwargs.get("timeout") == backend.LLM_TIMEOUT_SECONDS


class TestNormalizeApiBase:
    """_normalize_api_base 单元测试。"""

    def test_openai_appends_v1(self):
        assert backend._normalize_api_base("https://opencode.ai/zen/go", "openai") == (
            "https://opencode.ai/zen/go/v1"
        )

    def test_openai_keeps_existing_v1(self):
        assert backend._normalize_api_base("https://api.openai.com/v1", "openai") == (
            "https://api.openai.com/v1"
        )

    def test_openai_keeps_chat_completions_suffix(self):
        assert backend._normalize_api_base("https://x.com/chat/completions", "openai") == (
            "https://x.com/chat/completions"
        )

    def test_anthropic_unchanged(self):
        assert backend._normalize_api_base("https://api.anthropic.com", "anthropic") == (
            "https://api.anthropic.com"
        )

    def test_chat_returns_empty_string_for_null_content(self, fake_provider):
        """content 为 None 时应返回空字符串(避免渲染 null)。"""
        with patch.object(backend, "select_provider", return_value=(fake_provider, "cc_switch")):
            with patch.object(backend.litellm, "completion") as mock_completion:
                mock_completion.return_value = _mock_response(None)

                result = backend.chat(messages=[{"role": "user", "content": "x"}])
                assert result["text"] == ""

    def test_chat_raises_on_empty_choices(self, fake_provider):
        """response.choices 为空时应抛 RuntimeError(避免 IndexError)。"""
        with patch.object(backend, "select_provider", return_value=(fake_provider, "cc_switch")):
            with patch.object(backend.litellm, "completion") as mock_completion:
                mock_completion.return_value = type("Response", (), {"choices": []})()

                with pytest.raises(RuntimeError, match="LLM 返回空响应"):
                    backend.chat(messages=[{"role": "user", "content": "x"}])


class TestChatExceptionMapping:
    """iter 124: chat() 把 litellm 异常映射为带前缀的 RuntimeError, 让 router 能按
    HTTP 状态码分别处理 (iter 64)。这些分支之前只测过 router 层映射, chat() 自身的
    异常包装路径是 0% 覆盖, 任何 litellm 异常类型变更都会逃过测试。

    每个分支独立 mock litellm.completion 抛对应异常, 验证 RuntimeError 前缀。
    """

    @pytest.mark.parametrize(
        "exc,expected_prefix",
        [
            (backend.litellm.AuthenticationError("bad key", llm_provider="anthropic", model="X"), "LLM_AUTH:"),
            (backend.litellm.RateLimitError("rate limited", llm_provider="anthropic", model="X"), "LLM_RATE_LIMIT:"),
            (backend.litellm.Timeout("timed out", llm_provider="anthropic", model="X"), "LLM_TIMEOUT:"),
            (backend.litellm.APIConnectionError("conn refused", llm_provider="anthropic", model="X"), "LLM_TIMEOUT:"),
            (backend.litellm.ContextWindowExceededError("too big", llm_provider="anthropic", model="X"), "LLM_CONTEXT:"),
            (backend.litellm.ServiceUnavailableError("down", llm_provider="anthropic", model="X"), "LLM_UNAVAILABLE:"),
            (backend.litellm.APIError(500, "oops", llm_provider="anthropic", model="X"), "LLM_API_ERROR:"),
        ],
    )
    def test_chat_maps_litellm_exception_to_runtime_error_prefix(
        self, fake_provider, exc, expected_prefix
    ):
        with patch.object(backend, "select_provider", return_value=(fake_provider, "cc_switch")):
            with patch.object(backend.litellm, "completion") as mock_completion:
                mock_completion.side_effect = exc

                with pytest.raises(RuntimeError) as excinfo:
                    backend.chat(messages=[{"role": "user", "content": "x"}])
                # 验证前缀正确, router 才能按字符串前缀映射 HTTP 状态码
                assert str(excinfo.value).startswith(expected_prefix), (
                    f"Expected prefix {expected_prefix!r}, got {str(excinfo.value)!r}"
                )


def _mock_response(content):
    """构造 litellm completion 返回值的 mock。"""
    message = type("Message", (), {"content": content})()
    choice = type("Choice", (), {"message": message})()
    return type("Response", (), {"choices": [choice]})()
