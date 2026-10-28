"""S05.4 monitor tools through the real agent loop, the confirmation gate,
the applier and the Stop button (S05.4 §3.2; ARCHITECTURE §8.2, §8.3;
FR-TRI-06).

Required tests, findable by name (S05.4 §5, the AGENTS §8 tool tests):
`test_create_monitor_always_asks_for_confirmation_and_creates_nothing_before`,
`test_confirm_creates_one_monitor_and_a_retry_creates_no_second` (idempotent
retry), `test_create_monitor_refuses_a_foreign_contact_id` (RG-21),
`test_a_tainted_run_cannot_create_or_stop_a_monitor_without_a_tap` (RG-11),
`test_list_monitors_wraps_names_and_taints_the_run` (invariant 3b),
`test_stop_monitor_is_idempotent`, `test_the_stop_button_works_only_for_the_owner`,
`test_the_tool_result_never_says_active_before_confirmation` (RG-20 style).
Schema-valid but semantically invalid arguments:
`test_a_monitor_on_an_ignored_chat_or_past_its_expiry_is_refused` (and the
unit tests in `tests/unit/test_monitor_args.py`); registry filtering:
`test_the_registry_offers_monitor_tools_only_with_their_flags`.

Synthetic text only; FakeLLM and FakeChannel stand in for the provider and
the control chat (AGENTS §11).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.accounts.models import AuditLogEntry, PendingInput
from assistant.agent.models import AgentRun, ToolConfirmation
from assistant.agent.monitor_schemas import CreateMonitorArgs
from assistant.agent.tools.monitors import create_monitor_tool
from assistant.agent.tools.registry import (
    CapabilityTier,
    ToolContext,
    ToolServices,
    default_registry,
)
from assistant.automation import monitors
from assistant.automation.models import Automation
from assistant.automation.monitors import MON_CARD
from assistant.automation.rules import MonitorRule
from assistant.db.uow import transaction
from assistant.llm.port import LLMRequest, ToolResultItem
from assistant.telegram.control.monitors import handle_mon_press
from assistant.telegram.control.routes import ControlRoutes
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import (
    MONITOR_FLAGS,
    READ_FLAGS,
    applier,
    args,
    ask,
    one_id,
    runtime,
)
from tests.integration.business_env import (
    CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    callback,
    connect,
    connection_update,
    deliver,
    environment,
    grant_managed,
    onboard,
)

pytestmark = pytest.mark.integration

FLAGS = READ_FLAGS | MONITOR_FLAGS
OTHER_OWNER = 7000000002
FORWARD = {"forward_origin": {"type": "hidden_user", "date": T_BASE, "sender_user_name": "N"}}


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    """Two owners, and the Stop button routed as `_wire_m05` routes it."""
    async with environment(
        migrated_url,
        owner_ids=frozenset({OWNER, OTHER_OWNER}),
        routes=ControlRoutes(callbacks={MON_CARD: handle_mon_press}),
    ) as built:
        yield built


async def seed(env: Env, *, managed: bool = False) -> tuple[int, int]:
    """A connected owner and Kamola's chat; returns (conversation id, contact id)."""
    await connect(env)
    if managed:
        await grant_managed(env)
    await deliver(env, business_message(message_id=1, message_text="SYNTH-A", first_name="Kamola"))
    conversation = await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=CONTACT
    )
    contact = await one_id(
        env, "SELECT id FROM app.contacts WHERE telegram_user_id = :u", u=CONTACT
    )
    return conversation, contact


async def owner_account(env: Env, owner: int = OWNER) -> int:
    return await one_id(env, "SELECT id FROM app.accounts WHERE telegram_user_id = :u", u=owner)


def when_replies(contact_id: int, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "scope": {"contact_ids": [], "conversation_ids": [], "all_observed": False},
        "condition": {
            "kind": "contact_replied",
            "contact_id": contact_id,
            "terms": [],
            "description": None,
            "media_kinds": [],
        },
        "action": "alert",
        "lifecycle": "one_shot",
        "expires_at": None,
        "bypass_quiet_hours": False,
    }
    values.update(overrides)
    return values


def results(request: LLMRequest) -> list[dict[str, Any]]:
    return [json.loads(i.output) for i in request.items if isinstance(i, ToolResultItem)]


def outputs(request: LLMRequest) -> list[str]:
    return [i.output for i in request.items if isinstance(i, ToolResultItem)]


async def automations(env: Env) -> list[Automation]:
    async with env.factory() as session:
        return list((await session.execute(select(Automation).order_by(Automation.id))).scalars())


async def events(env: Env, event: str) -> list[AuditLogEntry]:
    async with env.factory() as session:
        rows = await session.execute(select(AuditLogEntry).where(AuditLogEntry.event == event))
        return list(rows.scalars())


async def ask_create(
    env: Env, contact_id: int, channel: FakeChannel, **extra: Any
) -> tuple[FakeLLM, dict[str, Any]]:
    turn = await ask(env, "Notify me when Kamola replies", **extra)
    fake = FakeLLM(
        tool_response(("c1", "create_monitor", json.dumps(when_replies(contact_id)))),
        text_response("It waits for your confirmation."),
    )
    await runtime(env, fake, channel, flags=FLAGS, registry=default_registry()).run(turn)
    [result] = results(fake.requests[1])
    return fake, result


async def confirm(env: Env) -> int:
    async with env.factory() as session:
        row = (await session.execute(select(ToolConfirmation))).scalar_one()
        card = (
            await session.execute(select(PendingInput).where(PendingInput.kind == "tool_confirm"))
        ).scalar_one()
    await deliver(
        env, callback(f"tool_confirm:{card.token}:confirm", message_id=card.prompt_message_id)
    )
    return row.id


async def direct_monitor(env: Env, contact_id: int, *, lifecycle: str = "one_shot") -> int:
    """A confirmed monitor made without the agent loop (as the applier would)."""
    rule = MonitorRule.model_validate(
        {
            "scope": {"contact_ids": [contact_id]},
            "condition": {
                "kind": "semantic",
                "description": "SYNTH-DESC the price",
            },
            "action": "alert",
            "lifecycle": lifecycle,
        }
    )
    async with transaction(env.factory) as uow:
        row, _ = await monitors.create(
            uow.session,
            account_id=await owner_account(env),
            rule=rule,
            expires_at=None,
            timezone="UTC",
            clock=env.clock,
            run_id=None,
            tool_call_id=None,
        )
    return row.id


# ------------------------------------------------------------ create_monitor
async def test_create_monitor_always_asks_for_confirmation_and_creates_nothing_before(
    env: Env,
) -> None:
    """FR-TRI-06: an untainted run's call is a card too; nothing exists
    before the tap, and the card says in plain words what will be watched."""
    _, contact = await seed(env)
    channel = FakeChannel()
    _, result = await ask_create(env, contact, channel)
    assert result["status"] == "awaiting_confirmation"
    assert await automations(env) == []
    card, reply = channel.sends  # the card, then the run's own reply
    assert reply == "It waits for your confirmation."
    assert "Confirm a change?" in card
    assert "Alert you once when Kamola writes (ends after 30 days)" in card
    assert "Nothing has been changed yet." in card
    assert [b.label for b in channel.buttons[0]] == ["Confirm", "Reject"]


async def test_confirm_creates_one_monitor_and_a_retry_creates_no_second(env: Env) -> None:
    conversation, contact = await seed(env)
    await ask_create(env, contact, FakeChannel())
    confirmation_id = await confirm(env)
    applied_channel = FakeChannel()
    outcome = await applier(env, applied_channel, flags=FLAGS).apply(confirmation_id)
    assert (outcome.status, outcome.tool_status) == ("applied", "ok")

    [row] = await automations(env)
    assert (row.kind, row.status, row.tz_policy, row.next_run_at) == (
        "monitor",
        "active",
        "fixed",
        None,
    )
    assert row.confirmed_at == env.clock.now()
    assert row.expires_at == env.clock.now() + timedelta(days=30)
    assert row.refs == {"conversation_ids": [], "contact_ids": [contact]}
    assert row.spec["condition"]["contact_id"] == contact
    notice, done = applied_channel.sends
    assert notice.startswith("🔔 <b>Monitor on:</b> Alert you once when Kamola writes")
    assert [b.label for b in applied_channel.buttons[0]] == ["Stop monitor"]
    assert "Done" in done
    assert len(await events(env, "monitor_created")) == 1

    # A retried call (same run, same call id) creates nothing more.
    assert row.run_id is not None
    assert row.tool_call_id == "c1"
    retry = await create_monitor_tool(
        ToolContext(
            account_id=row.account_id,
            run_id=row.run_id,
            run_type="owner_request",
            tainted=False,
            services=ToolServices(
                session_factory=env.factory,
                clock=env.clock,
                monthly_budget_usd=None,
                channel=FakeChannel(),
            ),
            call_id="c1",
            held=True,
        ),
        CreateMonitorArgs.model_validate(when_replies(contact)),
    )
    assert (retry.status, retry.data["changed"], retry.data["monitor_id"]) == ("ok", False, row.id)
    assert len(await automations(env)) == 1
    assert len(await events(env, "monitor_created")) == 1
    # The applier never runs a confirmation twice.
    again = await applier(env, FakeChannel(), flags=FLAGS).apply(confirmation_id)
    assert again.status == "skipped"
    del conversation


async def test_create_monitor_refuses_a_foreign_contact_id(env: Env) -> None:
    """RG-21: another account's contact is "not found" — no card, no row —
    exactly like an id that does not exist."""
    await seed(env)
    await onboard(env, OTHER_OWNER)
    await deliver(env, connection_update(user_id=OTHER_OWNER, connection_id="synthetic-conn-b"))
    await deliver(
        env,
        business_message(
            "synthetic-conn-b", from_id=7000000077, chat_id=7000000077, message_text="theirs"
        ),
    )
    theirs = await one_id(
        env, "SELECT id FROM app.contacts WHERE telegram_user_id = :u", u=7000000077
    )
    for foreign in (theirs, 987654321):
        channel = FakeChannel()
        turn = await ask(env, "Notify me when they reply")
        fake = FakeLLM(
            tool_response(("c1", "create_monitor", json.dumps(when_replies(foreign)))),
            text_response("Not found."),
        )
        await runtime(env, fake, channel, flags=FLAGS, registry=default_registry()).run(turn)
        [result] = results(fake.requests[1])
        assert result == {"status": "error", "reason": "not found"}
        assert channel.sends[:-1] == []  # only the reply; no card
    assert await automations(env) == []


async def test_a_monitor_on_an_ignored_chat_or_past_its_expiry_is_refused(env: Env) -> None:
    """Schema-valid, semantically invalid: the card says why it cannot
    start; the handler refuses with a factual error and creates nothing."""
    conversation, contact = await seed(env)
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = 'ignore' WHERE id = :c"),
            {"c": conversation},
        )
    channel = FakeChannel()
    await ask_create(env, contact, channel)
    assert "This monitor cannot start: that chat is ignored" in channel.sends[0]
    confirmation_id = await confirm(env)
    applied = FakeChannel()
    outcome = await applier(env, applied, flags=FLAGS).apply(confirmation_id)
    assert outcome.status == "failed"
    assert "that chat is ignored" in applied.sends[-1]
    assert await automations(env) == []

    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.conversations SET mode = 'observe' WHERE id = :c"),
            {"c": conversation},
        )
    past = CreateMonitorArgs.model_validate(when_replies(contact, expires_at="2026-10-18 08:00"))
    refused = await create_monitor_tool(
        ToolContext(
            account_id=await owner_account(env),
            run_id=0,
            run_type="owner_request",
            tainted=False,
            services=ToolServices(
                session_factory=env.factory, clock=env.clock, monthly_budget_usd=None
            ),
            held=True,
        ),
        past,
    )
    assert refused.status == "error"
    assert "future" in refused.data["reason"]
    assert await automations(env) == []


async def test_the_tool_result_never_says_active_before_confirmation(env: Env) -> None:
    """RG-20 style (§6 invariant 11): before the tap the model hears only
    `awaiting_confirmation`; the handler itself refuses without the tap."""
    _, contact = await seed(env)
    fake, result = await ask_create(env, contact, FakeChannel())
    assert result["status"] == "awaiting_confirmation"
    assert all("active" not in output for output in outputs(fake.requests[1]))
    unconfirmed = await create_monitor_tool(
        ToolContext(
            account_id=await owner_account(env),
            run_id=0,
            run_type="owner_request",
            tainted=False,
            services=ToolServices(
                session_factory=env.factory, clock=env.clock, monthly_budget_usd=None
            ),
            held=False,
        ),
        CreateMonitorArgs.model_validate(when_replies(contact)),
    )
    assert unconfirmed.status == "not_executed"
    assert "active" not in json.dumps(dict(unconfirmed.data))
    assert await automations(env) == []


# ------------------------------------------------------------ taint, list, stop
async def test_a_tainted_run_cannot_create_or_stop_a_monitor_without_a_tap(env: Env) -> None:
    """RG-11 (§6 invariant 3b): a run tainted by a forwarded message gets a
    card for both calls; nothing changes until the owner taps."""
    _, contact = await seed(env)
    existing = await direct_monitor(env, contact)
    channel = FakeChannel()
    turn = await ask(env, "Synthetic forwarded request", **FORWARD)
    fake = FakeLLM(
        tool_response(
            ("c1", "create_monitor", json.dumps(when_replies(contact))),
            ("c2", "stop_monitor", args(monitor_id=existing)),
        ),
        text_response("Waiting for your taps."),
    )
    await runtime(env, fake, channel, flags=FLAGS, registry=default_registry()).run(turn)
    created, stopped = results(fake.requests[1])
    assert (created["status"], stopped["status"]) == ("awaiting_confirmation",) * 2
    assert [(r.id, r.status) for r in await automations(env)] == [(existing, "active")]
    cards = [s for s in channel.sends if "Confirm a change?" in s]
    assert len(cards) == 2
    assert all("text from other people" in card for card in cards)
    assert any("Stop this monitor:" in card for card in cards)


async def test_list_monitors_wraps_names_and_taints_the_run(env: Env) -> None:
    conversation, contact = await seed(env, managed=True)
    monitor_id = await direct_monitor(env, contact, lifecycle="standing")
    turn = await ask(env, "What monitors do I have?")
    fake = FakeLLM(
        tool_response(("c1", "list_monitors", args())),
        text_response("One monitor."),
    )
    await runtime(env, fake, FakeChannel(), flags=FLAGS, registry=default_registry()).run(turn)
    [result] = results(fake.requests[1])
    assert result["status"] == "ok"
    [item] = result["monitors"]
    assert (item["monitor_id"], item["status"], item["condition"], item["lifecycle"]) == (
        monitor_id,
        "active",
        "semantic",
        "standing",
    )
    assert item["scope"]["contacts"] == [
        {
            "contact_id": contact,
            "name": '<untrusted source="contact_name" trust="untrusted">Kamola</untrusted>',
        }
    ]
    assert item["description"] == (
        '<untrusted source="monitor_spec" trust="untrusted">SYNTH-DESC the price</untrusted>'
    )
    async with env.factory() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.run_type == "owner_request"))
        ).scalar_one()
    assert run.tainted is True
    del conversation


async def test_stop_monitor_is_idempotent(env: Env) -> None:
    _, contact = await seed(env)
    monitor_id = await direct_monitor(env, contact)
    turn = await ask(env, "Stop that monitor")
    fake = FakeLLM(
        tool_response(("c1", "stop_monitor", args(monitor_id=monitor_id))),
        tool_response(("c2", "stop_monitor", args(monitor_id=monitor_id))),
        tool_response(("c3", "stop_monitor", args(monitor_id=987654321))),
        text_response("Stopped."),
    )
    await runtime(env, fake, FakeChannel(), flags=FLAGS, registry=default_registry()).run(turn)
    [first] = results(fake.requests[1])
    second = results(fake.requests[2])[-1]
    unknown = results(fake.requests[3])[-1]
    assert first == {"status": "ok", "monitor_id": monitor_id, "monitor_status": "cancelled"}
    assert (second["status"], second["changed"]) == ("ok", False)
    assert unknown == {"status": "error", "reason": "not found"}
    [row] = await automations(env)
    assert (row.status, row.ended_at) == ("cancelled", env.clock.now())
    [stopped] = await events(env, "monitor_stopped")
    assert stopped.actor == "agent"


async def test_the_stop_button_works_only_for_the_owner(env: Env) -> None:
    """`mon:<token>:stop` resolves only within the pressing owner's account
    (§6 invariant 12): another owner's press is "no longer active"."""
    _, contact = await seed(env)
    await ask_create(env, contact, FakeChannel())
    confirmation_id = await confirm(env)
    await applier(env, FakeChannel(), flags=FLAGS).apply(confirmation_id)
    [row] = await automations(env)
    async with env.factory() as session:
        card = (
            await session.execute(select(PendingInput).where(PendingInput.kind == "mon"))
        ).scalar_one()
    assert card.ref_id == row.id
    data = f"mon:{card.token}:stop"

    await onboard(env, OTHER_OWNER)
    await deliver(
        env,
        callback(data, from_id=OTHER_OWNER, chat_id=OTHER_OWNER, message_id=card.prompt_message_id),
    )
    [still] = await automations(env)
    assert still.status == "active"
    assert answers(env)[-1] == "This button is no longer active."

    await deliver(env, callback(data, message_id=card.prompt_message_id))
    [stopped] = await automations(env)
    assert stopped.status == "cancelled"
    assert answers(env)[-1] == "Monitor stopped"
    assert env.sender.edits[-1]["message_id"] == card.prompt_message_id
    assert "Monitor stopped. It will not alert you again." in env.sender.edits[-1]["text"]
    [event] = await events(env, "monitor_stopped")
    assert event.actor == "owner"
    # The card was single-use: a second press changes nothing.
    await deliver(env, callback(data, message_id=card.prompt_message_id))
    assert answers(env)[-1] == "This button is no longer active."
    assert len(await events(env, "monitor_stopped")) == 1


def answers(env: Env) -> list[str | None]:
    return [call["text"] for call in env.sender.answered_callbacks]


def test_the_registry_offers_monitor_tools_only_with_their_flags() -> None:
    registry = default_registry()
    tools = {t.name: t for t in registry.all()}
    assert {t.release_flag for t in tools.values() if t.name.endswith("_monitor")} | {
        tools["list_monitors"].release_flag
    } == MONITOR_FLAGS
    assert tools["create_monitor"].always_confirm
    assert tools["list_monitors"].returns_managed
    assert not tools["stop_monitor"].returns_managed
    offered = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=READ_FLAGS,
        managed_consent=True,
    )
    assert not set(offered) & {"create_monitor", "list_monitors", "stop_monitor"}
    with_flags = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=FLAGS,
        managed_consent=False,
    )
    # Without `ai_managed`, the list (it names contacts) is not offered.
    assert {"create_monitor", "stop_monitor"} <= set(with_flags)
    assert "list_monitors" not in with_flags
    for run_type in ("triage", "evaluate_monitor", "draft_reply", "summarize"):
        assert (
            registry.for_run(
                run_type,
                tier=CapabilityTier.CONTROL_ONLY,
                enabled_flags=FLAGS,
                managed_consent=True,
            )
            == {}
        )
