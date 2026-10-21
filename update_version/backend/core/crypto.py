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

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_KEY_FILE = _DATA_DIR / ".encryption_key"
_FERNET: Optional[Fernet] = None


def _key_file() -> Path:
    """密钥文件的实际路径。

    ★ 探索期修复(配置漂移)：原先密钥路径硬编码为 `backend/data/.encryption_key`，
      而数据库路径由 `db.migrate.DB_PATH` 决定（受 `V4_DB_PATH` 覆盖）。
      两者不一致时——测试隔离、多实例部署、自定义数据目录——会出现
      「数据写到 A，密钥还在 B」：多实例共享同一把密钥（隔离失效），
      测试也会往生产目录写密钥。

      现在优先跟随 `db.migrate.DB_PATH` 的目录；无法导入时回落到模块同级 data/，
      保持与原先完全一致的行为。
    """
    try:
        from db.migrate import DB_PATH as _db_path

        return Path(_db_path).parent / ".encryption_key"
    except Exception:
        return _KEY_FILE


def _get_or_create_key() -> bytes:
    """从环境变量或本地文件读取 Fernet key"""
    env_key = os.environ.get("APP_ENCRYPTION_KEY")
    if env_key:
        return env_key.encode("utf-8")
    key_file = _key_file()
    if key_file.exists():
        return key_file.read_bytes()
    key = Fernet.generate_key()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(key)
    try:
        key_file.chmod(0o600)
    except Exception:
        pass
    logger.warning("Generated new encryption key at %s; keep it safe!", key_file)
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