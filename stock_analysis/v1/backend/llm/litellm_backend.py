"""LiteLLM 多 provider LLM 调用后端。

调用顺序:
  1. 优先 CC switch 当前活跃 provider (用户明确要求)
  2. 兜底: 用户 .env 中的 ANTHROPIC_API_KEY / OPENAI_API_KEY
     (历史曾支持 MINIMAX/ANSPIRE_* 前缀, 已移除 - 见 iter 57)

参考 daily_stock_analysis_v2/src/llm/litellm_backend.py 的 GenerationResult 模式,
但本应用简化为只做 chat completion。
"""
from __future__ import annotations

import asyncio
import os
import re
from typing import Optional

import litellm

from llm.cc_switch import detect_active_provider
from services.config import is_force_mock

litellm.drop_params = True  # 自动剔除不支持的参数

# iter 162: Claude Code / CC switch 的模型上下文标记 (e.g. "deepseek-v4-flash[1M]")。
# 裸 API 不认识 "[1M]" 后缀 (opencode.ai 返回 "Model deepseek-v4-flash[1M] is not supported"),
# 调用前必须剔除。仅影响 API 请求的 model 名, 不影响展示 (chat() 返回原 model)。
_MODEL_CONTEXT_SUFFIX_RE = re.compile(r"\[[^\]]*\]")


def _normalize_api_base(base_url: str, litellm_prefix: str) -> str:
    """归一化 api_base 让 LiteLLM 命中正确端点。

    - OpenAI 兼容 (litellm_prefix="openai"): LiteLLM 拼 ``{api_base}/chat/completions``,
      所以 api_base 必须以 ``/v1`` 结尾才能命中真实端点 ``/v1/chat/completions``。
      CC switch 存的 base_url 常不带 /v1 (e.g. https://opencode.ai/zen/go)。
    - Anthropic (litellm_prefix="anthropic"): LiteLLM 自己拼 ``/v1/messages``,
      不需要额外处理。

    iter 162: OpenCode Go (apiFormat=openai_chat) 之前用 anthropic 协议 + 未补 /v1,
    导致 401/404。现在按协议补 /v1。
    """
    url = base_url.rstrip("/")
    if litellm_prefix == "openai" and not url.endswith(("/v1", "/v1/", "/chat/completions")):
        return f"{url}/v1"
    return url


_MOCK_RESPONSE = {
    "provider": "mock",
    "model": "mock",
    "source": "force_mock",
    "text": "[MOCK] LLM 解读已禁用 (FORCE_MOCK=1), 不会调用真实 API.",
}


def _try_fallback_env() -> Optional[dict]:
    """从环境变量构造一个兜底 provider 配置。

    iter 97: 用 os.environ.get() 代替 [] — 若环境变量被设为空字符串
    (e.g. ``ANTHROPIC_API_KEY=``), `os.environ.get()` 仍返回空字符串 (falsy),
    `os.environ[]` 直接抛 KeyError。统一用 get() 避免 KeyError。

    iter 108: base_url 显式归一化 — env 设为空字符串视为 "未设置", 而非 "故意为空"。
    litellm 不区分 None vs "" 但代码语义上, 空字符串应被当作 unset, 避免下游
    `if provider.get("base_url"):` 误判为"显式传空"。

    Returns None 表示没有可用的 API key (an/openai 都没配)。返回的 provider dict
    若 base_url 字段为空字符串, 调用方应跳过 `api_base` kwarg (chat() 已实现)。
    """
    def _norm_url(key: str, default: str = "") -> str:
        val = os.environ.get(key, default)
        # 空字符串视为 unset — 防止 "ANTHROPIC_BASE_URL=" 被当成有效配置传给 litellm
        return val if val else ""

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        return {
            "name": "env:anthropic",
            "base_url": _norm_url("ANTHROPIC_BASE_URL"),
            "api_key": anthropic_key,
            "model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
            "litellm_prefix": "anthropic",
        }
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        return {
            "name": "env:openai",
            "base_url": _norm_url("OPENAI_BASE_URL"),
            "api_key": openai_key,
            "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            "litellm_prefix": "openai",
        }
    return None


def select_provider() -> tuple[dict, str]:
    """选择要使用的 provider。

    Returns: (provider_config, source)
        source: "cc_switch" | "env_fallback" | "none"
    """
    active = detect_active_provider("claude")
    if active and active.get("api_key"):
        return active, "cc_switch"

    fallback = _try_fallback_env()
    if fallback:
        return fallback, "env_fallback"

    raise RuntimeError(
        "未找到可用 LLM provider: CC switch 未配置, 且 .env 中也没有 ANTHROPIC_API_KEY / OPENAI_API_KEY。"
        "请在 CC switch 中启用一个 provider, 或在 backend/.env 中配置 API key。"
    )


# iter 98: 与 routers/analysis.py 的 LLM_MAX_TOKENS=800 统一默认值 — 避免
# chat() 默认 1024 / router 传 800 造成不一致。
DEFAULT_MAX_TOKENS = 800

# iter 101: 与 routers/analysis.py LLM_TEMPERATURE=0.6 统一默认值 — chat() 之前默认 0.7,
# router 显式传 0.6 覆盖; 但未来如果有调用方不传 temperature, 会拿到不一致的值。
DEFAULT_TEMPERATURE = 0.6

# iter 165: LLM 调用超时 — litellm.completion 默认无限等待, 上游 API 卡住时后端线程
# 一直占用 (前端 axios 30s 超时后, 后端 to_thread 线程仍在等, 线程池可能耗尽)。
# 60s 给 LLM 足够时间 (实测 opencode.ai 解读 ~10s), 又避免无限挂起。
LLM_TIMEOUT_SECONDS = 60


def chat(
    messages: list[dict],
    system: Optional[str] = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """调用 LLM 生成回复 (同步包装器, 返回 _MOCK_RESPONSE 或返回 dict)。

    Returns:
        {
          "provider": "MiniMax",
          "model": "MiniMax-M3",
          "source": "cc_switch" | "env_fallback" | "force_mock",
          "text": "...",
        }

    Raises:
        RuntimeError: 当未配置 provider 时
        litellm.exceptions.AuthenticationError: API key 无效
        litellm.exceptions.RateLimitError: 触发限流
        litellm.exceptions.Timeout: 超时
        litellm.exceptions.APIError: 其他上游 API 错误

    注意: 此函数是同步的, 不应在 async 上下文直接调用 — 在 async 端点里
    改用 await ``achat()``, 后者把同步 litellm 调用推到线程池, 不阻塞事件循环。
    """
    # FORCE_MOCK 短路: 防止 FORCE_MOCK=0 时误消耗真实 API 配额
    # iter 77: _MOCK_RESPONSE 已经是 dict literal, 直接返回即可 (避免 dict() 拷贝开销)
    if is_force_mock():
        return _MOCK_RESPONSE

    provider, source = select_provider()

    full_messages = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    # iter 162: 剔除 Claude Code 上下文标记 "[1M]" — 裸 API 不识别该后缀,
    # opencode.ai 会返回 "Model deepseek-v4-flash[1M] is not supported"。
    api_model = _MODEL_CONTEXT_SUFFIX_RE.sub("", provider["model"]).strip()
    model_id = f"{provider['litellm_prefix']}/{api_model}"

    kwargs = {
        "model": model_id,
        "messages": full_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "api_key": provider["api_key"],
        # iter 165: 显式超时 — litellm 默认无限等待, 上游卡住会占线程池
        "timeout": LLM_TIMEOUT_SECONDS,
    }
    if provider.get("base_url"):
        # iter 162: OpenAI 兼容 provider 的 api_base 需要以 /v1 结尾, 否则 LiteLLM
        # 拼到 "/chat/completions" 返回 404。CC switch 存的 base_url 常不带 /v1
        # (e.g. https://opencode.ai/zen/go), 这里按协议补上。anthropic 协议不需要
        # (LiteLLM 自己拼 /v1/messages)。
        kwargs["api_base"] = _normalize_api_base(provider["base_url"], provider["litellm_prefix"])

    # 完整错误响应(原异常会包含敏感信息如完整 URL/key 提示)
    # iter 64: 显式捕获 litellm 的具体异常类型, 让路由器能按 HTTP 状态码分别处理
    # (之前的字符串匹配 "RateLimit"/"Authentication" 在子类型重命名后会漏)
    # iter 95: 调用方如果是 async 端点, 应使用 ``achat()`` 包装版, 本函数保持同步
    # 以兼容现有 tests/test_llm_chat.py 等同步测试 (mock litellm.completion 不需要 to_thread)。
    try:
        response = litellm.completion(**kwargs)
    except litellm.AuthenticationError as exc:
        raise RuntimeError(f"LLM_AUTH:{exc.__class__.__name__}") from exc
    except litellm.RateLimitError as exc:
        raise RuntimeError(f"LLM_RATE_LIMIT:{exc.__class__.__name__}") from exc
    except (litellm.Timeout, litellm.APIConnectionError) as exc:
        raise RuntimeError(f"LLM_TIMEOUT:{exc.__class__.__name__}") from exc
    except litellm.ContextWindowExceededError as exc:
        raise RuntimeError(f"LLM_CONTEXT:{exc.__class__.__name__}") from exc
    except litellm.ServiceUnavailableError as exc:
        # 503/502 from provider — map to 503 in router
        raise RuntimeError(f"LLM_UNAVAILABLE:{exc.__class__.__name__}") from exc
    except litellm.APIError as exc:
        raise RuntimeError(f"LLM_API_ERROR:{exc.__class__.__name__}") from exc
    # 校验响应结构,避免索引失败
    if not response or not getattr(response, "choices", None) or len(response.choices) == 0:
        raise RuntimeError(f"LLM 返回空响应 (provider={provider['name']}, model={provider['model']})")
    content = response.choices[0].message.content
    text = content if isinstance(content, str) else ""

    return {
        "provider": provider["name"],
        "model": provider["model"],
        "source": source,
        "text": text,
    }


# iter 95: async 包装器 — async 端点 (e.g. /api/v1/analysis/llm-insight) 用 await achat()
# 把同步的 litellm.completion 推到线程池, 避免 1-10 秒 LLM 调用冻结事件循环,
# 让其它并发请求 (rate-limit check, fetch_history, etc.) 继续被派发。
async def achat(
    messages: list[dict],
    system: Optional[str] = None,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict:
    """异步版 chat() — 在事件循环线程池中调用同步 chat()。

    行为与 chat() 完全一致, 仅在事件循环中可被 await。
    """
    return await asyncio.to_thread(
        chat, messages, system, temperature, max_tokens
    )