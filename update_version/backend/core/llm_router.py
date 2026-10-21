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
    GEMINI = "gemini"
    # ★ v3 融合：跟随本机 Claude Code switch 代理（LLM API key 全空时的兜底）
    #   读 ~/.claude/settings.json 的 ANTHROPIC_BASE_URL + 模型名（如 deepseek-v4-flash-vip）
    CC_SWITCH = "cc_switch"
    # ★ 探索期修复：跟随主项目的 LLM_CHANNELS 配置（见 _get_channel_config）。
    #   此前 v2 自带的 llm_router 只认上面 5 个 provider，而 .env 里配的是
    #   LLM_CHANNELS=minimax + LLM_MINIMAX_* —— v2 原生的 LLM 功能
    #   （/llm/chat、/technical/llm-insight、策略 DSL 编译、深度研究）
    #   全部静默降级成 [MOCK-LLM] 占位文本。这里补上。
    CHANNEL = "channel"


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
    # ★ 探索期修复：protocol ∈ {"openai"(默认), "anthropic"}。
    #   LLM_CHANNELS 的通道可能是 Anthropic 兼容协议（如 minimax/anthropic），
    #   端点路径与请求体都不同，必须由配置携带而不是靠 provider 硬编码。
    protocol: str = "openai"


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
    elif provider == Provider.GEMINI:
        # ★ v3 融合：Gemini 走 OpenAI 兼容端点（generativelanguage.googleapis.com/v1beta/openai）
        key = os.environ.get("GEMINI_API_KEY")
        if key:
            base = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
            model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
            return LLMConfig(key, base, model)
    elif provider == Provider.CC_SWITCH:
        # ★ v3 融合：跟随本机 Claude Code switch 代理。
        #   读 ~/.claude/settings.json 的 env.ANTHROPIC_BASE_URL（如 http://127.0.0.1:15721）
        #   与模型名（如 deepseek-v4-flash-vip）；无需 API key（本地代理）。
        cfg = _get_cc_switch_config()
        if cfg is not None:
            return cfg
    elif provider == Provider.CHANNEL:
        return _get_channel_config()
    return None


def _get_channel_config() -> Optional[LLMConfig]:
    """按主项目的 `LLM_CHANNELS` 约定读取第一个可用通道。

    主项目 `src/config.py` 的格式（这里只实现 v2 需要的子集）：
        LLM_CHANNELS=minimax,deepseek
        LLM_MINIMAX_PROTOCOL=anthropic      # 缺省 openai
        LLM_MINIMAX_BASE_URL=https://api.minimaxi.com/anthropic
        LLM_MINIMAX_API_KEY=sk-xxx          # 或 LLM_MINIMAX_API_KEYS=k1,k2
        LLM_MINIMAX_MODELS=MiniMax-M3       # 逗号分隔，取第一个

    ★ 探索期修复：v2 自带的 llm_router 完全不读这套配置，而 `.env` 里正是这样配的
      —— 于是 `/llm/chat`、`/technical/llm-insight`、策略 DSL 编译、深度研究
      全部静默返回 `[MOCK-LLM]` 占位文本（实测）。这里补上，并支持 anthropic 协议
      （请求走 `{base_url}/v1/messages`，与 CC switch 同一套格式）。
    """
    raw = (os.environ.get("LLM_CHANNELS") or "").strip()
    if not raw:
        return None
    for name in raw.split(","):
        ch = name.strip()
        if not ch:
            continue
        upper = ch.upper()
        enabled = (os.environ.get(f"LLM_{upper}_ENABLED") or "true").strip().lower()
        if enabled in ("false", "0", "no", "off"):
            continue
        key = (os.environ.get(f"LLM_{upper}_API_KEY") or "").strip()
        if not key:
            multi = [k.strip() for k in (os.environ.get(f"LLM_{upper}_API_KEYS") or "").split(",") if k.strip()]
            key = multi[0] if multi else ""
        if not key:
            continue
        base = (os.environ.get(f"LLM_{upper}_BASE_URL") or "").strip().rstrip("/")
        models = [m.strip() for m in (os.environ.get(f"LLM_{upper}_MODELS") or "").split(",") if m.strip()]
        if not base or not models:
            continue
        protocol = (os.environ.get(f"LLM_{upper}_PROTOCOL") or "openai").strip().lower()
        if protocol not in ("openai", "anthropic"):
            logger.warning("LLM channel %s: 未知 protocol %r，按 openai 处理", ch, protocol)
            protocol = "openai"
        logger.info("LLM channel resolved: %s (%s, %s)", ch, models[0], protocol)
        return LLMConfig(key, base, models[0], protocol=protocol)
    return None


def _get_cc_switch_config() -> Optional[LLMConfig]:
    """从本机 Claude Code 配置读取 CC switch 代理（Anthropic 兼容协议）。

    读取顺序：
      1. 环境变量 ANTHROPIC_BASE_URL / ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME（当前 CC 会话注入）
      2. ~/.claude/settings.json 的 env 段（持久配置）
    模型名优先取 *_MODEL_NAME（如 deepseek-v4-flash-vip），
    回退到 *_MODEL（如 claude-sonnet-4-6[1M]）。
    """
    try:
        from pathlib import Path
        base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
        model = (os.environ.get("ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME")
                 or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL_NAME")
                 or os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL_NAME")
                 or os.environ.get("ANTHROPIC_DEFAULT_FABLE_MODEL_NAME") or "").strip()
        # settings.json 兜底
        if not base_url or not model:
            settings_path = Path.home() / ".claude" / "settings.json"
            if settings_path.exists():
                import json as _json
                env = _json.loads(settings_path.read_text(encoding="utf-8")).get("env", {}) or {}
                base_url = base_url or (env.get("ANTHROPIC_BASE_URL") or "").strip()
                model = model or (
                    env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME")
                    or env.get("ANTHROPIC_DEFAULT_SONNET_MODEL_NAME")
                    or env.get("ANTHROPIC_DEFAULT_OPUS_MODEL_NAME")
                    or env.get("ANTHROPIC_DEFAULT_FABLE_MODEL_NAME")
                    or env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
                    or env.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
                    or ""
                ).strip()
        if not base_url or not model:
            return None
        # 去掉 [1M] 等上下文窗口后缀（代理只认纯模型名）
        model = model.split("[")[0].strip()
        return LLMConfig("cc-switch-local", base_url.rstrip("/"), model)
    except Exception as e:
        logger.warning("_get_cc_switch_config failed: %s", e)
        return None


# 成本估算（人民币 / 1k tokens）
# ★ Round 23 修复(M8)：旧 OPENAI 常量 (0.18, 0.72) 虚高 10~100 倍
#   （gpt-4o 实际约 ¥0.018/0.072，gpt-4o-mini 约 ¥0.001/0.004）
_COST_PER_1K_TOKENS = {
    Provider.DEEPSEEK: (0.001, 0.002),   # input, output（人民币约）
    Provider.QWEN: (0.02, 0.06),
    # gpt-4o-mini 默认；gpt-4o 见 _estimate_cost 内按 OPENAI_MODEL 区分
    Provider.OPENAI: (0.001, 0.004),
    # gemini-2.0-flash 免费额度/极低价格（约 ¥0.0001/0.0004）
    Provider.GEMINI: (0.0001, 0.0004),
    # 本机 CC switch 代理（deepseek-v4-flash-vip，近似 deepseek 价格）
    Provider.CC_SWITCH: (0.001, 0.002),
    # LLM_CHANNELS 通道（价格因供应商而异，取中等保守值；真实计费以供应商为准）
    Provider.CHANNEL: (0.01, 0.03),
}
# gpt-4o 的价格（按 OPENAI_MODEL 覆盖）
_OPENAI_COST_BY_MODEL = {
    "gpt-4o": (0.018, 0.072),
    "gpt-4o-mini": (0.001, 0.004),
    "gpt-4": (0.018, 0.072),
}


def _estimate_cost(provider: Provider, prompt_tokens: int, completion_tokens: int) -> float:
    in_cost, out_cost = _COST_PER_1K_TOKENS.get(provider, (0.01, 0.03))
    if provider == Provider.OPENAI:
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini").strip().lower()
        if model in _OPENAI_COST_BY_MODEL:
            in_cost, out_cost = _OPENAI_COST_BY_MODEL[model]
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
        try:
            conn.execute(
                """INSERT INTO llm_usage
                   (user_id, provider, model, prompt_tokens, completion_tokens, total_tokens, cost_cny, task_type, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, provider.value, model, prompt_tokens, completion_tokens, total, cost, task_type, time.time()),
            )
        finally:
            # ★ 探索期修复(fd 泄漏)：每次 LLM 调用都会走这里，连接却从不关闭。
            #   每条 SQLite 连接占 2 个 fd（db + wal）。
            try:
                conn.close()
            except Exception:
                pass
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
        # 降级链：先尝试 prefer_provider，再按 channel → openai → qwen → deepseek → gemini → CC switch
        # ★ v3 融合：最后尝试本机 CC switch 代理（LLM API key 全空时跟随本地配置）
        # ★ 探索期修复：`LLM_CHANNELS`（.env 里实际配置的那套）排在 openai 之前 ——
        #   否则只要有任何一家遗留 key，用户的显式配置反而被忽略。
        fallback_chain = []
        if prefer_provider is not None:
            fallback_chain.append(prefer_provider)
        for p in [Provider.CHANNEL, Provider.OPENAI, Provider.QWEN,
                  Provider.DEEPSEEK, Provider.GEMINI, Provider.CC_SWITCH]:
            if p not in fallback_chain:
                fallback_chain.append(p)
        for fallback in fallback_chain:
            cfg = _get_config(fallback)
            if cfg:
                provider = fallback
                config = cfg
                break
        if config is None:
            # 完全没 key + 无 CC switch，返 mock
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
            config, prompt, system, temperature, max_tokens, provider=provider
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
    provider: Optional[Provider] = None,
) -> tuple[str, int, int]:
    """调真实 LLM API
    ★ Round 5：超时降到 20s（之前 60s 阻塞 event loop 太长）；fallback chain 总预算 ≤30s
    ★ v3 融合：CC switch（Anthropic 兼容协议 /v1/messages）与 OpenAI 格式（/chat/completions）双协议支持
    """
    import urllib.request
    import urllib.error

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    # ★ v3 融合：CC switch 走 Anthropic 兼容协议（/v1/messages），
    #   其余 provider 走 OpenAI 兼容格式（/chat/completions）
    # ★ 探索期修复：判断依据改为 `config.protocol` —— LLM_CHANNELS 里的通道
    #   （如 minimax）也可能是 anthropic 协议，只看 provider 会走错端点。
    if provider == Provider.CC_SWITCH or config.protocol == "anthropic":
        # ★ 修复：CC switch 代理（glm 等思考模型）返回 thinking + text 双块结构；
        #   思考块可能耗尽 max_tokens（stop_reason=="max_tokens"），响应只有
        #   thinking 块、text 块缺失 → content="" 静默返回，deep_research 等
        #   调用方拿到空报告降级为"数据不足"。检测到该场景加倍 max_tokens 重试一次。
        timeout_s = int(os.environ.get("LLM_TIMEOUT", "300"))
        attempt_max_tokens = max_tokens
        for _attempt in range(2):
            body = json.dumps({
                "model": config.model,
                "system": system or None,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": attempt_max_tokens,
            }, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                f"{config.base_url}/v1/messages",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "x-api-key": config.api_key or "cc-switch-local",
                },
            )
            # ★ 修复：60s 硬编码对 complex 任务（四大师 deep research 等）不够，
            #   claude-opus 长 prompt 常超 60s 导致 TimeoutError → 报告降级为空。
            #   从环境变量读取，默认 300s；LLM_TIMEOUT 可按部署覆盖。
            #   （迭代3 实测：glm-5.3-flash 思考模型 16k token 预算下需 ~190s）
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content_parts = data.get("content", [])
            content = "".join(
                p.get("text", "") for p in content_parts if isinstance(p, dict) and p.get("type") == "text"
            )
            if content or data.get("stop_reason") != "max_tokens":
                break
            attempt_max_tokens *= 2  # 思考块耗尽 token → 加倍重试
        usage = data.get("usage", {})
        return (
            content,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
        )

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
    """流式 chat，逐 token 返回

    ★ Round 20 修复(M12)：原实现直接在 async 生成器里同步调用 chat()（urllib 阻塞最长 20s）
       → 会卡住整个事件循环。改为 asyncio.to_thread 委托线程执行；分块输出保留为模拟流式。
    """
    result = await asyncio.to_thread(
        chat, prompt, system=system, task_complexity=task_complexity,
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
    try:
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
    finally:
        try:
            conn.close()
        except Exception:
            pass
    total_cost = sum((r["SUM(cost_cny)"] or 0) for r in rows)
    total_tokens = sum((r["SUM(total_tokens)"] or 0) for r in rows)
    return {
        "by_provider": rows,
        "total_cost_cny": round(total_cost, 4),
        "total_tokens": total_tokens,
        "days": days,
    }