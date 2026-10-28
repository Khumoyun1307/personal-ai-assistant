"""S05.2 §3.6: contact priority (FR-TRI-04) — the domain setter and the
`set_contact_priority` tool, echoed with a one-tap Undo (S03.3 pattern).

Required tests, findable by name: `test_set_contact_priority_echoes_with_undo`,
`test_undo_restores_only_while_unchanged`, `test_a_foreign_contact_id_is_not_found`,
`test_a_tainted_run_gets_a_confirmation_card`,
`test_a_retried_call_changes_nothing_twice`.

Synthetic text only; FakeLLM stands in for the provider (AGENTS §11).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from assistant.accounts.models import AuditLogEntry
from assistant.agent.tools.priority import (
    PRIORITY_FLAG,
    SetContactPriorityArgs,
    set_contact_priority_tool,
)
from assistant.agent.tools.registry import ToolContext, ToolServices
from assistant.conversations import priority
from assistant.conversations.models import Contact
from assistant.core.errors import NotInAccountError
from assistant.db.uow import transaction
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import ask, one_id, runtime
from tests.integration.business_env import (
    CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    callback,
    connect,
    deliver,
    environment,
    grant_managed,
)
from tests.integration.p2_factories import seed_account, seed_conversation

pytestmark = pytest.mark.integration

FLAGS = frozenset({PRIORITY_FLAG})


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


async def kamola(env: Env) -> int:
    """The owner's chat with Kamola; her contact id."""
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message(message_id=11, message_text="SYNTH", first_name="Kamola"))
    return await one_id(env, "SELECT id FROM app.contacts WHERE telegram_user_id = :u", u=CONTACT)


async def account_id(env: Env) -> int:
    return await one_id(env, "SELECT id FROM app.accounts WHERE telegram_user_id = :u", u=OWNER)


async def ctx(
    env: Env, channel: FakeChannel | None, *, run_id: int = 0, call_id: str = ""
) -> ToolContext:
    return ToolContext(
        account_id=await account_id(env),
        run_id=run_id,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(
            session_factory=env.factory, clock=env.clock, monthly_budget_usd=None, channel=channel
        ),
        call_id=call_id,
    )


async def current(env: Env, contact_id: int) -> str:
    async with env.factory() as session:
        value = await session.scalar(select(Contact.priority).where(Contact.id == contact_id))
    assert value is not None
    return value


async def changes(env: Env) -> list[dict[str, Any]]:
    async with env.factory() as session:
        rows = await session.execute(
            select(AuditLogEntry.details)
            .where(AuditLogEntry.event == priority.EVENT_PRIORITY_CHANGED)
            .order_by(AuditLogEntry.id)
        )
        return [dict(d) for d in rows.scalars()]


async def press_undo(env: Env, channel: FakeChannel, index: int) -> None:
    [button] = channel.buttons[index]
    assert button.label == "Undo"
    await deliver(env, callback(button.callback_data))


async def test_set_contact_priority_echoes_with_undo(env: Env) -> None:
    contact = await kamola(env)
    channel = FakeChannel()
    outcome = await set_contact_priority_tool(
        await ctx(env, channel), SetContactPriorityArgs(contact_id=contact, priority="vip")
    )
    assert outcome.status == "ok"
    assert outcome.data["changed"] is True
    assert outcome.data["contact_id"] == contact
    assert "Kamola" not in json.dumps(dict(outcome.data))  # ids only to the model
    assert await current(env, contact) == "vip"
    assert "Kamola" in channel.sends[0]
    assert (await changes(env))[0] == {
        "contact_id": contact,
        "old": "normal",
        "new": "vip",
        "run_id": 0,
        "call_id": "",
    }
    await press_undo(env, channel, 0)
    assert await current(env, contact) == "normal"


async def test_undo_restores_only_while_unchanged(env: Env) -> None:
    """S03.3 rule: Undo reverses its change only while the value is still
    the one that change set; a later change is never overwritten."""
    contact = await kamola(env)
    channel = FakeChannel()
    await set_contact_priority_tool(
        await ctx(env, channel, call_id="c1"),
        SetContactPriorityArgs(contact_id=contact, priority="vip"),
    )
    await set_contact_priority_tool(
        await ctx(env, channel, call_id="c2"),
        SetContactPriorityArgs(contact_id=contact, priority="low"),
    )
    await press_undo(env, channel, 0)  # the vip change: superseded by low
    assert await current(env, contact) == "low"
    await press_undo(env, channel, 1)  # the low change: still in place
    assert await current(env, contact) == "vip"


async def test_a_foreign_contact_id_is_not_found(env: Env) -> None:
    await kamola(env)
    async with env.factory() as session, session.begin():
        other = await seed_account(session)
        chat = await seed_conversation(session, account_id=other.id)
        foreign = chat.contact_id
    with pytest.raises(NotInAccountError):
        await set_contact_priority_tool(
            await ctx(env, FakeChannel()),
            SetContactPriorityArgs(contact_id=foreign, priority="vip"),
        )
    async with transaction(env.factory) as uow:
        with pytest.raises(NotInAccountError):
            await priority.set_priority(
                uow.session,
                account_id=await account_id(env),
                contact_id=foreign,
                priority="low",
                actor="owner",
                clock=env.clock,
            )
    assert await current(env, foreign) == "normal"
    assert await changes(env) == []


async def test_a_tainted_run_gets_a_confirmation_card(env: Env) -> None:
    """A forwarded message taints the run: the change is held for the
    owner's tap, and the card names whose priority it is."""
    contact = await kamola(env)
    turn = await ask(
        env,
        "Make her VIP",
        forward_origin={"type": "hidden_user", "sender_user_name": "X", "date": T_BASE},
    )
    fake = FakeLLM(
        tool_response(
            (
                "c1",
                "set_contact_priority",
                json.dumps({"contact_id": contact, "priority": "vip"}),
            )
        ),
        text_response("Please confirm."),
    )
    channel = FakeChannel()
    await runtime(env, fake, channel, flags=FLAGS).run(turn)
    assert await current(env, contact) == "normal"
    assert "Confirm a change?" in channel.sends[0]
    assert "Kamola" in channel.sends[0]


async def test_a_retried_call_changes_nothing_twice(env: Env) -> None:
    """Idempotent on `(run_id, tool_call_id)` through the audit row: a
    retried call after the owner changed it back does not change it again."""
    contact = await kamola(env)
    first = await set_contact_priority_tool(
        await ctx(env, FakeChannel(), run_id=7, call_id="c1"),
        SetContactPriorityArgs(contact_id=contact, priority="vip"),
    )
    assert first.data["changed"] is True
    async with transaction(env.factory) as uow:  # the owner sets it back meanwhile
        await priority.set_priority(
            uow.session,
            account_id=await account_id(env),
            contact_id=contact,
            priority="normal",
            actor="owner",
            clock=env.clock,
        )
    again = await set_contact_priority_tool(
        await ctx(env, FakeChannel(), run_id=7, call_id="c1"),
        SetContactPriorityArgs(contact_id=contact, priority="vip"),
    )
    assert again.status == "ok"
    assert again.data["changed"] is False
    assert await current(env, contact) == "normal"
    assert len(await changes(env)) == 2  # the tool's once, the owner's once
