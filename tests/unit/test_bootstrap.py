"""Composition root wiring for the agent (S01.4). No network: constructing the
SDK client does not contact the provider."""

from __future__ import annotations

from typing import Any

import pytest

from assistant import bootstrap
from assistant.agent.monitor_eval import MonitorEvaluator
from assistant.automation.monitor_trigger import MonitorIngestionListener
from assistant.automation.monitors import MON_CARD, render_monitor_match
from assistant.config import ConfigError, Settings
from assistant.inbox.notification_types import NotificationKind
from assistant.inbox.notifications import NTF_CARD, NotificationDelivery
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.telegram.control.alerts import handle_ntf_press
from assistant.telegram.control.monitors import handle_mon_press
from tests.fakes.channel import FakeChannel
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


@pytest.mark.parametrize("flags", ["", "notifications.deliver"])
def test_m05_wires_notification_delivery_its_reconciler_and_the_alert_cards(
    clean_env: None, flags: str
) -> None:
    """S05.2 in `_wire_m05`: the `notify.deliver` handler, its reconciler and
    the `ntf` card route are always wired; flag `notifications.deliver`
    decides whether standard alerts go out (system alerts never wait)."""
    wiring = bootstrap._PhaseWiring()
    bootstrap._wire_m05(
        wiring,
        settings=settings(feature_flags=flags),
        session_factory=None,  # type: ignore[arg-type]
        gateway=None,  # type: ignore[arg-type]
        channel=FakeChannel(),
        clock=FakeClock(),
        scheduler=None,  # type: ignore[arg-type]
        llm=None,
        roles=frozenset({"worker"}),
    )
    assert wiring.inbox is not None
    assert isinstance(wiring.inbox.notifications, NotificationDelivery)
    assert wiring.notifications_enabled is bool(flags)
    assert wiring.routes.callbacks[NTF_CARD] is handle_ntf_press
    assert len(wiring.reconcilers) == 1


@pytest.mark.parametrize("flags", ["", "monitors.run"])
def test_m05_wires_monitors_and_matches_only_with_the_flag(clean_env: None, flags: str) -> None:
    """S05.4 in `_wire_m05`: the `MONITOR_MATCH` renderer, the Stop button
    and the hourly expiry are always wired; the listener, the evaluation job
    and its reconciler only with flag `monitors.run`."""
    wiring = bootstrap._PhaseWiring()
    bootstrap._wire_m05(
        wiring,
        settings=settings(feature_flags=flags),
        session_factory=None,  # type: ignore[arg-type]
        gateway=None,  # type: ignore[arg-type]
        channel=FakeChannel(),
        clock=FakeClock(),
        scheduler=None,  # type: ignore[arg-type]
        llm=FakeLLM(),  # type: ignore[arg-type]
        roles=frozenset({"worker"}),
    )
    assert wiring.routes.callbacks[MON_CARD] is handle_mon_press
    assert wiring.inbox is not None
    assert wiring.inbox.notifications is not None
    renderers = wiring.inbox.notifications._renderers
    assert renderers[NotificationKind.MONITOR_MATCH] is render_monitor_match
    assert wiring.automation is not None
    assert wiring.automation.monitors is not None
    listeners = [type(listener) for listener in wiring.listeners]
    if flags:
        assert listeners == [MonitorIngestionListener]
        assert isinstance(wiring.automation.monitors.evaluator, MonitorEvaluator)
        assert len(wiring.reconcilers) == 2
    else:
        assert listeners == []
        assert wiring.automation.monitors.evaluator is None
        assert len(wiring.reconcilers) == 1
