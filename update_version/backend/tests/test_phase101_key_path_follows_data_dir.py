"""Phase 101: 密钥文件必须跟随数据目录（V4_DB_PATH）

`core/crypto.py` 与 `core/webhook_signing.py` 原先都把密钥路径硬编码成
`backend/data/.encryption_key` / `.llm_usage_hmac_secret`，而数据库路径由
`db.migrate.DB_PATH` 决定并受 `V4_DB_PATH` 覆盖。

两者不一致时（容器内实测）：

    V4_DB_PATH=/tmp/isolated/x.db
    DB 路径:   /tmp/isolated/x.db
    密钥路径:  /app/update_version/backend/data/.encryption_key
    同一目录:  False

后果：
  - 测试隔离：数据库进了临时目录，密钥却写进生产目录；
  - 多实例部署：多个实例的数据目录不同，却共用同一把加密/签名密钥 ——
    一个实例可以解密另一个实例的敏感字段（webhook URL、SMTP 密码、API key），
    HMAC 隔离也一起失效。

修复：密钥路径改为跟随 `db.migrate.DB_PATH` 的父目录；导入失败时回落到
原先的模块同级 `data/`，行为不变。
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """把 DB 指到临时目录，并重载相关模块"""
    from db import migrate

    monkeypatch.setattr(migrate, "DB_PATH", tmp_path / "v2.db")
    return tmp_path


def test_encryption_key_follows_db_directory(isolated_db):
    from core import crypto

    importlib.reload(crypto)
    try:
        assert crypto._key_file().parent == isolated_db, (
            f"密钥路径 {crypto._key_file()} 没有跟随 DB 目录 {isolated_db}"
        )
    finally:
        importlib.reload(crypto)


def test_hmac_key_follows_db_directory(isolated_db):
    from core import webhook_signing

    importlib.reload(webhook_signing)
    try:
        assert webhook_signing._hmac_file().parent == isolated_db, (
            f"HMAC 密钥路径 {webhook_signing._hmac_file()} 没有跟随 DB 目录 {isolated_db}"
        )
    finally:
        importlib.reload(webhook_signing)


def test_generated_key_is_written_next_to_the_database(isolated_db, monkeypatch):
    """端到端：生成密钥时真的落在 DB 所在目录"""
    from core import crypto

    monkeypatch.delenv("APP_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(crypto, "_FERNET", None)
    importlib.reload(crypto)
    try:
        key = crypto._get_or_create_key()
        assert key
        written = isolated_db / ".encryption_key"
        assert written.exists(), f"密钥没有写到 {written}，实际文件: {list(isolated_db.iterdir())}"
        assert written.read_bytes() == key
    finally:
        importlib.reload(crypto)


def test_env_var_key_still_wins(isolated_db, monkeypatch):
    """显式提供 APP_ENCRYPTION_KEY 时不应落盘"""
    from cryptography.fernet import Fernet

    from core import crypto

    key = Fernet.generate_key()
    monkeypatch.setenv("APP_ENCRYPTION_KEY", key.decode())
    importlib.reload(crypto)
    try:
        got = crypto._get_or_create_key()
        assert got == key
        assert not (isolated_db / ".encryption_key").exists(), "有环境变量时不应写文件"
    finally:
        importlib.reload(crypto)


def test_fallback_when_migrate_unavailable(isolated_db, monkeypatch):
    """db.migrate 导入失败时回落到模块同级 data/（保持旧行为，不炸）"""
    from core import crypto

    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "db.migrate" or name.endswith("db.migrate"):
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    try:
        p = crypto._key_file()
        assert p.name == ".encryption_key"
        assert p.parent.name == "data"
    finally:
        monkeypatch.undo()
        importlib.reload(crypto)
