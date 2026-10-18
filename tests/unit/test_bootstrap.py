"""Composition root wiring for the agent (S01.4). No network: constructing the
SDK client does not contact the provider."""

from __future__ import annotations

from typing import Any

import pytest

from assistant import bootstrap
from assistant.config import ConfigError, Settings
from assistant.llm.consent import ConsentGatedLLMClient
from tests.fakes.clock import FakeClock
from tests.fakes.llm import FakeLLM

DB = "postgresql+psycopg://user:pw@db:5432/assistant"


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"database_url": DB}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_no_api_key_is_a_configuration_error(clean_env: None) -> None:
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        bootstrap.build_llm_client(settings(), None, FakeClock())  # type: ignore[arg-type]


def test_the_consent_gate_is_the_outermost_layer(clean_env: None) -> None:
    client = bootstrap.build_llm_client(
        settings(openai_api_key="sk-test-synthetic"),
        None,  # type: ignore[arg-type]
        FakeClock(),
    )

    assert isinstance(client, ConsentGatedLLMClient)
    assert client.processor == "OpenAI"


def test_agent_config_comes_from_settings(clean_env: None) -> None:
    config = bootstrap.agent_config(
        settings(
            llm_default_model="gpt-6-luna",
            llm_owner_request_effort="",
            feature_flags="tool.get_status",
            llm_monthly_budget_usd="5",
        )
    )

    assert config.model == "gpt-6-luna"
    assert config.effort is None
    assert config.enabled_flags == frozenset({"tool.get_status"})
    assert str(config.monthly_budget_usd) == "5"


def test_an_injected_llm_is_used_as_is(clean_env: None) -> None:
    fake = FakeLLM()

    runtime = bootstrap.build_owner_runtime(
        settings(),
        None,
        channel=None,
        clock=FakeClock(),
        llm=fake,  # type: ignore[arg-type]
    )

    assert runtime._llm is fake
