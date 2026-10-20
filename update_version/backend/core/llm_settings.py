# -*- coding: utf-8 -*-
"""v4 LLM 设置持久化 — 让 Web 界面可以直接配置自定义模型与 API key。

存储：backend/data/llm_settings.json（本地文件，明文存储——与主项目 .env 同级安全）。
安全约定：
- API 响应永不回传完整 key，只回传 is_set + 尾 4 位（mask 处理，防窥）；
- 前端传回 mask 哨兵值（__MASKED__）表示"未修改，保留原值"；
- 启动时与保存后立即 apply 到 os.environ，llm_router 每次请求都读 environ，
  因此改完即生效，无需重启。
"""
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "data" / "llm_settings.json"

MASK_SENTINEL = "__MASKED__"  # 前端回传哨兵：保留原值不覆盖

# 可配置字段清单：(存储键, 环境变量, 是否敏感, 默认值/占位提示)
FIELDS: list = [
    # DeepSeek
    ("deepseek_api_key", "DEEPSEEK_API_KEY", True, None),
    # Qwen / DashScope
    ("qwen_api_key", "QWEN_API_KEY", True, None),
    # OpenAI 兼容（自定义模型的主要入口：任意 base_url + model + key）
    ("openai_api_key", "OPENAI_API_KEY", True, None),
    ("openai_base_url", "OPENAI_BASE_URL", False, "https://api.openai.com/v1"),
    ("openai_model", "OPENAI_MODEL", False, "gpt-4o-mini"),
    # Gemini（OpenAI 兼容端点）
    ("gemini_api_key", "GEMINI_API_KEY", True, None),
    ("gemini_base_url", "GEMINI_BASE_URL", False, "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("gemini_model", "GEMINI_MODEL", False, "gemini-2.0-flash"),
]

_lock = threading.Lock()
# 记录"由本模块从文件写入 os.environ"的变量名。
# 用于区分两类同名环境变量：
#   - 进程启动时已存在（.env / docker -e / 系统环境）→ 优先级更高，不覆盖
#   - 之前由本模块写入 → 可以被后续保存的新值更新或清除
_ENV_OWNED: set = set()


def _load_file() -> Dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("llm_settings.json 读取失败，忽略: %s", exc)
        return {}


def _save_file(data: Dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    # ★ 探索期修复：直接 write_text 覆盖，并发读（get_status / apply_to_environ）
    #   可能读到半截 JSON → json.JSONDecodeError → 返回 {} → 界面短暂显示
    #   "所有 key 未设置"。改为临时文件 + os.replace 原子替换。
    tmp = SETTINGS_PATH.with_suffix(SETTINGS_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # 尽力收紧文件权限（POSIX；Windows 忽略）
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, SETTINGS_PATH)


def mask_secret(value: str) -> str:
    """防窥展示：只露尾 4 位。"""
    if not value:
        return ""
    if len(value) <= 4:
        return "••••"
    return f"••••••••{value[-4:]}"


def apply_to_environ() -> None:
    """把已保存的设置写入 os.environ（启动时 + 保存后调用）。

    ★ 探索期修复：
      1) 旧实现只做 `if val: os.environ[env] = val` —— 用户在设置页**清除** key
         （写入空串）后，os.environ 里的旧值仍在，llm_router 继续用旧 key 计费，
         界面却显示"未设置"。现在空串会显式 `os.environ.pop`。
      2) 文档注释称"已设置的环境变量优先不覆盖"，实现却是无条件覆盖 ——
         运维在 .env 配的新 key 会被 llm_settings.json 里的旧值顶掉。
         改为 setdefault 语义，与注释一致。
    """
    data = _load_file()
    for key, env, _sensitive, _default in FIELDS:
        if key not in data:
            continue
        val = data.get(key)
        if val:
            # 进程启动时注入的环境变量（.env / docker -e）优先，不被文件覆盖；
            # 但之前由本模块写入的值必须能被"改完即生效"更新。
            if env not in os.environ or env in _ENV_OWNED:
                os.environ[env] = val
                _ENV_OWNED.add(env)
        else:
            # 用户清除该字段：只移除本模块写入的值，不动外部注入的环境变量
            if env in _ENV_OWNED:
                os.environ.pop(env, None)
                _ENV_OWNED.discard(env)


def get_status() -> Dict[str, Any]:
    """返回各字段状态（敏感字段脱敏，防窥）。"""
    data = _load_file()
    out: Dict[str, Any] = {"fields": [], "storage_path": str(SETTINGS_PATH)}
    for key, env, sensitive, default in FIELDS:
        saved = data.get(key) or ""
        env_val = os.environ.get(env) or ""
        effective = saved or env_val
        item: Dict[str, Any] = {
            "key": key,
            "env": env,
            "sensitive": sensitive,
            "is_set": bool(effective),
            "source": "file" if saved else ("env" if env_val else None),
        }
        if sensitive:
            # 防窥：只回尾 4 位
            item["masked"] = mask_secret(effective) if effective else None
            item["value"] = None
        else:
            item["value"] = effective or default or ""
            item["default"] = default
        out["fields"].append(item)
    return out


def update(items: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """保存设置。值为 MASK_SENTINEL 表示保留原值；空串表示清除该字段。

    ★ 探索期修复：非字符串值（前端把数字/布尔序列化进来）会让 `val.strip()`
      抛 AttributeError → 端点 500。这里显式转成 str（数字/布尔）或拒绝容器类型。
    """
    with _lock:
        data = _load_file()
        changed = []
        for key, env, _sensitive, _default in FIELDS:
            if key not in items:
                continue
            val = items[key]
            if val is None:
                continue
            if val == MASK_SENTINEL:
                continue  # 前端未修改，保留
            if isinstance(val, bool) or not isinstance(val, (str, int, float)):
                raise ValueError(f"字段 {key} 的值类型不支持（需要字符串），got {type(val).__name__}")
            text = str(val).strip()
            data[key] = text
            changed.append(key)
        _save_file(data)
    apply_to_environ()
    logger.info("LLM 设置已更新并即时生效: %s", changed or "(无变更)")
    return {"saved": changed, "status": get_status()}
