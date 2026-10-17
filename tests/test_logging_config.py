# -*- coding: utf-8 -*-
"""Regression tests for application logging configuration."""

import logging

import pytest

from src.logging_config import LITELLM_LOGGERS, setup_logging


@pytest.fixture(autouse=True)
def restore_logging_state():
    root_logger = logging.getLogger()
    original_root_level = root_logger.level
    original_handlers = list(root_logger.handlers)
    original_litellm_levels = {
        logger_name: logging.getLogger(logger_name).level
        for logger_name in LITELLM_LOGGERS
    }

    yield

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        if handler not in original_handlers:
            handler.close()
    for handler in original_handlers:
        root_logger.addHandler(handler)
    root_logger.setLevel(original_root_level)

    for logger_name, level in original_litellm_levels.items():
        logging.getLogger(logger_name).setLevel(level)


def _read_debug_log(log_dir) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    debug_log = next(log_dir.glob("stock_analysis_debug_*.log"))
    return debug_log.read_text(encoding="utf-8")


def test_log_format_includes_logger_name(tmp_path, monkeypatch):
    monkeypatch.delenv("LITELLM_LOG_LEVEL", raising=False)

    setup_logging(log_prefix="stock_analysis", log_dir=str(tmp_path), debug=False)

    logging.getLogger("src.sample").info("logger context smoke")

    debug_log_text = _read_debug_log(tmp_path)
    assert " | src.sample | " in debug_log_text
    assert "logger context smoke" in debug_log_text


@pytest.mark.parametrize("env_value", [None, "", "  "])
def test_litellm_debug_is_quiet_by_default_and_empty_env(tmp_path, monkeypatch, env_value):
    if env_value is None:
        monkeypatch.delenv("LITELLM_LOG_LEVEL", raising=False)
    else:
        monkeypatch.setenv("LITELLM_LOG_LEVEL", env_value)

    setup_logging(log_prefix="stock_analysis", log_dir=str(tmp_path), debug=False)

    for logger_name in LITELLM_LOGGERS:
        logging.getLogger(logger_name).debug("%s token debug should be filtered", logger_name)
    logging.getLogger("LiteLLM").warning("litellm warning should remain")
    logging.getLogger("src.sample").debug("project debug should remain")

    debug_log_text = _read_debug_log(tmp_path)

    for logger_name in LITELLM_LOGGERS:
        assert f"{logger_name} token debug should be filtered" not in debug_log_text
    assert "litellm warning should remain" in debug_log_text
    assert "project debug should remain" in debug_log_text


def test_litellm_log_level_debug_restores_litellm_debug(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_LOG_LEVEL", "DEBUG")

    setup_logging(log_prefix="stock_analysis", log_dir=str(tmp_path), debug=False)

    for logger_name in LITELLM_LOGGERS:
        logging.getLogger(logger_name).debug("%s debug should remain", logger_name)

    debug_log_text = _read_debug_log(tmp_path)

    for logger_name in LITELLM_LOGGERS:
        assert f"{logger_name} debug should remain" in debug_log_text


def test_invalid_litellm_log_level_falls_back_to_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_LOG_LEVEL", "verbose")

    setup_logging(log_prefix="stock_analysis", log_dir=str(tmp_path), debug=False)

    logging.getLogger("LiteLLM").debug("invalid level debug should be filtered")
    logging.getLogger("LiteLLM").warning("invalid level warning should remain")

    debug_log_text = _read_debug_log(tmp_path)

    assert "invalid level debug should be filtered" not in debug_log_text
    assert "invalid level warning should remain" in debug_log_text
    assert "LITELLM_LOG_LEVEL" in debug_log_text
    assert "已回退为 WARNING" in debug_log_text


def test_log_records_redact_webhook_and_bot_credentials(tmp_path, monkeypatch):
    """Credentials echoed in ``requests`` errors must never reach the log files.

    Connection/HTTP errors from ``requests`` embed the request URL, and webhook /
    bot endpoints carry the token inside the path or query string.
    """
    monkeypatch.delenv("LITELLM_LOG_LEVEL", raising=False)
    setup_logging(log_prefix="stock_analysis", log_dir=str(tmp_path), debug=False)

    telegram_token = "123456:AAHsecretTelegramToken"
    serverchan_key = "SCTsecretServerChanKey"
    dingtalk_token = "DINGsecretAccessToken"

    logger = logging.getLogger("src.notification_sender")
    logger.error("Telegram request failed: Max retries exceeded with url: /bot%s/sendMessage", telegram_token)
    logger.error("ServerChan failed: Max retries exceeded with url: /send/%s.send", serverchan_key)
    logger.error("DingTalk failed: Max retries exceeded with url: /robot/send?access_token=%s", dingtalk_token)

    debug_log_text = _read_debug_log(tmp_path)
    assert telegram_token not in debug_log_text
    assert serverchan_key not in debug_log_text
    assert dingtalk_token not in debug_log_text
    # Diagnostics remain useful: the non-secret context is preserved.
    assert "/sendMessage" in debug_log_text
