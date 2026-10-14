# -*- coding: utf-8 -*-
"""
敏感字段加密模块 (Fernet 对称加密)
- 用于持仓成本、API key、推送 webhook 等敏感字段
- 密钥从环境变量读取 (APP_ENCRYPTION_KEY)，缺失则随机生成并落盘
"""
import os
import logging
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_KEY_FILE = Path(__file__).resolve().parent.parent / "data" / ".encryption_key"
_FERNET: Optional[Fernet] = None


def _get_or_create_key() -> bytes:
    """从环境变量或本地文件读取 Fernet key"""
    env_key = os.environ.get("APP_ENCRYPTION_KEY")
    if env_key:
        return env_key.encode("utf-8")
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KEY_FILE.write_bytes(key)
    try:
        _KEY_FILE.chmod(0o600)
    except Exception:
        pass
    logger.warning("Generated new encryption key at %s; keep it safe!", _KEY_FILE)
    return key


def get_fernet() -> Fernet:
    global _FERNET
    if _FERNET is None:
        _FERNET = Fernet(_get_or_create_key())
    return _FERNET


def encrypt_str(plaintext: str) -> str:
    """加密字符串 → base64 字符串"""
    if plaintext is None:
        return ""
    return get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_str(ciphertext: str) -> str:
    """解密 base64 字符串 → 明文"""
    if not ciphertext:
        return ""
    try:
        return get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.error("Invalid encryption token; possibly key rotated")
        return ""


def mask_sensitive(value: str, head: int = 4, tail: int = 4) -> str:
    """脱敏：保留头尾若干字符，中间打码"""
    if not value or len(value) < head + tail + 2:
        return "***"
    return f"{value[:head]}{'*' * (len(value) - head - tail)}{value[-tail:]}"