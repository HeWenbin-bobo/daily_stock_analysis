# -*- coding: utf-8 -*-
"""
HMAC 签名模块 — 防止 webhook 伪造 / LLM 用量记录被篡改
- generate_secret() : 生成 base64 url-safe 随机密钥
- sign_payload(secret, body, ts) : 计算 X-Signature 头
- verify_signature(secret, body, ts, signature, max_age=300) : 校验签名（默认 5 分钟有效期）
"""
import base64
import hmac
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_HMAC_FILE = _DATA_DIR / ".llm_usage_hmac_secret"
_SIGNING_KEY: Optional[bytes] = None


def _hmac_file() -> Path:
    """签名密钥文件路径（★ 探索期修复：跟随 db.migrate.DB_PATH 的目录）。

    原先硬编码 `backend/data/`，而数据库路径受 `V4_DB_PATH` 覆盖 ——
    测试隔离或多实例部署时会出现「数据在 A、密钥在 B」，多实例因此共享
    同一把签名密钥，隔离形同虚设。
    """
    try:
        from db.migrate import DB_PATH as _db_path

        return Path(_db_path).parent / ".llm_usage_hmac_secret"
    except Exception:
        return _HMAC_FILE


def _get_or_create_key() -> bytes:
    """从环境变量或本地文件读取 HMAC 签名密钥"""
    import os
    env = os.environ.get("APP_HMAC_KEY")
    if env:
        return env.encode("utf-8")
    key_file = _hmac_file()
    if key_file.exists():
        return key_file.read_bytes()
    key = secrets.token_bytes(32)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(key)
    try:
        key_file.chmod(0o600)
    except Exception:
        pass
    logger.warning("Generated new HMAC key at %s; keep it safe!", key_file)
    return key


def get_signing_key() -> bytes:
    global _SIGNING_KEY
    if _SIGNING_KEY is None:
        _SIGNING_KEY = _get_or_create_key()
    return _SIGNING_KEY


def generate_secret() -> str:
    """生成新的 base64 随机密钥（用于对外共享）"""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")


def _get_verify_key() -> bytes:
    """verify 用共享密钥：优先 APP_WEBHOOK_VERIFY_KEY 环境变量，兜底本地文件。

    ★ 修复(M2)：sign 与 verify 同钥 → 任何能调 /webhooks/sign（admin 保护）的人
    可对任意 body 签名，verify 的防伪意义被削弱。分离：sign 用管理密钥
    （APP_HMAC_KEY/本地文件），verify 用对外共享密钥（APP_WEBHOOK_VERIFY_KEY）。
    """
    import os
    env = os.environ.get("APP_WEBHOOK_VERIFY_KEY")
    if env:
        return env.encode("utf-8")
    # 未配置共享密钥时兜底本地文件（与 sign 同钥，保持向后兼容）
    return get_signing_key()


def canonical_body(body: Any) -> str:
    """把任意 JSON 值规范成参与签名/校验的字符串。

    ★ 探索期修复(签名永远校验失败)：sign 与 verify 此前用了不同的 json.dumps
      参数 —— sign 用 `ensure_ascii=False`（字典按插入序输出），verify 端点用
      `ensure_ascii=False, sort_keys=True`。于是 `{"z":1,"a":2}` 这种键序非字母序
      的 body，签出来的 HMAC 与校验时重算的 HMAC 不同：
      `/webhooks/sign` 签出的签名拿去 `/webhooks/verify` 恒返回 valid:false。
      字符串 body 不受影响，所以原有测试（只测字符串）没发现。
      现在两端共用本函数，序列化规则只有一处。
    """
    if isinstance(body, str):
        return body
    return json.dumps(body, ensure_ascii=False, sort_keys=True)


def sign_payload(body: str, timestamp: Optional[int] = None) -> str:
    """计算 body 的 HMAC-SHA256 签名（sign 端点用管理密钥）

    格式：sha256=<hex>   （时间戳分离，调用方自己加 X-Timestamp 头）

    ★ 探索期修复：body 非字符串时 `body.encode` 抛 AttributeError → 端点 500。
      显式转 str（JSON 里 body 可能是数字/对象）。
    """
    if timestamp is None:
        timestamp = int(time.time())
    msg = f"{timestamp}.".encode("utf-8") + canonical_body(body).encode("utf-8")
    digest = hmac.new(get_signing_key(), msg, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(
    body: str,
    timestamp: int,
    signature: str,
    max_age: int = 300,
) -> bool:
    """校验 webhook 回调签名（带时间窗防重放）

    用共享密钥 verify（与 sign 密钥分离，见 _get_verify_key）。

    Returns:
        True / False
    """
    # 时间窗
    now = int(time.time())
    if abs(now - int(timestamp)) > max_age:
        logger.warning("Signature timestamp out of window: now=%s ts=%s", now, timestamp)
        return False
    # 用共享密钥重算期望签名
    msg = f"{timestamp}.".encode("utf-8") + body.encode("utf-8")
    expected = f"sha256={hmac.new(_get_verify_key(), msg, hashlib.sha256).hexdigest()}"
    # 恒定时间比较
    return hmac.compare_digest(expected, signature)
