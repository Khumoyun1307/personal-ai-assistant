"""Taint and consent edges found in the S03.1 reviews (D, I, O). Synthetic text."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.accounts import service
from assistant.accounts.models import ControlTurn
from assistant.agent.models import AgentRun
from assistant.llm.port import LLMRequest, ToolResultItem
from assistant.privacy.deletion import delete_managed_data
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import args, ask, note_rows, one_id, request_texts, runtime
from tests.integration.business_env import (
    CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    connect,
    deliver,
    grant_managed,
)

pytestmark = pytest.mark.integration

FORWARD_FROM_CONTACT = {
    "forward_origin": {
        "type": "user",
        "date": T_BASE,
        "sender_user": {"id": CONTACT, "is_bot": False, "first_name": "Synthetic"},
    }
}
CANARY = "CANARY-SYNTH-O1"


def results(request: LLMRequest) -> list[dict[str, Any]]:
    return [json.loads(i.output) for i in request.items if isinstance(i, ToolResultItem)]


async def latest_run(env: Env) -> AgentRun:
    async with env.factory() as session:
        run = (await session.execute(select(AgentRun).order_by(AgentRun.id.desc()))).scalars()
        return run.first()  # type: ignore[return-value]


# ------------------------------------------------------------ D
async def test_a_tainted_runs_reply_keeps_later_runs_tainted(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D: the reply of a tainted run carries the taint, so a later run is
    tainted even when the forwarded turn has left the history window."""
    await connect(env)
    first = await ask(
        env,
        "Synthetic forwarded instruction",
        forward_origin={"type": "hidden_user", "date": T_BASE, "sender_user_name": "Nobody"},
    )
    await runtime(env, FakeLLM(text_response("I read it.")), FakeChannel()).run(first)
    async with env.factory() as session:
        reply = (
            (
                await session.execute(
                    select(ControlTurn)
                    .where(ControlTurn.role == "assistant")
                    .order_by(ControlTurn.id)
                )
            )
            .scalars()
            .first()
        )
    assert reply is not None
    assert reply.third_party is True

    # Only the reply is still in the window; the forwarded turn is not.
    monkeypatch.setattr("assistant.agent.runtime.OWNER_HISTORY_TURNS", 1)
    second = await ask(env, "Now note it")
    fake = FakeLLM(tool_response(("c1", "test_set_note", args(note="pwned"))), text_response("ok"))
    await runtime(env, fake, FakeChannel()).run(second)
    assert (await latest_run(env)).tainted is True
    assert results(fake.requests[1])[0]["status"] == "awaiting_confirmation"
    assert await note_rows(env) == 0


# ------------------------------------------------------------ I
async def test_managed_tools_need_a_consent_naming_the_routes_processor(env: Env) -> None:
    """I: an `ai_managed` consent for another processor offers no managed tool
    and includes no managed history (defence in depth behind the wrapper)."""
    await connect(env)
    async with env.factory() as session, session.begin():
        account = await service.get_or_create_account(
            session, telegram_user_id=OWNER, clock=env.clock
        )
        await service.grant_consent(
            session,
            account[0],
            kind="ai_managed",
            policy_version="v3",
            processors=["SomeOtherProvider"],
            clock=env.clock,
        )
    turn = await ask(env, "What did they say?")
    fake = FakeLLM(text_response("I cannot read your chats."))
    await runtime(env, fake, FakeChannel()).run(turn)
    offered = {tool.name for tool in fake.requests[0].tools}
    assert offered.isdisjoint(
        {"search_messages", "resolve_contact", "get_conversation", "list_conversations"}
    )


# ------------------------------------------------------------ O
async def test_a_forward_from_a_managed_contact_is_managed_content(env: Env) -> None:
    """O: forwarded text from a managed chat's contact is stored as managed
    content and withheld from the model without `ai_managed`."""
    await connect(env)
    await deliver(env, business_message(message_text="Synthetic chat line"))
    turn_id = await ask(env, f"{CANARY} forwarded line", **FORWARD_FROM_CONTACT)
    async with env.factory() as session:
        turn = await session.get(ControlTurn, turn_id)
    assert turn is not None
    assert (turn.third_party, turn.contains_managed) == (True, True)

    fake = FakeLLM(text_response("I cannot read that message."))
    await runtime(env, fake, FakeChannel()).run(turn_id)
    [request] = fake.requests
    assert CANARY not in request_texts(request)
    assert "withheld" in request_texts(request)
    assert not request.contains_managed()


async def test_with_managed_consent_the_forward_is_shown_wrapped(env: Env) -> None:
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message(message_text="Synthetic chat line"))
    turn_id = await ask(env, f"{CANARY} forwarded line", **FORWARD_FROM_CONTACT)
    fake = FakeLLM(text_response("ok"))
    await runtime(env, fake, FakeChannel()).run(turn_id)
    [request] = fake.requests
    assert f'<untrusted source="forwarded" trust="untrusted">{CANARY}' in request_texts(request)
    assert request.contains_managed()


async def test_a_quote_from_a_managed_business_chat_is_managed(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_text="Synthetic chat line"))
    turn_id = await ask(
        env,
        "What did they mean?",
        quote={"text": f"{CANARY} quoted", "position": 0},
        external_reply={
            "origin": {"type": "hidden_user", "date": T_BASE, "sender_user_name": "x"},
            "chat": {"id": CONTACT, "type": "private"},
        },
    )
    async with env.factory() as session:
        turn = await session.get(ControlTurn, turn_id)
    assert turn is not None
    assert turn.contains_managed is True
    fake = FakeLLM(text_response("ok"))
    await runtime(env, fake, FakeChannel()).run(turn_id)
    shown = request_texts(fake.requests[0])
    assert "What did they mean?" in shown
    assert CANARY not in shown


async def test_a_forward_from_an_unknown_sender_is_not_managed(env: Env) -> None:
    await connect(env)
    turn_id = await ask(
        env,
        "Synthetic",
        forward_origin={
            "type": "user",
            "date": T_BASE,
            "sender_user": {"id": 1234, "is_bot": False},
        },
    )
    async with env.factory() as session:
        turn = await session.get(ControlTurn, turn_id)
    assert turn is not None
    assert (turn.third_party, turn.contains_managed) == (True, False)


async def test_managed_data_deletion_clears_managed_forwards(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_text="Synthetic chat line"))
    turn_id = await ask(env, f"{CANARY} forwarded line", **FORWARD_FROM_CONTACT)
    account_id = await one_id(
        env, "SELECT id FROM app.accounts WHERE telegram_user_id = :o", o=OWNER
    )
    async with env.factory() as session, session.begin():
        await delete_managed_data(session, account_id=account_id, clock=env.clock)
    async with env.factory() as session:
        value = (
            await session.execute(
                text("SELECT text FROM app.control_turns WHERE id = :i"), {"i": turn_id}
            )
        ).scalar_one()
    assert value is None
