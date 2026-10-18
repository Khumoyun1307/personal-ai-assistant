"""Composition root.

The only module that instantiates adapters and wires them to ports
(AGENTS §5, ARCHITECTURE §5.2). The running process (S01.5) calls these
factories; nothing else constructs an SDK client.
"""

from __future__ import annotations

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.agent.persistence import DbCallRecorder, DbConsentReader
from assistant.agent.ports import ControlChannel
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime
from assistant.agent.tools.registry import default_registry
from assistant.config import ConfigError, Settings
from assistant.core.clock import Clock
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.llm.metering import MeteredLLMClient
from assistant.llm.openai_adapter import OpenAIClient
from assistant.llm.port import LLMClient

#: SDK-level retries for connection errors, 429 and 5xx. Safe for a model call
#: (it changes nothing in the world); never applied to Telegram sends.
OPENAI_MAX_RETRIES = 2


def build_llm_client(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> LLMClient:
    """OpenAI behind the consent gate and metering, in that order.

    Consent is outermost so that a call without consent never reaches even the
    metering layer, let alone the provider (§6 invariant 9).
    """
    if settings.openai_api_key is None:
        raise ConfigError("OPENAI_API_KEY is not set")
    sdk = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(), max_retries=OPENAI_MAX_RETRIES
    )
    metered = MeteredLLMClient(OpenAIClient(sdk), DbCallRecorder(session_factory, clock))
    return ConsentGatedLLMClient(metered, DbConsentReader(session_factory))


def agent_config(settings: Settings) -> AgentConfig:
    return AgentConfig(
        model=settings.llm_default_model,
        effort=settings.llm_owner_request_effort,
        monthly_budget_usd=settings.llm_monthly_budget_usd,
        enabled_flags=settings.feature_flags,
    )


def build_owner_runtime(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    channel: ControlChannel,
    clock: Clock,
    llm: LLMClient | None = None,
) -> OwnerRequestRuntime:
    return OwnerRequestRuntime(
        session_factory=session_factory,
        llm=llm or build_llm_client(settings, session_factory, clock),
        channel=channel,
        registry=default_registry(),
        clock=clock,
        config=agent_config(settings),
    )
