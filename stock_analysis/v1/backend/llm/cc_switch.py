r"""读取 CC switch 当前活跃的 LLM provider。

CC switch 是用户的 Claude Code LLM 切换工具, 配置位于:
  - C:/Users/jww/.cc-switch/settings.json (快速索引)
  - C:/Users/jww/.cc-switch/cc-switch.db (SQLite, 完整 provider 配置)

工作机制:
  1. 读 settings.json 取 currentProviderClaude (UUID)
  2. 查 cc-switch.db 中 providers 表, is_current=1 的那条
  3. 解析 settings_config JSON (含 env 字段: ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN/ANTHROPIC_MODEL)
  4. 返回标准化字典给 litellm 调用

参考 daily_stock_analysis_v2/src/llm/litellm_backend.py 的 LLM 调用模式。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Optional


CC_SWITCH_DIR = Path(
    os.environ.get("CC_SWITCH_DIR")
    # iter 97: 默认路径不再硬编码 jww 用户名 — 用 Path.home() 让任何 OS/用户都能解析。
    # Windows 下 Path.home() = %USERPROFILE% (~), POSIX 下 = $HOME, 等价语义。
    or str(Path.home() / ".cc-switch")
)
SETTINGS_JSON = CC_SWITCH_DIR / "settings.json"
DB_PATH = CC_SWITCH_DIR / "cc-switch.db"

import logging
logger = logging.getLogger(__name__)


def _connect_db() -> "closing[sqlite3.Connection]":
    """Open CC switch DB with proper closing semantics.

    sqlite3.Connection's ``__enter__/__exit__`` only handle transactions
    (commit/rollback), NOT connection closing. Wrap with ``contextlib.closing``
    so callers get a context manager that actually closes the underlying
    connection and avoids ResourceWarning leaks.
    """
    # iter 81: timeout=5.0 让 sqlite3.connect 在第一次打开文件失败前等待 5s,
    # 避免与 Claude Code 同时写入 CC switch DB 时遇到 SQLITE_BUSY 立刻抛错。
    # PRAGMA busy_timeout 在连接建立后才会生效, 因此 connect 阶段的瞬时锁竞争仍会失败。
    # iter 85: 用 _with_busy_retry 包裹后续 query, 处理 connect 后发生的 SQLITE_BUSY。
    return closing(sqlite3.connect(str(DB_PATH), timeout=5.0))


# iter 85: SQLite 瞬时锁竞争 (Claude Code 切换 provider 时同时写入 cc-switch.db) 的重试包装。
# 只对 OperationalError 的 "database is locked" 重试, 其它异常照常抛出。
# 退避 100ms / 200ms / 400ms, 最多 3 次 — 配合 connect timeout=5s 已经覆盖大多数场景。
RETRY_DELAYS = (0.1, 0.2, 0.4)
# iter 105: 注释清理 — 之前说明的"第三次零延迟"已经在 iter 86 移除, 该注释已过时。
# 当前三次延迟显式列出, 没有零延迟, 不需要再解释"为什么不是零延迟"。

# iter 131: detect_active_provider 结果缓存 — /llm-insight + /llm-status 每次调用都触发
# 一次 settings.json 读取 + 一次 SQLite 查询。SQLite open 在锁竞争下可能 5-50ms。
# 30s TTL 覆盖典型用户行为 (切 provider → 多次解读请求), 又能在用户切 provider 后
# 30s 内生效。比 filesystem mtime 监听更简单且足够稳健 (切 provider 本就是低频操作)。
_DETECT_CACHE: dict[str, tuple[float, Optional[dict]]] = {}
_DETECT_CACHE_TTL_S = 30.0

# iter 102: 用 sqlite_errcode 取代字符串匹配 — Python 的 sqlite3 在底层错误码
# 层面最稳定 (locked=5, busy=5 同一个码)。字符串如 "database is locked" 在不同
# sqlite 版本/驱动里措辞略有差异 (e.g. WAL 模式下可能是 "database table is locked"),
# 字符串匹配容易漏判。
SQLITE_BUSY_ERRCODE = 5


def _with_busy_retry(fn):
    last_err: Optional[Exception] = None
    for attempt, delay in enumerate(RETRY_DELAYS):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            # sqlite_errcode 可能为 None (e.g. 自定义异常子类); fallback 到字符串
            errcode = getattr(exc, "sqlite_errcode", None)
            if errcode != SQLITE_BUSY_ERRCODE and "locked" not in str(exc).lower():
                raise
            last_err = exc
            logger.debug("CC switch DB busy (code=%s), retry %d in %.1fs", errcode, attempt + 1, delay)
            time.sleep(delay)
    assert last_err is not None  # 仅当上面循环总是走 except 时才触发
    raise last_err


def _read_settings_index() -> dict:
    """读取 settings.json, 返回快速索引。

    iter 75: 文件存在但解析失败时记 warning — 之前静默 return {} 让运维看不到
    settings.json 损坏的痕迹。
    """
    if not SETTINGS_JSON.exists():
        return {}
    try:
        return json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "CC switch settings.json 解析失败 (%s): %s — fallback 到空配置",
            SETTINGS_JSON, exc,
        )
        return {}


def _query_provider_by_id(provider_id: str) -> Optional[dict]:
    """从 SQLite 查询指定 ID 的 provider 完整配置。"""
    if not DB_PATH.exists():
        # iter 70: 静默缺失时记 warning, 让运维能在日志里发现"为什么 LLM 不可用"
        logger.warning("CC switch DB 不存在: %s (检查 CC_SWITCH_DIR 环境变量)", DB_PATH)
        return None
    # iter 85: 包 retry 包装, connect 后 SQLITE_BUSY 自动重试 3 次
    return _with_busy_retry(lambda: _fetch_provider_by_id_inner(provider_id))


def _fetch_provider_by_id_inner(provider_id: str) -> Optional[dict]:
    try:
        with _connect_db() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            cur = conn.cursor()
            # CC switch DB schema (v3+): id, app_type, name, settings_config (JSON string), is_current
            # iter 116: 移除 website_url — 该字段是供应商营销/文档链接, 不是 API endpoint,
            # /fetch-by-id 也不在响应中暴露 (与 iter 108/111 移除 base_url/SELECT website_url 配套)。
            # 之前 list_all_providers 已移除, 单条查询遗留 SELECT 是 dead code (sqlite 仍会读列)。
            cur.execute(
                "SELECT id, app_type, name, settings_config, is_current, meta "
                "FROM providers WHERE id = ?",
                (provider_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        result = dict(row)
        # 解析 settings_config JSON 字符串
        if result.get("settings_config"):
            try:
                result["settings_config_parsed"] = json.loads(result["settings_config"])
            except Exception as exc:
                # iter 79: 之前是裸 pass — 让 settings_config JSON 损坏被静默吞掉,
                # 表现为"provider 找到了但 litellm 拿不到 base_url/api_key"
                logger.warning("CC switch settings_config JSON 解析失败 (%s): %s", provider_id, exc)
                result["settings_config_parsed"] = {}
        # iter 162: 解析 meta JSON — CC switch 把 apiFormat (openai_chat/anthropic/...)
        # 存在 meta 字段, 之前没读取导致协议推断错误 (见 _normalize_provider)。
        if result.get("meta"):
            try:
                result["meta_parsed"] = json.loads(result["meta"])
            except Exception as exc:
                logger.debug("CC switch meta JSON 解析失败 (%s): %s", provider_id, exc)
                result["meta_parsed"] = {}
        return result
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        # iter 79: 之前是裸 return None — SQLite corruption/locking 等问题被静默吞掉,
        # 运维看不到任何痕迹, 难以诊断"为什么 LLM 突然不可用"
        # iter 90: 收窄到 sqlite3 + OSError, 避免吞掉 KeyboardInterrupt / SystemExit /
        # MemoryError 这些应当向上传播的关键异常。
        logger.warning("CC switch DB 查询失败 (provider_id=%s): %s", provider_id, exc)
        return None


def _query_current_provider(app_type: str = "claude") -> Optional[dict]:
    """直接从 DB 查 is_current=1 的 provider。"""
    if not DB_PATH.exists():
        return None
    # iter 85: retry 包装同 _query_provider_by_id
    return _with_busy_retry(lambda: _query_current_provider_inner(app_type))


def _query_current_provider_inner(app_type: str) -> Optional[dict]:
    try:
        with _connect_db() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            cur = conn.cursor()
            # iter 116: 移除 website_url SELECT (与 _fetch_provider_by_id_inner 同注释)
            cur.execute(
                "SELECT id, app_type, name, settings_config, is_current, meta "
                "FROM providers WHERE app_type = ? AND is_current = 1 LIMIT 1",
                (app_type,),
            )
            row = cur.fetchone()
        if not row:
            return None
        result = dict(row)
        if result.get("settings_config"):
            try:
                result["settings_config_parsed"] = json.loads(result["settings_config"])
            except Exception as exc:
                logger.warning("CC switch settings_config JSON 解析失败 (current/%s): %s", app_type, exc)
                result["settings_config_parsed"] = {}
        # iter 162: 解析 meta JSON (apiFormat) — 与 _fetch_provider_by_id_inner 一致
        if result.get("meta"):
            try:
                result["meta_parsed"] = json.loads(result["meta"])
            except Exception as exc:
                logger.debug("CC switch meta JSON 解析失败 (current/%s): %s", app_type, exc)
                result["meta_parsed"] = {}
        return result
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        # iter 79: 同 _query_provider_by_id — 不再裸 return
        # iter 90: 收窄异常类型 — 不要吞掉 KeyboardInterrupt / SystemExit 等关键异常。
        logger.warning("CC switch DB 查询失败 (current/%s): %s", app_type, exc)
        return None


def detect_active_provider(app_type: str = "claude") -> Optional[dict]:
    """检测 CC switch 当前激活的 provider。

    读取优先级: settings.json index (UUID) > DB is_current=1 兜底。

    Args:
        app_type: CC switch app 类型，默认为 "claude"。
            支持 "claude"/"codex"/"gemini"，决定读 settings.json 中哪个字段
            (currentProviderClaude/currentProviderCodex/currentProviderGemini)。

    Returns:
        归一化后的 provider 字典，包含字段:
          - id, name, app_type: 原始记录
          - base_url, api_key, model: 解析后的 LLM 调用参数
          - litellm_prefix: 推断的 litellm 前缀 (anthropic/openai/gemini/deepseek)
          - raw: 原始 DB 记录 (含 settings_config_parsed)
        若无激活 provider 或 DB 不存在，返回 None。
    """
    # iter 131: 30s TTL 缓存 — 避免每次 /llm-insight / /llm-status 都读 settings.json + 查 SQLite。
    cached = _DETECT_CACHE.get(app_type)
    if cached is not None:
        ts, value = cached
        if time.monotonic() - ts < _DETECT_CACHE_TTL_S:
            return value
        _DETECT_CACHE.pop(app_type, None)

    settings = _read_settings_index()
    # 优先用 settings.json 的索引
    field_map = {
        "claude": "currentProviderClaude",
        "codex": "currentProviderCodex",
        "gemini": "currentProviderGemini",
    }
    provider_id = settings.get(field_map.get(app_type, "currentProviderClaude"))

    provider = None
    if provider_id:
        provider = _query_provider_by_id(provider_id)
    if not provider:
        # 兜底: 直接查 is_current=1
        provider = _query_current_provider(app_type)

    if not provider:
        _DETECT_CACHE[app_type] = (time.monotonic(), None)
        return None

    result = _normalize_provider(provider)
    _DETECT_CACHE[app_type] = (time.monotonic(), result)
    return result


def _api_format_prefix(provider: dict) -> Optional[str]:
    """从 CC switch provider 的 meta.apiFormat 推断 litellm prefix。

    CC switch 把协议声明存在 ``meta`` 字段 (DB 独立列), 例如:
      - ``{"apiFormat": "openai_chat", ...}`` → OpenAI 兼容协议 → "openai"
      - ``{"apiFormat": "anthropic", ...}`` → Anthropic 协议 → "anthropic"
      - ``{"apiFormat": "openai_native", ...}`` → OpenAI 原生 → "openai"

    iter 162: 之前只从 env 变量名 / provider 名猜协议, 对 "OpenCode Go" 这类
    OpenAI 兼容但 env 用 ANTHROPIC_* 命名的 provider 会误判为 anthropic。

    Returns: "openai" / "anthropic" / "gemini" / None (无法判断时 None, 走其它启发式)
    """
    meta = provider.get("meta_parsed") if isinstance(provider.get("meta_parsed"), dict) else {}
    api_format = (meta.get("apiFormat") or "").lower()
    if "openai" in api_format:
        return "openai"
    if "anthropic" in api_format or "claude" in api_format:
        return "anthropic"
    if "gemini" in api_format:
        return "gemini"
    return None


def _normalize_provider(provider: dict) -> dict:
    """把 CC switch 的 provider 记录归一化为 LLM 调用所需字段。"""
    parsed = provider.get("settings_config_parsed", {}) or {}
    env = parsed.get("env", {}) if isinstance(parsed, dict) else {}

    # 提取关键字段(env 是 CC switch 写入 Claude Code 环境变量的字段)
    # iter 108: 移除 website_url fallback — 该字段是 CC switch 的供应商"主页/文档链接",
    # 不一定是 API endpoint。第三方代理若只配 website_url (没填 base_url), 之前会
    # 把营销页 URL 当 API URL 传给 litellm, 导致请求发到错误主机。现在显式要求
    # base_url 来自 env 或 parsed.base_url; 都没有则用空字符串, chat() 里 if provider.get("base_url")
    # 会过滤掉 (空字符串 falsy)。
    base_url = (
        env.get("ANTHROPIC_BASE_URL")
        or env.get("OPENAI_BASE_URL")
        or parsed.get("base_url")
        or ""
    )
    api_key = (
        env.get("ANTHROPIC_AUTH_TOKEN")
        or env.get("ANTHROPIC_API_KEY")
        or env.get("OPENAI_API_KEY")
        or parsed.get("api_key")
        or ""
    )
    # model: 优先级 ANTHROPIC_MODEL → parsed.model → parsed.models[0] → 默认
    models_list = parsed.get("models") if isinstance(parsed.get("models"), list) else None
    model = (
        env.get("ANTHROPIC_MODEL")
        or parsed.get("model")
        or (models_list[0] if models_list else None)
        or "claude-sonnet-4-5"
    )

    # 推断 provider 类型(用于 litellm model 前缀)
    # iter 103: 优先级 — 1) 显式 parsed["litellm_prefix"] (CC switch 后续版本可能支持)
    # 2) provider_name 字符串匹配 3) base_url 启发式 4) 默认 OpenAI 兼容。
    # 之前的 fallback 顺序在 base_url 优先, 但 name 含 "openai" 但用 Anthropic 协议
    # (e.g. 第三方代理重命名) 的场景会误判。name 比 base_url 更可靠。
    provider_name = (provider.get("name") or "").lower()
    explicit_prefix = parsed.get("litellm_prefix")
    if explicit_prefix:
        litellm_prefix = explicit_prefix
        logger.debug("CC switch provider %s: explicit litellm_prefix=%s", provider.get("name"), explicit_prefix)
    elif _api_format_prefix(provider):
        # iter 162: CC switch meta.apiFormat — provider 显式声明协议 (openai_chat/anthropic),
        # 比从 env 变量名猜更可靠。之前 OpenCode Go 这类 OpenAI 兼容 provider 的
        # env 是 ANTHROPIC_* (CC switch 对 claude app 的惯例), 被误判为 anthropic
        # 协议 → 调用 /v1/messages 返回 401。读 meta 后能正确识别 openai 协议。
        litellm_prefix = _api_format_prefix(provider)
        logger.debug(
            "CC switch provider %s: litellm_prefix from meta.apiFormat=%s",
            provider.get("name"), litellm_prefix,
        )
    elif "anthropic" in provider_name or "claude" in provider_name or "ANTHROPIC" in str(env):
        litellm_prefix = "anthropic"
    elif "openai" in provider_name or "gpt" in provider_name or "OPENAI" in str(env):
        litellm_prefix = "openai"
    elif "gemini" in provider_name:
        litellm_prefix = "gemini"
    elif "deepseek" in provider_name:
        litellm_prefix = "deepseek"
    elif "minimax" in provider_name or "minimax" in base_url.lower():
        # MiniMax 用 anthropic 兼容协议
        litellm_prefix = "anthropic"
    else:
        # 根据 base_url 推断 — 仅在 name 完全不匹配时使用, 作为最终兜底
        if "anthropic" in base_url.lower():
            litellm_prefix = "anthropic"
        elif "openai" in base_url.lower():
            litellm_prefix = "openai"
        elif "gemini" in base_url.lower():
            litellm_prefix = "gemini"
        else:
            # 默认 OpenAI 兼容协议; 升级为 WARNING — 之前是 DEBUG, 用户配置陌生 provider
            # 时 silent fallback 到 openai, 请求发了才知道协议不匹配。
            # WARNING 让运维在配置阶段就能从日志发现"为什么 LLM 莫名其妙失败",
            # 而不是 LLM 调用返回 4xx 后才去翻日志。
            # iter 118: WARNING (替代 DEBUG) — 兜底路径本应是罕见事件, 每次发生都值得告警。
            litellm_prefix = "openai"
            logger.warning(
                "CC switch provider %s: litellm_prefix 兜底为 'openai' (provider_name=%s, base_url=%s) — 若该 provider 非 OpenAI 兼容协议, 请求会失败",
                provider.get("name"), provider_name, base_url,
            )

    return {
        "id": provider.get("id"),
        "name": provider.get("name"),
        "app_type": provider.get("app_type"),
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "litellm_prefix": litellm_prefix,
        "raw": provider,
    }


def list_all_providers(app_type: str = "claude") -> list[dict]:
    """列出指定 app_type 下所有配置的 provider (含激活与非激活)。

    查询 cc-switch.db providers 表，返回 id/name/is_current 列表。
    用于调试或前端展示 provider 列表。

    Args:
        app_type: CC switch app 类型，默认为 "claude"。

    Returns:
        provider 字典列表，每项含 id (uuid)、name、is_current (1=激活)。
        若 DB 不存在或查询失败，返回空列表并记 WARNING 日志。
    """
    if not DB_PATH.exists():
        return []
    # iter 85: retry 包装同其它查询
    return _with_busy_retry(lambda: _list_all_providers_inner(app_type))


def _list_all_providers_inner(app_type: str) -> list[dict]:
    try:
        with _connect_db() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            cur = conn.cursor()
            cur.execute(
                # iter 111: 移除 website_url — 与 iter 108 (移除 base_url fallback) 配套,
                # 该字段是供应商营销/文档链接, 不是 API endpoint, 不应在 /llm-status
                # 响应中暴露给调用方 (即便 require_admin 也只防未鉴权访问, 不防 admin
                # 探测内部供应商 URL)。
                "SELECT id, name, is_current FROM providers WHERE app_type = ?",
                (app_type,),
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]
    except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) as exc:
        # iter 79: 不再裸 return — list_all_providers 是调试/展示用,
        # 失败时只记 warning, 不阻塞调用方
        # iter 90: 收窄异常类型, 不要吞掉关键异常。
        logger.warning("CC switch DB 列表查询失败 (%s): %s", app_type, exc)
        return []