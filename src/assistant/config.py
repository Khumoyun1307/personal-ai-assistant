"""Typed application configuration (ARCHITECTURE §15.2).

Values come from the environment, which Compose populates from the git-ignored
`.env`. Every setting is listed in `.env.example` with a placeholder; secrets are
`SecretStr` so they cannot be printed by accident (AGENTS §9).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

AppEnv = Literal["dev", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogFormat = Literal["json", "console"]

REQUIRED_DB_DRIVER = "postgresql+psycopg"


class ConfigError(Exception):
    """Configuration is missing or invalid. Carries no secret values."""


class Settings(BaseSettings):
    """All settings the application reads. Frozen: nothing mutates config."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # --- Application ---------------------------------------------------------
    app_env: AppEnv = "dev"
    log_level: LogLevel = "INFO"
    log_format: LogFormat = "console"

    # --- Database ------------------------------------------------------------
    database_url: SecretStr
    test_database_url: SecretStr | None = None

    # --- Telegram ------------------------------------------------------------
    bot_token: SecretStr | None = None
    owner_telegram_ids: Annotated[tuple[int, ...], NoDecode] = ()

    # --- Anthropic -----------------------------------------------------------
    anthropic_api_key: SecretStr | None = None
    llm_default_model: str = "claude-opus-5-5"
    llm_monthly_budget_usd: Decimal | None = Field(default=None, gt=0)

    # --- Operations ----------------------------------------------------------
    heartbeat_url: SecretStr | None = None

    @field_validator(
        "test_database_url",
        "bot_token",
        "anthropic_api_key",
        "llm_monthly_budget_usd",
        "heartbeat_url",
        mode="before",
    )
    @classmethod
    def _blank_means_unset(cls, value: object) -> object:
        """`SETTING=` in .env means "not configured", not an empty value."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("database_url", "test_database_url")
    @classmethod
    def _psycopg_driver_required(
        cls, value: SecretStr | None, info: ValidationInfo
    ) -> SecretStr | None:
        """psycopg is required by procrastinate and is SQLAlchemy's default (ADR-06)."""
        if value is None:
            return None
        if not value.get_secret_value().startswith(f"{REQUIRED_DB_DRIVER}://"):
            # The message names the setting only — a URL carries the password.
            raise ValueError(f"{info.field_name} must start with '{REQUIRED_DB_DRIVER}://'")
        return value

    @field_validator("owner_telegram_ids", mode="before")
    @classmethod
    def _parse_owner_ids(cls, value: object) -> object:
        """Accept `123,456` from the environment; JSON decoding is disabled."""
        if not isinstance(value, str):
            return value
        return tuple(int(part) for part in value.replace(" ", "").split(",") if part)

    @field_validator("llm_default_model")
    @classmethod
    def _model_id_without_date_suffix(cls, value: str) -> str:
        """Model ids carry no date suffix (AGENTS §8)."""
        if not value:
            raise ValueError("llm_default_model must not be empty")
        return value

    @model_validator(mode="after")
    def _production_requirements(self) -> Self:
        if self.app_env != "prod":
            return self
        missing = [
            name
            for name, value in (
                ("BOT_TOKEN", self.bot_token),
                ("ANTHROPIC_API_KEY", self.anthropic_api_key),
            )
            if value is None
        ]
        if not self.owner_telegram_ids:
            missing.append("OWNER_TELEGRAM_IDS")
        if missing:
            raise ValueError(f"missing in prod: {', '.join(missing)}")
        if self.log_format != "json":
            raise ValueError("LOG_FORMAT must be 'json' in prod")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "prod"
