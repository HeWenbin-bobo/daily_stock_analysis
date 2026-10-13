# -*- coding: utf-8 -*-
"""
LLM 路由器 - 多模型 + 自动选模型 + 用量统计
- 提供商：DeepSeek (便宜) / Qwen-Max / GPT-4o
- 按任务难度自动选择：简单 (deepseek-chat) / 复杂 (gpt-4o / qwen-max)
- 统一接口：chat(prompt, task_complexity='simple')
- 用量自动写入 llm_usage 表（成本估算）
"""
import os
import time
import json
import logging
import asyncio
from typing import Optional, List, Dict, Any, AsyncIterator
from dataclasses import dataclass
from enum import Enum

from db.migrate import get_conn

logger = logging.getLogger(__name__)


class Provider(Enum):
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    OPENAI = "openai"


# 任务复杂度 → 推荐模型
TASK_COMPLEXITY = {
    "simple": Provider.DEEPSEEK,          # 价格便宜，适合大批量
    "medium": Provider.QWEN,
    "complex": Provider.OPENAI,           # 效果最好
}


@dataclass
class LLMConfig:
    api_key: str
    base_url: str
    model: str


# 各 provider 配置（key 从环境变量读；缺失则降级到 mock）
def _get_config(provider: Provider) -> Optional[LLMConfig]:
    if provider == Provider.DEEPSEEK:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if key:
            return LLMConfig(key, "https://api.deepseek.com", "deepseek-chat")
    elif provider == Provider.QWEN:
        key = os.environ.get("QWEN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
        if key:
            return LLMConfig(key, "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-max")
    elif provider == Provider.OPENAI:
        key = os.environ.get("OPENAI_API_KEY")
        if key:
            base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
            return LLMConfig(key, base, model)
    return None


# 成本估算（元 / 1k tokens）
_COST_PER_1K_TOKENS = {
    Provider.DEEPSEEK: (0.001, 0.002),   # input, output（人民币约）
    Provider.QWEN: (0.02, 0.06),
    Provider.OPENAI: (0.18, 0.72),       # gpt-4o-mini
}


def _estimate_cost(provider: Provider, prompt_tokens: int, completion_tokens: int) -> float:
    in_cost, out_cost = _COST_PER_1K_TOKENS.get(provider, (0.01, 0.03))
    return (prompt_tokens / 1000.0) * in_cost + (completion_tokens / 1000.0) * out_cost


def _record_usage(
    user_id: Optional[str],
    provider: Provider,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    task_type: str,
) -> None:
    try:
        total = prompt_tokens + completion_tokens
        cost = _estimate_cost(provider, prompt_tokens, completion_tokens)
        conn = get_conn()
        conn.execute(
            """INSERT INTO llm_usage
               (user_id, provider, model, prompt_tokens, completion_tokens, total_tokens, cost_cny, task_type, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, provider.value, model, prompt_tokens, completion_tokens, total, cost, task_type, time.time()),
        )
    except Exception as e:
        logger.warning("Failed to record LLM usage: %s", e)


# ============================================================================
# Mock provider（无 API key 时的兜底）
# ============================================================================
def _mock_chat(prompt: str, system: str = "") -> str:
    """Mock LLM 响应（用于无 API key 的本地开发/测试）"""
    return f"[MOCK-LLM] 基于 prompt ({len(prompt)} chars) 的占位响应。系统提示：{system[:50] if system else '无'}。配置真实 API key 后会替换为真实模型输出。"


# ============================================================================
# 主入口
# ============================================================================
# ★ Round 5：LLM 调用计数
_llm_request_total = 0
_llm_fallback_total = 0
_llm_mock_total = 0


def get_llm_metrics() -> Dict[str, int]:
    return {
        "llm_request_total": _llm_request_total,
        "llm_fallback_total": _llm_fallback_total,
        "llm_mock_total": _llm_mock_total,
    }


def chat(
    prompt: str,
    *,
    system: str = "",
    task_complexity: str = "simple",
    user_id: Optional[str] = None,
    task_type: str = "general",
    prefer_provider: Optional[Provider] = None,
    temperature: float = 0.7,
    max_tokens: int = 2000,
) -> Dict[str, Any]:
    """
    同步聊天接口
    返回: {"content": str, "provider": str, "model": str, "tokens": int, "cost_cny": float, "mocked": bool}
    """
    global _llm_request_total, _llm_fallback_total, _llm_mock_total
    _llm_request_total += 1
    provider = prefer_provider or TASK_COMPLEXITY.get(task_complexity, Provider.DEEPSEEK)
    config = _get_config(provider)

    if config is None:
        _llm_fallback_total += 1
        # 降级链：先尝试 prefer_provider，再按 openai → qwen → deepseek 顺序
        fallback_chain = []
        if prefer_provider is not None:
            fallback_chain.append(prefer_provider)
        for p in [Provider.OPENAI, Provider.QWEN, Provider.DEEPSEEK]:
            if p not in fallback_chain:
                fallback_chain.append(p)
        for fallback in fallback_chain:
            cfg = _get_config(fallback)
            if cfg:
                provider = fallback
                config = cfg
                break
        if config is None:
            # 完全没 key，返 mock
            _llm_mock_total += 1
            content = _mock_chat(prompt, system)
            return {
                "content": content,
                "provider": "mock",
                "model": "mock",
                "tokens": len(prompt) // 2 + len(content) // 2,
                "cost_cny": 0.0,
                "mocked": True,
            }

    # 有 API key，调真实模型
    try:
        content, prompt_tokens, completion_tokens = _call_real(
            config, prompt, system, temperature, max_tokens
        )
        cost = _estimate_cost(provider, prompt_tokens, completion_tokens)
        _record_usage(user_id, provider, config.model, prompt_tokens, completion_tokens, task_type)
        return {
            "content": content,
            "provider": provider.value,
            "model": config.model,
            "tokens": prompt_tokens + completion_tokens,
            "cost_cny": round(cost, 4),
            "mocked": False,
        }
    except Exception as e:
        _llm_fallback_total += 1
        _llm_mock_total += 1
        logger.exception("LLM call failed: %s; falling back to mock", e)
        content = _mock_chat(prompt, system) + f"\n[ERROR: {str(e)[:100]}]"
        return {
            "content": content,
            "provider": provider.value,
            "model": config.model,
            "tokens": 0,
            "cost_cny": 0.0,
            "mocked": True,
            "error": str(e),
        }


def _call_real(
    config: LLMConfig,
    prompt: str,
    system: str,
    temperature: float,
    max_tokens: int,
) -> tuple[str, int, int]:
    """调真实 LLM API
    ★ Round 5：超时降到 20s（之前 60s 阻塞 event loop 太长）；fallback chain 总预算 ≤30s
    """
    import urllib.request
    import urllib.error

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body = json.dumps({
        "model": config.model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{config.base_url}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.api_key}",
        },
    )
    # ★ Round 5：20s 上限（之前 60s 单次阻塞太长；fallback 链累加 ≤30s 通过 max_tokens + 上层 retry 控制）
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return (
        content,
        usage.get("prompt_tokens", 0),
        usage.get("completion_tokens", 0),
    )


# ============================================================================
# 流式输出 (SSE)
# ============================================================================
async def chat_stream(
    prompt: str,
    *,
    system: str = "",
    task_complexity: str = "simple",
    user_id: Optional[str] = None,
    task_type: str = "general",
) -> AsyncIterator[Dict[str, Any]]:
    """流式 chat，逐 token 返回"""
    result = chat(
        prompt, system=system, task_complexity=task_complexity,
        user_id=user_id, task_type=task_type,
    )
    content = result["content"]
    # 模拟流式（按词切分）
    for i, chunk in enumerate(_chunk_text(content, chunk_size=10)):
        yield {
            "event": "chunk",
            "delta": chunk,
            "index": i,
            "done": False,
        }
        await asyncio.sleep(0.02)
    yield {
        "event": "done",
        "provider": result["provider"],
        "model": result["model"],
        "tokens": result["tokens"],
        "cost_cny": result["cost_cny"],
        "mocked": result["mocked"],
        "index": -1,
        "done": True,
    }


def _chunk_text(text: str, chunk_size: int = 10) -> List[str]:
    return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]


# ============================================================================
# 用量查询
# ============================================================================
def get_usage_summary(user_id: Optional[str] = None, days: int = 30) -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.cursor()
    since = time.time() - days * 86400
    if user_id:
        cur.execute(
            """SELECT provider, COUNT(*), SUM(total_tokens), SUM(cost_cny)
               FROM llm_usage WHERE user_id=? AND created_at>=? GROUP BY provider""",
            (user_id, since),
        )
    else:
        cur.execute(
            """SELECT provider, COUNT(*), SUM(total_tokens), SUM(cost_cny)
               FROM llm_usage WHERE created_at>=? GROUP BY provider""",
            (since,),
        )
    rows = [dict(r) for r in cur.fetchall()]
    total_cost = sum((r["SUM(cost_cny)"] or 0) for r in rows)
    total_tokens = sum((r["SUM(total_tokens)"] or 0) for r in rows)
    return {
        "by_provider": rows,
        "total_cost_cny": round(total_cost, 4),
        "total_tokens": total_tokens,
        "days": days,
    }