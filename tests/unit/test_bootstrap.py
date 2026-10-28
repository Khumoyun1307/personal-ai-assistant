"""Composition root wiring for the agent (S01.4). No network: constructing the
SDK client does not contact the provider."""

from __future__ import annotations

from typing import Any

import pytest

from assistant import bootstrap
from assistant.config import ConfigError, Settings
from assistant.inbox.notifications import NTF_CARD, NotificationDelivery
from assistant.inbox.triage_trigger import TriageIngestionListener
from assistant.jobs.tasks_inbox import TriageJob
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.telegram.control.alerts import handle_ntf_press
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


@pytest.mark.parametrize(
    ("flags", "llm", "listener", "job"),
    [
        ("", True, False, False),
        ("triage.run", True, True, True),
        # Without a model client (no worker or system role) the job is not
        # registered in this process; the listener and reconciler still are.
        ("triage.run", False, True, False),
    ],
)
def test_m05_wires_triage_only_while_its_flag_is_on(
    clean_env: None, flags: str, llm: bool, listener: bool, job: bool
) -> None:
    """S05.1 in `_wire_m05`: flag `triage.run` adds the ingestion listener,
    the triage reconciler and (with a model client) the `inbox.triage` job;
    without it, nothing of triage is wired."""
    wiring = bootstrap._PhaseWiring()
    bootstrap._wire_m05(
        wiring,
        settings=settings(feature_flags=flags),
        session_factory=None,  # type: ignore[arg-type]
        gateway=None,  # type: ignore[arg-type]
        channel=FakeChannel(),
        clock=FakeClock(),
        scheduler=None,  # type: ignore[arg-type]
        llm=FakeLLM() if llm else None,  # type: ignore[arg-type]
        roles=frozenset({"worker"}),
    )
    listeners = [item for item in wiring.listeners if isinstance(item, TriageIngestionListener)]
    assert len(listeners) == (1 if listener else 0)
    # The notification reconciler is always there; triage adds its own.
    assert len(wiring.reconcilers) == (2 if listener else 1)
    assert wiring.inbox is not None
    assert isinstance(wiring.inbox.triage, TriageJob) is job
