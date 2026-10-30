"""Typed application configuration (ARCHITECTURE §15.2).

Values come from the environment, which Compose populates from the git-ignored
`.env`. Every setting is listed in `.env.example` with a placeholder; secrets are
`SecretStr` so they cannot be printed by accident (AGENTS §9).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from assistant.core.model_routes import ModelRoutes, processor_for_model

AppEnv = Literal["dev", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
LogFormat = Literal["json", "console"]

REQUIRED_DB_DRIVER = "postgresql+psycopg"

#: Release flag of the background summary refresh (S03.2, M03 D61).
SUMMARY_REFRESH_FLAG = "summary.refresh"

#: Run types with a `LLM_ROUTE_<RUN_TYPE>_MODEL` setting (agent/models.py RUN_TYPES).
ROUTE_RUN_TYPES = (
    "owner_request",
    "triage",
    "draft_reply",
    "summarize",
    "extract_memory",
    "evaluate_monitor",
    "interpret_instruction",
)


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

    # --- LLM provider (OpenAI, ADR-07a) ---------------------------------------
    openai_api_key: SecretStr | None = None
    llm_default_model: str = "gpt-6-luna"
    llm_monthly_budget_usd: Decimal | None = Field(default=None, gt=0)
    #: Reasoning effort for owner requests (ARCHITECTURE §7.4). Empty sends no
    #: effort at all, for a model that does not accept the parameter. `medium`
    #: since owner decision D10 (2026-10-30 owner_drafting eval: 27/31 at
    #: medium vs 26/31 at low, about +3 % cost).
    llm_owner_request_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None
    ) = "medium"
    #: Reasoning effort for `summarize` runs (S03.2); empty sends none.
    llm_summarize_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None
    ) = "low"
    #: Per-route model overrides `LLM_ROUTE_<RUN_TYPE>_MODEL` (ARCHITECTURE
    #: §7.4); empty means `LLM_DEFAULT_MODEL`. The processor behind each
    #: model is checked against consent on every call (`core/model_routes.py`).
    llm_route_owner_request_model: str | None = None
    llm_route_triage_model: str | None = None
    llm_route_draft_reply_model: str | None = None
    llm_route_summarize_model: str | None = None
    llm_route_extract_memory_model: str | None = None
    llm_route_evaluate_monitor_model: str | None = None
    llm_route_interpret_instruction_model: str | None = None

    # --- Feature flags -------------------------------------------------------
    #: Release flags that are ON, e.g. `tool.get_status`. Everything defaults
    #: off (AGENTS §8): a tool reaches the model only when its flag is listed.
    feature_flags: Annotated[frozenset[str], NoDecode] = frozenset()

    # --- Summaries (S03.2, M03 D60) -------------------------------------------
    #: A chat's rolling summary is refreshed after this many quiet minutes...
    summary_refresh_debounce_minutes: int = Field(default=30, ge=1, le=1440)
    #: ...or at the latest this many hours after its first unseen message.
    summary_refresh_max_wait_hours: int = Field(default=6, ge=1, le=168)

    # --- Assisted replies and inbox intelligence (P2) ------------------------
    approval_max_age_hours: int = Field(default=12, ge=1, le=24)
    draft_debounce_seconds: int = Field(default=20, ge=5, le=300)
    triage_debounce_seconds: int = Field(default=20, ge=5, le=300)
    monitor_debounce_seconds: int = Field(default=20, ge=5, le=300)
    mark_uncertain_after_seconds: int = Field(default=120, ge=120)
    connection_stale_alert_minutes: int = Field(default=30, ge=1)
    #: The draft writer's effort (`draft_reply.v1` and `draft_owner`): `medium`
    #: since D10, with `owner_request` (the eval above).
    llm_draft_reply_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None
    ) = "medium"
    llm_triage_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None
    ) = "low"
    llm_evaluate_monitor_effort: (
        Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None
    ) = "low"

    # --- Privacy ---------------------------------------------------------------
    #: The published privacy policy (FR-PRV-01), shown by /privacy.
    privacy_policy_url: str | None = None

    # --- Operations ----------------------------------------------------------
    heartbeat_url: SecretStr | None = None
    #: Where the backup sidecar writes its last-success timestamp (production
    #: Compose mounts the backups volume read-only at /backups). Empty disables
    #: the daily backup_check alert (ARCHITECTURE §17).
    backup_status_path: Path | None = None
    #: The backup sidecar's rclone remote (e.g. `b2:<bucket>`). The app only
    #: needs to know whether one is configured: then backup_check also alerts
    #: when the off-site upload marker goes stale.
    backup_remote: str | None = None

    @field_validator(
        "test_database_url",
        "bot_token",
        "openai_api_key",
        "llm_monthly_budget_usd",
        "llm_owner_request_effort",
        "llm_summarize_effort",
        "llm_draft_reply_effort",
        "llm_triage_effort",
        "llm_evaluate_monitor_effort",
        "llm_route_owner_request_model",
        "llm_route_triage_model",
        "llm_route_draft_reply_model",
        "llm_route_summarize_model",
        "llm_route_extract_memory_model",
        "llm_route_evaluate_monitor_model",
        "llm_route_interpret_instruction_model",
        "privacy_policy_url",
        "heartbeat_url",
        "backup_status_path",
        "backup_remote",
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

    @field_validator("feature_flags", mode="before")
    @classmethod
    def _parse_flags(cls, value: object) -> object:
        """Accept `tool.a,tool.b` from the environment; JSON decoding is disabled."""
        if not isinstance(value, str):
            return value
        return frozenset(part for part in value.replace(" ", "").split(",") if part)

    @field_validator("llm_default_model")
    @classmethod
    def _model_id_without_date_suffix(cls, value: str) -> str:
        """Model ids come from configuration, never hard-coded (AGENTS §8)."""
        if not value:
            raise ValueError("llm_default_model must not be empty")
        return value

    @model_validator(mode="after")
    def _every_route_has_a_known_processor(self) -> Self:
        """A model no processor serves would only fail later, at the consent
        wrapper, on every call; refuse it at startup and in `healthcheck`,
        naming the setting (never its value)."""
        settings = {"LLM_DEFAULT_MODEL": self.llm_default_model} | {
            f"LLM_ROUTE_{run_type.upper()}_MODEL": model
            for run_type in ROUTE_RUN_TYPES
            if (model := getattr(self, f"llm_route_{run_type}_model"))
        }
        unknown = [name for name, model in settings.items() if processor_for_model(model) is None]
        if unknown:
            raise ValueError(f"no known processor serves the model in: {', '.join(unknown)}")
        return self

    @model_validator(mode="after")
    def _production_requirements(self) -> Self:
        if self.app_env != "prod":
            return self
        missing = [
            name
            for name, value in (
                ("BOT_TOKEN", self.bot_token),
                ("OPENAI_API_KEY", self.openai_api_key),
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

    @property
    def summaries_enabled(self) -> bool:
        """Background summary refreshes run only with flag `summary.refresh`
        (M03 D61); the catch-up and attention tools summarize on demand."""
        return SUMMARY_REFRESH_FLAG in self.feature_flags

    @property
    def model_routes(self) -> ModelRoutes:
        """The model per run type (ARCHITECTURE §7.4)."""
        overrides = {
            run_type: model
            for run_type in ROUTE_RUN_TYPES
            if (model := getattr(self, f"llm_route_{run_type}_model"))
        }
        return ModelRoutes(default=self.llm_default_model, overrides=overrides)
