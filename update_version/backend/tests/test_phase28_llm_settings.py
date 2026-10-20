# -*- coding: utf-8 -*-
"""Regression tests for LLM settings persistence (2026-10-19 exploration).

Three defects reproduced against the live module:

1. **Clearing a key did not take effect.** ``update({"openai_api_key": ""})``
   wrote ``""`` to the file, but ``apply_to_environ`` only did
   ``if val: os.environ[env] = val`` — the old key stayed in ``os.environ`` and
   kept being billed while the UI reported "not set".
2. **Non-string values crashed.** ``update({"openai_model": 123})`` raised
   ``AttributeError: 'int' object has no attribute 'strip'`` → endpoint 500.
3. **Documented "env wins" was inverted.** The docstring promised that
   pre-existing environment variables take precedence, but the implementation
   overwrote them unconditionally.

Run: ``python -m pytest update_version/backend/tests/test_phase28_llm_settings.py``
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import pytest

from core import llm_settings


@pytest.fixture()
def isolated_settings(tmp_path, monkeypatch):
    """把 SETTINGS_PATH 指到临时文件，并隔离 os.environ。

    ★ 注意：`llm_settings.update()` 会**主动写 os.environ**（这就是被测行为），
      所以只用 `monkeypatch.delenv` 不够 —— 它只恢复 setup 时记录的旧值，
      测试期间新增的写入会留在进程里，污染后续依赖 LLM env 的测试
      （实测会让 test_phase9_smoke 的 fallback 链断言失败）。
      这里显式快照所有相关变量，teardown 时精确还原。
    """
    path = tmp_path / "llm_settings.json"
    envs = [env for _key, env, _sensitive, _default in llm_settings.FIELDS]
    saved = {env: os.environ.get(env) for env in envs}

    monkeypatch.setattr(llm_settings, "SETTINGS_PATH", path)
    monkeypatch.setattr(llm_settings, "_ENV_OWNED", set())
    for env in envs:
        os.environ.pop(env, None)
    try:
        yield path
    finally:
        for env, value in saved.items():
            if value is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = value


def test_clear_key_removes_it_from_environ(isolated_settings) -> None:
    llm_settings.update({"openai_api_key": "key-1"})
    assert os.environ["OPENAI_API_KEY"] == "key-1"

    llm_settings.update({"openai_api_key": ""})
    assert "OPENAI_API_KEY" not in os.environ, "清除后必须从 os.environ 移除，否则旧 key 继续被使用"
    assert json.loads(isolated_settings.read_text(encoding="utf-8"))["openai_api_key"] == ""


def test_updating_key_takes_effect_immediately(isolated_settings) -> None:
    """改完即生效：第二次保存必须覆盖 module 自己写入的旧值。"""
    llm_settings.update({"openai_api_key": "key-1"})
    llm_settings.update({"openai_api_key": "key-2"})
    assert os.environ["OPENAI_API_KEY"] == "key-2"


def test_externally_injected_env_wins(isolated_settings, monkeypatch) -> None:
    """启动时由 .env / docker -e 注入的值优先级更高，不被文件覆盖。"""
    monkeypatch.setenv("OPENAI_MODEL", "from-dotenv")
    llm_settings.update({"openai_model": "from-file"})
    assert os.environ["OPENAI_MODEL"] == "from-dotenv"


def test_file_value_used_when_env_absent(isolated_settings) -> None:
    llm_settings.update({"openai_model": "from-file"})
    assert os.environ["OPENAI_MODEL"] == "from-file"


@pytest.mark.parametrize("bad", [True, [1], {"a": 1}])
def test_update_rejects_container_types(isolated_settings, bad) -> None:
    with pytest.raises(ValueError, match="值类型不支持"):
        llm_settings.update({"openai_model": bad})


@pytest.mark.parametrize("value,expected", [(123, "123"), (1.5, "1.5")])
def test_update_coerces_scalars_to_string(isolated_settings, value, expected) -> None:
    llm_settings.update({"openai_model": value})
    saved = json.loads(isolated_settings.read_text(encoding="utf-8"))["openai_model"]
    assert saved == expected


def test_save_is_atomic_and_leaves_no_tmp_file(isolated_settings) -> None:
    llm_settings.update({"gemini_model": "g-1"})
    leftovers = list(isolated_settings.parent.glob("*.tmp"))
    assert leftovers == [], f"atomic save must not leave temp files: {leftovers}"
    assert json.loads(isolated_settings.read_text(encoding="utf-8"))["gemini_model"] == "g-1"


def test_masked_sentinel_preserves_original(isolated_settings) -> None:
    llm_settings.update({"openai_api_key": "keep-me"})
    llm_settings.update({"openai_api_key": llm_settings.MASK_SENTINEL})
    saved = json.loads(isolated_settings.read_text(encoding="utf-8"))["openai_api_key"]
    assert saved == "keep-me"
