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


def resolve_model(route: str, override: str | None) -> ModelChoice:
    """The model for `route`: the override, else the configured route model."""
    try:
        settings = _settings()
    except LiveSetupError:
        if override is None:
            raise
        return ModelChoice(override, None)
    model = override or settings.model_routes.model_for(route)
    effort = settings.llm_owner_request_effort if route == "owner_request" else None
    return ModelChoice(model, effort)


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
