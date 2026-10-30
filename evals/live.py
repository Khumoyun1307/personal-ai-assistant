"""The real model for `--live` runs: configuration in, an `LLMClient` out.

Reached only after the runner has checked `--live` and the cost cap. Models
and the API key come from the application settings, never from this code
(AGENTS §8): the route's model is `LLM_ROUTE_<ROUTE>_MODEL`, else
`LLM_DEFAULT_MODEL`, unless `--model` overrides it for a model-choice
comparison. Nothing here prints a secret or a settings value.

The client is the product's OpenAI adapter (official SDK, Responses API,
`store=False`); no provider file, batch or feedback API is used. Evaluation
runs send **synthetic** data only, so the consent wrapper (which guards the
owner's data) is not part of the stack; the budget guard is.

This module is the only place in `evals/` that builds an adapter, and it is
never exercised by the tests (they use `FakeLLM`); its failure paths are.
"""

from __future__ import annotations

from dataclasses import dataclass

from openai import AsyncOpenAI
from pydantic import SecretStr, ValidationError

from assistant.agent.drafting import DraftConfig
from assistant.config import Settings
from assistant.core.model_routes import processor_for_model
from assistant.llm.openai_adapter import OpenAIClient
from assistant.llm.port import LLMClient
from evals.errors import EvalsError

#: No SDK retries: they happen invisibly, so the budget ledger would miss
#: billed attempts (the product's client retries twice; evaluation runs must
#: count every call). A failed call is billed at its worst case instead.
OPENAI_MAX_RETRIES = 0


class LiveSetupError(EvalsError):
    """The live run cannot be configured. Never carries a value."""


@dataclass(frozen=True, slots=True)
class ModelChoice:
    model: str
    effort: str | None


@dataclass(frozen=True, slots=True)
class LiveConfig:
    choice: ModelChoice
    api_key: SecretStr


def _settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]  # values come from the environment
    except ValidationError as error:
        # Field names only: the error text can echo the offending value.
        names = sorted({str(e["loc"][0]) for e in error.errors() if e["loc"]})
        raise LiveSetupError(
            "the settings are invalid (" + ", ".join(names) + "); run `just healthcheck`"
        ) from None


#: Routes whose effort is the product's fixed default, not a setting: drafting
#: (S04.2) sends `DraftConfig.effort`. E2's baseline stays pinned at that `low`
#: on purpose: D10 (2026-10-30) moved the product's `LLM_DRAFT_REPLY_EFFORT` to
#: `medium`, but E2's runs stay one series (re-pinning it is a TL decision).
_FIXED_EFFORTS: dict[str, str | None] = {"draft_reply": DraftConfig(model="").effort}


def resolve_model(route: str, override: str | None) -> ModelChoice:
    """The model for `route`: the override, else the configured route model."""
    try:
        settings = _settings()
    except LiveSetupError:
        if override is None:
            raise
        return ModelChoice(override, _FIXED_EFFORTS.get(route))
    model = override or settings.model_routes.model_for(route)
    # The effort the product sends on that route, so the evaluation measures what runs.
    efforts = {
        "owner_request": settings.llm_owner_request_effort,
        "summarize": settings.llm_summarize_effort,
        "triage": settings.llm_triage_effort,
        "evaluate_monitor": settings.llm_evaluate_monitor_effort,
        **_FIXED_EFFORTS,
    }
    return ModelChoice(model, efforts.get(route))


def draft_owner_choice(override: str | None) -> ModelChoice:
    """The model and effort of an owner-requested draft as the product sends it
    (`bootstrap.draft_config`: the `draft_reply` route's model and
    `LLM_DRAFT_REPLY_EFFORT`), for the `owner_drafting` target. Unlike E2's
    fixed baseline effort, this follows the setting, so the low and medium
    runs the owner compares (D10) are set the way production would be."""
    try:
        settings = _settings()
    except LiveSetupError:
        if override is None:
            raise
        return ModelChoice(override, Settings.model_fields["llm_draft_reply_effort"].default)
    model = override or settings.model_routes.model_for("draft_reply")
    return ModelChoice(model, settings.llm_draft_reply_effort)


def load_live_config(route: str, override: str | None) -> LiveConfig:
    """Everything a real call needs, or `LiveSetupError`."""
    settings = _settings()
    choice = resolve_model(route, override)
    if processor_for_model(choice.model) != OpenAIClient.processor:
        raise LiveSetupError(
            "the configured model is not served by the OpenAI adapter; evaluation runs "
            "call the model only through it"
        )
    if settings.openai_api_key is None:
        raise LiveSetupError("OPENAI_API_KEY is not set")
    return LiveConfig(choice, settings.openai_api_key)


def build_client(config: LiveConfig) -> LLMClient:
    sdk = AsyncOpenAI(api_key=config.api_key.get_secret_value(), max_retries=OPENAI_MAX_RETRIES)
    return OpenAIClient(sdk)
