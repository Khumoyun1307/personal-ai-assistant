"""Taint and consent edges found in the S03.1 reviews (D, I, O). Synthetic text."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.accounts import service
from assistant.accounts.models import ControlTurn
from assistant.agent.models import AgentRun
from assistant.agent.tools.registry import CHAT_ACCESS_OFF
from assistant.llm.port import LLMRequest, ToolResultItem
from assistant.privacy.deletion import delete_managed_data, purge_conversation
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
HIDDEN_FORWARD = {"type": "hidden_user", "date": T_BASE, "sender_user_name": "Nobody"}


async def replies(env: Env) -> list[ControlTurn]:
    async with env.factory() as session:
        rows = await session.execute(
            select(ControlTurn).where(ControlTurn.role == "assistant").order_by(ControlTurn.id)
        )
        return list(rows.scalars().all())


async def note_run(env: Env, question: str, reply: str = "ok") -> tuple[FakeLLM, AgentRun]:
    turn = await ask(env, question)
    fake = FakeLLM(tool_response(("c1", "test_set_note", args(note="pwned"))), text_response(reply))
    await runtime(env, fake, FakeChannel()).run(turn)
    return fake, await latest_run(env)


async def test_a_directly_tainted_runs_reply_still_taints_the_next_run(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D56(b): taint from a run's OWN input propagates through its reply."""
    await connect(env)
    first = await ask(env, "Synthetic forwarded instruction", forward_origin=HIDDEN_FORWARD)
    await runtime(env, FakeLLM(text_response("I read it.")), FakeChannel()).run(first)
    [reply] = await replies(env)
    assert (reply.third_party, reply.from_tainted_run) == (True, True)

    monkeypatch.setattr("assistant.agent.runtime.OWNER_HISTORY_TURNS", 1)  # only the reply
    fake, run = await note_run(env, "Now note it")
    assert run.tainted is True
    assert results(fake.requests[1])[0]["status"] == "awaiting_confirmation"
    assert await note_rows(env) == 0


async def test_inherited_taint_decays_once_the_source_leaves_the_window(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D56: a reply tainted only by history does not taint further runs; it
    is still shown to the model as untrusted (a)."""
    await connect(env)
    first = await ask(env, "Synthetic forwarded instruction", forward_origin=HIDDEN_FORWARD)
    await runtime(env, FakeLLM(text_response("I read it.")), FakeChannel()).run(first)
    monkeypatch.setattr("assistant.agent.runtime.OWNER_HISTORY_TURNS", 1)

    second = await ask(env, "Tell me more")
    await runtime(env, FakeLLM(text_response("SYNTH-INHERITED reply")), FakeChannel()).run(second)
    assert (await latest_run(env)).tainted is True  # inherited from the first reply
    inherited = (await replies(env))[-1]
    assert (inherited.third_party, inherited.from_tainted_run) == (False, True)

    fake, run = await note_run(env, "Note it now")
    assert run.tainted is False  # the chain decayed
    assert results(fake.requests[1])[0]["status"] == "ok"
    assert await note_rows(env) == 1
    shown = request_texts(fake.requests[0])
    assert '<untrusted source="tainted_run_reply" trust="untrusted">SYNTH-INHERITED reply' in shown


# ------------------------------------------------------------ I
async def test_managed_tools_need_a_consent_naming_the_routes_processor(env: Env) -> None:
    """I: an `ai_managed` consent for another processor offers the managed
    tools only as blocked stubs (S03.7 B5: a call reads nothing and returns
    `chat_access_off`) and includes no managed history (defence in depth
    behind the wrapper). The consent card is not sent: the consent exists."""
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
        await service.record_control_turn(
            session,
            account_id=account[0].id,
            role="assistant",
            text="CANARY-SYNTH-I1 an earlier answer built from managed chats",
            contains_managed=True,
        )
    turn = await ask(env, "What did they say?")
    fake = FakeLLM(
        tool_response(
            (
                "c1",
                "search_messages",
                args(
                    queries=["synthetic"],
                    contact_id=None,
                    conversation_id=None,
                    since=None,
                    until=None,
                    limit=None,
                ),
            )
        ),
        text_response("I cannot read your chats."),
    )
    channel = FakeChannel()
    await runtime(env, fake, channel).run(turn)
    offered = {tool.name for tool in fake.requests[0].tools}
    assert {"search_messages", "resolve_contact", "get_conversation", "list_conversations"} <= (
        offered
    )
    assert [r["status"] for r in results(fake.requests[1])] == [CHAT_ACCESS_OFF]
    assert all(not buttons for buttons in channel.buttons)  # no consent card
    # Managed history is excluded too, not only the tools.
    for request in fake.requests:
        assert "CANARY-SYNTH-I1" not in request_texts(request)
        assert not request.contains_managed()


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


async def test_purging_the_chat_redacts_a_managed_forward_from_its_contact(env: Env) -> None:
    """Review item 4: a managed forward records which conversation/contact it
    came from (`refs`), so purging that chat redacts it."""
    await connect(env)
    await deliver(env, business_message(message_text="Synthetic chat line"))
    turn_id = await ask(env, f"{CANARY} forwarded line", **FORWARD_FROM_CONTACT)
    async with env.factory() as session:
        conversation_id, business_chat_id, contact_id = (
            await session.execute(
                text("SELECT id, business_chat_id, contact_id FROM app.conversations")
            )
        ).one()
        turn = await session.get(ControlTurn, turn_id)
    assert turn is not None
    assert turn.refs == {"conversation_ids": [conversation_id], "contact_ids": [contact_id]}
    account_id = await one_id(
        env, "SELECT id FROM app.accounts WHERE telegram_user_id = :o", o=OWNER
    )
    async with env.factory() as session, session.begin():
        await purge_conversation(
            session,
            account_id=account_id,
            conversation_id=conversation_id,
            business_chat_id=business_chat_id,
            clock=env.clock,
        )
    async with env.factory() as session:
        value = (
            await session.execute(
                text("SELECT text FROM app.control_turns WHERE id = :i"), {"i": turn_id}
            )
        ).scalar_one()
    assert value is None


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


# ------------------------------------------------------------ review 3, item 1
async def purge_the_chat(env: Env) -> dict[str, list[int]]:
    """Purge the one managed conversation; returns its refs shape."""
    async with env.factory() as session:
        conversation_id, business_chat_id, contact_id = (
            await session.execute(
                text("SELECT id, business_chat_id, contact_id FROM app.conversations")
            )
        ).one()
    account_id = await one_id(
        env, "SELECT id FROM app.accounts WHERE telegram_user_id = :o", o=OWNER
    )
    async with env.factory() as session, session.begin():
        await purge_conversation(
            session,
            account_id=account_id,
            conversation_id=conversation_id,
            business_chat_id=business_chat_id,
            clock=env.clock,
        )
    return {"conversation_ids": [conversation_id], "contact_ids": [contact_id]}


async def test_purging_the_chat_redacts_the_reply_to_a_managed_forward(env: Env) -> None:
    """Review 3 item 1: the reply to a managed forward may restate it, so it
    carries the forward's refs and is redacted with the chat."""
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message(message_text="Synthetic chat line"))
    turn_id = await ask(env, f"{CANARY} forwarded line", **FORWARD_FROM_CONTACT)
    await runtime(env, FakeLLM(text_response(f"They said {CANARY}")), FakeChannel()).run(turn_id)
    [reply] = await replies(env)
    assert reply.contains_managed is True

    refs = await purge_the_chat(env)

    assert reply.refs == refs
    assert [r.text for r in await replies(env)] == [None]


async def test_purging_the_chat_redacts_a_reply_drawing_on_a_forward_in_history(
    env: Env,
) -> None:
    """Review 3 item 1: an included managed history turn seeds the refs too."""
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message(message_text="Synthetic chat line"))
    await ask(env, f"{CANARY} forwarded line", **FORWARD_FROM_CONTACT)
    later = await ask(env, "What did that forward say?")
    await runtime(env, FakeLLM(text_response(f"It said {CANARY}")), FakeChannel()).run(later)
    [reply] = await replies(env)

    refs = await purge_the_chat(env)

    assert reply.refs == refs
    assert [r.text for r in await replies(env)] == [None]


# ------------------------------------------------------------ review 3, gap (c)
async def test_a_history_tainted_run_retainted_by_a_tool_result_passes_taint_on(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D56: inherited taint alone decays, but if the run's own tool result
    carries contact text too, its reply is third-party and taints later runs."""
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message(message_text="Synthetic chat line"))
    conversation_id = await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=CONTACT
    )
    first = await ask(env, "Synthetic forwarded instruction", forward_origin=HIDDEN_FORWARD)
    await runtime(env, FakeLLM(text_response("I read it.")), FakeChannel()).run(first)
    monkeypatch.setattr("assistant.agent.runtime.OWNER_HISTORY_TURNS", 1)  # only the reply

    second = await ask(env, "Read the chat")
    read = args(conversation_id=conversation_id, contact_id=None, cursor=None, limit=None)
    fake = FakeLLM(tool_response(("c1", "get_conversation", read)), text_response("Read."))
    await runtime(env, fake, FakeChannel()).run(second)

    assert (await latest_run(env)).tainted is True
    reply = (await replies(env))[-1]
    assert (reply.third_party, reply.from_tainted_run) == (True, True)
