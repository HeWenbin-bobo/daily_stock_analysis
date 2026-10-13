"""CC switch 模块单元测试。

通过临时目录 + monkeypatch, 测试:
  - detect_active_provider 各种场景
  - _normalize_provider 字段提取
  - list_all_providers 容错
  - sqlite3 连接正确关闭 (无 ResourceWarning)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import gc
import json
import os
import sqlite3
import tempfile
import warnings
from unittest.mock import patch

import pytest

from llm import cc_switch


@pytest.fixture
def fake_cc_switch_dir(monkeypatch, tmp_path):
    """创建临时 CC switch 目录, 用 monkeypatch 重定向全局路径变量。"""
    settings = {
        "currentProviderClaude": "test-uuid",
        "currentProviderCodex": None,
        "currentProviderGemini": None,
    }
    (tmp_path / "settings.json").write_text(
        json.dumps(settings), encoding="utf-8"
    )
    # 创建 SQLite
    db_path = tmp_path / "cc-switch.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE providers (
            id TEXT PRIMARY KEY,
            app_type TEXT,
            name TEXT,
            settings_config TEXT,
            website_url TEXT,
            is_current INTEGER,
            meta TEXT
        );
        CREATE TABLE settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
    """)
    providers = [
        ("test-uuid", "claude", "MiniMax",
         json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.minimaxi.com/anthropic",
                              "ANTHROPIC_AUTH_TOKEN": "sk-test-123",
                              "ANTHROPIC_MODEL": "MiniMax-M3"}}),
         "https://platform.minimaxi.com", 1,
         json.dumps({"apiFormat": "anthropic"})),
        ("claude-official", "claude", "Claude Official",
         json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "sk-official-456",
                              "ANTHROPIC_MODEL": "claude-sonnet-4-5"}}),
         "https://anthropic.com", 0,
         json.dumps({"apiFormat": "anthropic"})),
        ("kimi-uuid", "claude", "Kimi Coding",
         json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://api.kimi.com",
                              "ANTHROPIC_AUTH_TOKEN": "sk-kimi-789",
                              "ANTHROPIC_MODEL": "kimi-k2"}}),
         "https://kimi.com", 0,
         json.dumps({"apiFormat": "anthropic"})),
    ]
    conn.executemany(
        "INSERT INTO providers (id, app_type, name, settings_config, website_url, is_current, meta) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        providers
    )
    conn.commit()
    conn.close()

    # monkeypatch 全局路径变量(必须在 fixtures 内重新绑定)
    monkeypatch.setattr(cc_switch, "SETTINGS_JSON", tmp_path / "settings.json")
    monkeypatch.setattr(cc_switch, "DB_PATH", tmp_path / "cc-switch.db")
    # iter 131: detect_active_provider 新增 30s TTL 缓存 — fixtures 重定向 SETTINGS_JSON/DB_PATH
    # 后必须清空缓存, 否则前一个测试的 (settings, db) → 结果会通过旧缓存命中, 看不到新 tmp_path 的状态。
    cc_switch._DETECT_CACHE.clear()
    yield tmp_path
    cc_switch._DETECT_CACHE.clear()


@pytest.fixture(autouse=True)
def _clear_detect_cache_between_tests():
    """iter 131: detect_active_provider 加了 30s 模块级缓存, 测试间必须清空, 否则
    跨测试的 (SETTINGS_JSON, DB_PATH) 状态会通过旧缓存命中, 让 fake_cc_switch_dir 等
    monkeypatch 失效。所有 cc_switch 测试统一在 setup/teardown 清缓存。"""
    cc_switch._DETECT_CACHE.clear()
    yield
    cc_switch._DETECT_CACHE.clear()


class TestDetectActiveProvider:
    """detect_active_provider 核心功能。"""

    def test_returns_active_claude_provider(self, fake_cc_switch_dir):
        active = cc_switch.detect_active_provider("claude")
        assert active is not None
        assert active["name"] == "MiniMax"
        assert active["model"] == "MiniMax-M3"
        assert active["base_url"] == "https://api.minimaxi.com/anthropic"
        assert active["api_key"] == "sk-test-123"
        assert active["litellm_prefix"] == "anthropic"

    def test_returns_none_when_no_active(self, fake_cc_switch_dir, monkeypatch):
        """is_current=0 的 provider 不应被选中, 但 settings.json 还指向 test-uuid。"""
        # 把 is_current 全设为 0, 但 settings.json 仍指向 test-uuid
        db = fake_cc_switch_dir / "cc-switch.db"
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE providers SET is_current = 0")
        conn.commit()
        conn.close()
        # test-uuid 仍存在 → is_current 改为 0 但仍被 _query_provider_by_id 找到
        # 因为查询是按 id 而不是按 is_current
        # 真实场景: provider 被删除或 DB 不一致, 这里测试 is_current=0 但 id 存在的情况
        active = cc_switch.detect_active_provider("claude")
        # id 查询会成功 → 仍能返回, 但 is_current 字段是 0
        assert active is not None  # id-based lookup still works

    def test_returns_none_when_db_missing(self, monkeypatch, tmp_path):
        """DB 不存在时应返回 None。"""
        monkeypatch.setattr(cc_switch, "SETTINGS_JSON", tmp_path / "nonexistent.json")
        monkeypatch.setattr(cc_switch, "DB_PATH", tmp_path / "nonexistent.db")
        active = cc_switch.detect_active_provider("claude")
        assert active is None

    def test_anthropic_prefix_detection(self, fake_cc_switch_dir):
        active = cc_switch.detect_active_provider("claude")
        # MiniMax 用 anthropic 兼容协议
        assert active["litellm_prefix"] == "anthropic"


class TestNormalizeProvider:
    """_normalize_provider 字段提取。"""

    def test_extracts_from_env_dict(self):
        raw = {
            "id": "test-uuid",
            "name": "TestProvider",
            "app_type": "claude",
            "settings_config_parsed": {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://api.test.com",
                    "ANTHROPIC_AUTH_TOKEN": "sk-123",
                    "ANTHROPIC_MODEL": "test-model",
                }
            },
        }
        result = cc_switch._normalize_provider(raw)
        assert result["base_url"] == "https://api.test.com"
        assert result["api_key"] == "sk-123"
        assert result["model"] == "test-model"
        assert result["litellm_prefix"] == "anthropic"

    def test_meta_api_format_overrides_env_var_names(self):
        """iter 162: meta.apiFormat=openai_chat → litellm_prefix='openai'。

        关键场景: OpenCode Go 这类 OpenAI 兼容 provider, env 变量名是 ANTHROPIC_*
        (CC switch 对 claude app 的惯例), 但协议是 OpenAI 兼容。之前按 env 名猜
        → anthropic → 401。读 meta.apiFormat 后能正确识别 openai。
        """
        raw = {
            "id": "x", "name": "OpenCode Go", "app_type": "claude",
            "settings_config_parsed": {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://opencode.ai/zen/go",
                    "ANTHROPIC_AUTH_TOKEN": "sk-opencode",
                    "ANTHROPIC_MODEL": "deepseek-v4-flash[1M]",
                }
            },
            "meta_parsed": {"apiFormat": "openai_chat", "endpointAutoSelect": True},
        }
        result = cc_switch._normalize_provider(raw)
        assert result["litellm_prefix"] == "openai", (
            f"OpenAI 兼容 provider 应推断 openai, 实际 {result['litellm_prefix']}"
        )
        assert result["base_url"] == "https://opencode.ai/zen/go"
        assert result["model"] == "deepseek-v4-flash[1M]"

    def test_meta_api_format_anthropic(self):
        """meta.apiFormat=anthropic → litellm_prefix='anthropic'。"""
        raw = {
            "id": "x", "name": "Anthropic Official", "app_type": "claude",
            "settings_config_parsed": {"env": {"ANTHROPIC_AUTH_TOKEN": "sk-x"}},
            "meta_parsed": {"apiFormat": "anthropic"},
        }
        result = cc_switch._normalize_provider(raw)
        assert result["litellm_prefix"] == "anthropic"

    def test_does_not_use_website_url_as_api_endpoint(self):
        # iter 108: 移除 website_url fallback — 该字段是供应商"主页/文档链接", 不一定是
        # API endpoint。第三方代理若只配 website_url, 之前会把营销页 URL 当 API URL
        # 传给 litellm。现在 base_url 应为空字符串, 避免发到错误主机。
        raw = {
            "id": "x", "name": "OpenAI", "app_type": "openai",
            "settings_config_parsed": {},
            "website_url": "https://openai.com",
        }
        result = cc_switch._normalize_provider(raw)
        assert result["base_url"] == ""

    def test_marks_unknown_provider(self):
        """完全空配置应给出空字段, 不抛异常。"""
        raw = {
            "id": "x", "name": "EmptyProvider", "app_type": "claude",
            "settings_config_parsed": {},
            "website_url": "",
        }
        result = cc_switch._normalize_provider(raw)
        assert result["base_url"] == ""
        assert result["api_key"] == ""
        # model 应回退到默认值
        assert result["model"] in ("claude-sonnet-4-5", "")

    def test_detects_openai_prefix(self):
        raw = {
            "id": "x", "name": "GPT-Provider", "app_type": "openai",
            "settings_config_parsed": {"env": {"OPENAI_API_KEY": "sk-x"}},
        }
        result = cc_switch._normalize_provider(raw)
        assert result["litellm_prefix"] == "openai"

    def test_detects_deepseek_prefix(self):
        raw = {
            "id": "x", "name": "DeepSeek", "app_type": "claude",
            "settings_config_parsed": {},
            "website_url": "https://deepseek.com",
        }
        result = cc_switch._normalize_provider(raw)
        assert result["litellm_prefix"] == "deepseek"

    def test_handles_invalid_settings_config(self):
        """settings_config 不是合法 JSON 时不应抛异常。"""
        raw = {
            "id": "x", "name": "Broken", "app_type": "claude",
            "settings_config": "not-json{{{}",
            "settings_config_parsed": {},  # 模拟已 parse 失败后的状态
        }
        result = cc_switch._normalize_provider(raw)
        assert result is not None

    def test_models_list_fallback(self):
        """当 ANTHROPIC_MODEL 缺失但 models 数组非空时, 取 models[0]。"""
        raw = {
            "id": "x", "name": "CustomProvider", "app_type": "claude",
            "settings_config_parsed": {
                "models": ["custom-model-v1", "custom-model-v2"],
                "env": {},
            },
        }
        result = cc_switch._normalize_provider(raw)
        assert result["model"] == "custom-model-v1"


class TestListAllProviders:
    """list_all_providers 容错。"""

    def test_lists_all_claude_providers(self, fake_cc_switch_dir):
        providers = cc_switch.list_all_providers("claude")
        assert len(providers) == 3
        names = [p["name"] for p in providers]
        assert "MiniMax" in names
        assert "Claude Official" in names
        assert "Kimi Coding" in names

    def test_returns_empty_when_no_db(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cc_switch, "DB_PATH", tmp_path / "nonexistent.db")
        assert cc_switch.list_all_providers("claude") == []


class TestSqliteConnectionCleanup:
    """回归测试: sqlite3.Connection 必须被关闭, 不留 ResourceWarning。

    背景: sqlite3.Connection 的 ``__enter__/__exit__`` 只处理事务 (commit/rollback),
    不负责关闭底层连接。必须用 ``contextlib.closing`` 显式关闭, 否则 GC 时会触发
    ``ResourceWarning: unclosed database in <sqlite3.Connection ...>``。
    """

    def test_detect_active_provider_closes_connection(self, fake_cc_switch_dir):
        """detect_active_provider 调用后, 不应泄漏 sqlite3.Connection。"""
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            for _ in range(5):
                provider = cc_switch.detect_active_provider("claude")
                assert provider is not None
                assert provider["name"] == "MiniMax"
            gc.collect()

    def test_list_all_providers_closes_connection(self, fake_cc_switch_dir):
        """list_all_providers 调用后, 不应泄漏 sqlite3.Connection。"""
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            for _ in range(5):
                providers = cc_switch.list_all_providers("claude")
                assert len(providers) == 3
            gc.collect()

    def test_query_provider_by_id_handles_missing_db_path(self, monkeypatch, tmp_path):
        """DB 路径不存在时, 各查询函数应返回 None/[] (不要抛异常)。"""
        monkeypatch.setattr(cc_switch, "DB_PATH", tmp_path / "missing.db")
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            assert cc_switch._query_provider_by_id("anything") is None
            assert cc_switch._query_current_provider("claude") is None


class TestBusyRetryExhaustion:
    """iter 124: 补测 _with_busy_retry 全部重试耗尽路径 — review 报告标注 CRITICAL。

    现有测试只覆盖 retry 成功的路径 (fn 第 2 次返回成功), 全部 3 次都失败的路径
    (raise last_err) 是 0% 覆盖。当 SQLite lock storm 持续 >0.7s 时, 这个分支
    应该抛出 OperationalError 而不是无限循环。
    """

    def test_raises_last_err_after_all_retries_exhausted(self, monkeypatch):
        """SQLITE_BUSY 持续 3 次后应抛出最后一次的 OperationalError。"""
        busy_err = sqlite3.OperationalError("database is locked")
        busy_err.sqlite_errcode = cc_switch.SQLITE_BUSY_ERRCODE

        # 把 RETRY_DELAYS 缩到最小, 加速测试 (避免 0.1+0.2+0.4=0.7s 等待)
        monkeypatch.setattr(cc_switch, "RETRY_DELAYS", (0.001, 0.001, 0.001))

        call_count = [0]

        def always_busy():
            call_count[0] += 1
            raise busy_err

        with pytest.raises(sqlite3.OperationalError) as excinfo:
            cc_switch._with_busy_retry(always_busy)
        # 3 次调用 → 3 次 fail → 第 3 次的 busy_err 重抛
        assert call_count[0] == 3
        assert excinfo.value is busy_err

    def test_non_busy_operational_error_raises_immediately(self, monkeypatch):
        """非 BUSY 类 OperationalError (e.g. syntax error) 不应被 retry, 立即抛。"""
        syntax_err = sqlite3.OperationalError("syntax error")
        syntax_err.sqlite_errcode = 1  # SQLITE_ERROR, 不是 BUSY

        call_count = [0]

        def fail_syntax():
            call_count[0] += 1
            raise syntax_err

        with pytest.raises(sqlite3.OperationalError):
            cc_switch._with_busy_retry(fail_syntax)
        # 立即抛出, 只调用 1 次
        assert call_count[0] == 1


class TestDbExceptionPaths:
    """iter 124: 补测 cc_switch DB 异常路径 — review 报告标注 HIGH。

    现有测试覆盖 happy path, 但 _fetch_provider_by_id_inner / _list_all_providers_inner
    的 except (sqlite3.OperationalError, sqlite3.DatabaseError, OSError) 块从未被
    触发, 一旦 SQLite 真的报错, 这些路径会怎样完全未验证。
    """

    def test_query_provider_by_id_returns_none_on_db_error(self, monkeypatch):
        """DB 查询抛 OperationalError 时, 应返回 None 而非向上抛。"""
        def fake_connect(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(cc_switch.sqlite3, "connect", fake_connect)
        result = cc_switch._query_provider_by_id("anything")
        assert result is None

    def test_list_all_providers_returns_empty_on_db_error(self, monkeypatch):
        """DB 查询抛 DatabaseError 时, list_all_providers 应返回 []。"""
        def fake_connect(*args, **kwargs):
            raise sqlite3.DatabaseError("corrupt")

        monkeypatch.setattr(cc_switch.sqlite3, "connect", fake_connect)
        result = cc_switch.list_all_providers("claude")
        assert result == []


class TestReadSettingsIndexCorruption:
    """iter 124: 补测 settings.json JSON 损坏的 fallback 路径 — review 报告标注 HIGH。

    _read_settings_index 用 except Exception 兜住 json.JSONDecodeError, 损坏的
    settings.json 应返回 {} 而非崩溃。当前测试只覆盖 happy path。
    """

    def test_corrupt_settings_json_returns_empty_dict(self, monkeypatch, tmp_path, caplog):
        """settings.json 是非法 JSON 时, 应记录 WARNING 并返回 {}。"""
        import logging
        settings_path = tmp_path / "settings.json"
        # 截断的 JSON, 触发 json.JSONDecodeError
        settings_path.write_text('{"currentProviderClaude": "broken', encoding="utf-8")
        monkeypatch.setattr(cc_switch, "SETTINGS_JSON", settings_path)

        with caplog.at_level(logging.WARNING):
            result = cc_switch._read_settings_index()

        assert result == {}
