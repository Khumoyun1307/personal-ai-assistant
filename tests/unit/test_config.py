"""Configuration loading and validation (S00.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from assistant.config import Settings

VALID_DB_URL = "postgresql+psycopg://user:pw@db:5432/assistant"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {"database_url": VALID_DB_URL}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_defaults_are_development_safe(clean_env: None) -> None:
    settings = _settings()
    assert settings.app_env == "dev"
    assert settings.is_production is False
    assert settings.llm_default_model == "claude-opus-5-5"
    assert settings.owner_telegram_ids == ()


def test_database_url_must_use_psycopg(clean_env: None) -> None:
    with pytest.raises(ValidationError) as caught:
        _settings(database_url="postgresql+asyncpg://user:pw@db:5432/assistant")
    assert "database_url" in str(caught.value)


def test_validation_errors_can_be_rendered_without_the_value(clean_env: None) -> None:
    """A URL carries a password, so errors must be rendered by name only.

    `str(ValidationError)` echoes the rejected input and therefore leaks it;
    every caller renders errors through `errors(include_input=False, ...)`
    instead (see `assistant.__main__.healthcheck`). This test pins that the safe
    rendering is in fact free of the value, and that our own validator message
    names the setting rather than quoting it (AGENTS §9, ARCHITECTURE §14.4).
    """
    secret = "sup3r-s3cret-pw"
    with pytest.raises(ValidationError) as caught:
        _settings(database_url=f"mysql://user:{secret}@db:3306/assistant")

    safe = caught.value.errors(include_input=False, include_url=False)
    assert secret not in str(safe)
    assert [item["loc"] for item in safe] == [("database_url",)]
    assert all(secret not in item["msg"] for item in safe)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ()),
        ("123", (123,)),
        ("123,456", (123, 456)),
        (" 123 , 456 ", (123, 456)),
    ],
)
def test_owner_ids_parse_from_comma_separated_env(
    clean_env: None, raw: str, expected: tuple[int, ...]
) -> None:
    assert _settings(owner_telegram_ids=raw).owner_telegram_ids == expected


def test_production_requires_token_key_and_owner(clean_env: None) -> None:
    with pytest.raises(ValidationError) as caught:
        _settings(app_env="prod", log_format="json")
    message = str(caught.value)
    assert "BOT_TOKEN" in message
    assert "ANTHROPIC_API_KEY" in message
    assert "OWNER_TELEGRAM_IDS" in message


def test_production_requires_json_logs(clean_env: None) -> None:
    with pytest.raises(ValidationError, match="LOG_FORMAT"):
        _settings(
            app_env="prod",
            log_format="console",
            bot_token="123:abc",
            anthropic_api_key="sk-ant-x",
            owner_telegram_ids="1",
        )


def test_production_settings_accepted_when_complete(clean_env: None) -> None:
    settings = _settings(
        app_env="prod",
        log_format="json",
        bot_token="123:abc",
        anthropic_api_key="sk-ant-x",
        owner_telegram_ids="1",
    )
    assert settings.is_production is True


def test_secrets_are_not_rendered(clean_env: None) -> None:
    settings = _settings(bot_token="123456:super-secret-token")
    assert "super-secret-token" not in repr(settings)
    assert "super-secret-token" not in str(settings)
    assert settings.bot_token is not None
    assert settings.bot_token.get_secret_value() == "123456:super-secret-token"


def test_settings_are_frozen(clean_env: None) -> None:
    settings = _settings()
    with pytest.raises(ValidationError):
        settings.app_env = "prod"  # type: ignore[misc]


def test_budget_must_be_positive(clean_env: None) -> None:
    with pytest.raises(ValidationError):
        _settings(llm_monthly_budget_usd="0")


@pytest.mark.parametrize(
    "setting",
    [
        "test_database_url",
        "bot_token",
        "anthropic_api_key",
        "llm_monthly_budget_usd",
        "heartbeat_url",
    ],
)
def test_blank_optional_settings_are_treated_as_unset(clean_env: None, setting: str) -> None:
    """`.env` ships these empty; an empty string must not fail validation."""
    assert getattr(_settings(**{setting: ""}), setting) is None


def test_env_example_loads_as_valid_configuration(clean_env: None) -> None:
    """The committed example must itself be a usable dev configuration."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[2] / ".env.example"
    settings = Settings(_env_file=example)  # type: ignore[call-arg]
    assert settings.app_env == "dev"
    assert settings.llm_monthly_budget_usd is None
    assert settings.heartbeat_url is None
