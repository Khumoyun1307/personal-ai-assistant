"""Logging configuration (ARCHITECTURE §14.4)."""

from __future__ import annotations

import logging

import pytest

from assistant.config import Settings
from assistant.observability.logging import (
    CHATTY_LOGGERS,
    UnsafeLoggingError,
    assert_production_logging_is_safe,
    configure_logging,
    get_logger,
)

VALID_DB_URL = "postgresql+psycopg://user:pw@db:5432/assistant"
BOT_TOKEN = "8123456789:AAH1bQ7xKqY-ZZ_pLm3nOpQrStUvWxYz012"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"database_url": VALID_DB_URL}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_configured_logger_emits_json_without_content(
    clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(_settings(log_format="json"))
    get_logger("test").info(
        "message_stored",
        conversation_id=7,
        text="Salom, ertaga uchrashamizmi?",
        url=f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
    )
    out = capsys.readouterr().out
    assert '"conversation_id": 7' in out
    assert "Salom" not in out
    assert BOT_TOKEN not in out


def test_prod_rejects_debug_third_party_logger(clean_env: None) -> None:
    settings = _settings(
        app_env="prod",
        log_format="json",
        bot_token="123:abc",
        anthropic_api_key="sk-ant-x",
        owner_telegram_ids="1",
    )
    logger = logging.getLogger(CHATTY_LOGGERS[0])
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        with pytest.raises(UnsafeLoggingError, match=CHATTY_LOGGERS[0]):
            assert_production_logging_is_safe(settings)
    finally:
        logger.setLevel(previous)


def test_prod_rejects_anthropic_debug_env(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    settings = _settings(
        app_env="prod",
        log_format="json",
        bot_token="123:abc",
        anthropic_api_key="sk-ant-x",
        owner_telegram_ids="1",
    )
    with pytest.raises(UnsafeLoggingError, match="ANTHROPIC_LOG"):
        assert_production_logging_is_safe(settings)


def test_dev_logging_is_not_restricted(clean_env: None) -> None:
    assert_production_logging_is_safe(_settings(log_level="DEBUG"))
