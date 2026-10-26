"""S03.5 safety review item 1: a button deletion never races an owner run.

The owner taps "Delete chat data" while a run that already read the chat is
between two model calls. The deletion is queued under the owner's control
lock (`acct:{id}:control`, like owner runs and applied confirmations), so it
runs after that run: its answer, a held call and a note confirmed meanwhile
are found by their refs and deleted — no text of the chat survives in any
content column (M03 D83).

Synthetic text only; FakeLLM stands in for the provider.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.accounts.models import PendingInput
from assistant.agent.models import ToolConfirmation
from assistant.llm.port import LLMRequest
from assistant.privacy.content_columns import CONTENT_COLUMNS
from assistant.telegram.control import panels
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import MEMORY_FLAGS, READ_FLAGS, applier, args, ask, runtime
from tests.integration.business_env import (
    CONTACT,
    OWNER,
    Env,
    business_message,
    callback,
    connect,
    deliver,
    environment,
    grant_managed,
    press,
    private_message,
    run_deletion_jobs,
)
from tests.integration.test_conversation_modes import tap

pytestmark = pytest.mark.integration

FLAGS = READ_FLAGS | MEMORY_FLAGS
SAID = "CHATK Ertaga soat 10 da uchrashamiz"


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


async def hits(env: Env, needle: str) -> set[str]:
    found: set[str] = set()
    async with env.factory() as session:
        for table, column in sorted(CONTENT_COLUMNS):
            row = await session.execute(
                text(
                    f'SELECT 1 FROM app."{table}" '  # noqa: S608 - registry identifiers
                    f'WHERE "{column}"::text ILIKE :needle LIMIT 1'
                ),
                {"needle": f"%{needle}%"},
            )
            if row.first() is not None:
                found.add(f"{table}.{column}")
    return found


async def seed(env: Env) -> None:
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message(message_id=1, message_text=SAID, first_name="Kamola"))
    # The owner has the chat's panel open (`/start bizChat…`).
    await deliver(env, private_message(OWNER, f"/start bizChat{CONTACT}"))


def search() -> tuple[str, str, str]:
    return (
        "c1",
        "search_messages",
        args(
            queries=["uchrashamiz"],
            contact_id=None,
            conversation_id=None,
            since=None,
            until=None,
            limit=None,
        ),
    )


def remember_call(content: str) -> tuple[str, str, str]:
    return (
        "c2",
        "remember",
        args(
            content=content,
            kind="fact",
            contact_id=None,
            conversation_id=None,
            applies_to_replies=None,
            replaces_memory_id=None,
        ),
    )


def tapping_delete_then(env: Env, response: Any) -> Any:
    """A model call during which the owner taps Delete → Chat data."""

    async def hook(request: LLMRequest) -> Any:
        await tap(env, panels.PANEL_CARD, "delete")
        await tap(env, panels.DELETE_CARD, "chat")
        return response

    return hook


async def test_an_answer_written_after_a_tapped_deletion_does_not_keep_the_chats_text(
    env: Env,
) -> None:
    await seed(env)
    turn = await ask(env, "What did Kamola say?")
    fake = FakeLLM(
        tool_response(search()),
        tapping_delete_then(env, text_response(f"Kamola said: {SAID}")),
    )
    await runtime(env, fake, FakeChannel(), flags=FLAGS).run(turn)
    await run_deletion_jobs(env)
    assert await hits(env, "CHATK") == set()


async def test_a_call_held_after_a_tapped_deletion_loses_the_chats_text(env: Env) -> None:
    await seed(env)
    turn = await ask(env, "Remember what Kamola said")
    fake = FakeLLM(
        tool_response(search()),
        tapping_delete_then(env, tool_response(remember_call(f"Kamola: {SAID}"))),
        text_response("Please confirm."),
    )
    await runtime(env, fake, FakeChannel(), flags=FLAGS).run(turn)
    async with env.factory() as session:
        held = (await session.execute(select(ToolConfirmation))).scalar_one()
    assert held.tool_name == "remember"  # held: the run read the chat
    await run_deletion_jobs(env)
    assert await hits(env, "CHATK") == set()
    async with env.factory() as session:
        assert (await session.execute(select(ToolConfirmation.status))).scalar_one() == "expired"


async def test_a_note_confirmed_while_the_deletion_waits_is_not_saved(env: Env) -> None:
    """Confirm is tapped after Delete: the apply job queues behind the
    deletion (same lock), finds its arguments gone and saves nothing."""
    await seed(env)
    turn = await ask(env, "Remember what Kamola said")
    fake = FakeLLM(
        tool_response(search()),
        tapping_delete_then(env, tool_response(remember_call(f"Kamola: {SAID}"))),
        text_response("Please confirm."),
    )
    await runtime(env, fake, FakeChannel(), flags=FLAGS).run(turn)
    async with env.factory() as session:
        held = (await session.execute(select(ToolConfirmation))).scalar_one()
        card = (
            await session.execute(select(PendingInput).where(PendingInput.kind == "tool_confirm"))
        ).scalar_one()
    await deliver(env, callback(f"tool_confirm:{card.token}:confirm"))
    # The worker runs the lock's jobs in order: the deletion first.
    await run_deletion_jobs(env)
    outcome = await applier(env, FakeChannel(), flags=FLAGS).apply(held.id)
    assert outcome.status != "applied"
    assert await hits(env, "CHATK") == set()
    async with env.factory() as session:
        assert (
            await session.execute(text("SELECT count(*) FROM app.memory_items"))
        ).scalar_one() == 0


# ------------------------------------------------------------ fix round 2
async def test_an_answer_written_after_the_revocation_cards_delete_does_not_keep_the_chats_text(
    env: Env,
) -> None:
    """Item 5: the run's last model call is in flight when the owner revokes
    and taps the revocation card's Delete; the run then sends its answer and
    records it. The managed-data deletion is queued under the control lock
    too, so it runs after the answer is recorded and clears it (M03 D83)."""
    await seed(env)
    turn = await ask(env, "What did Kamola say?")

    async def revoke_and_delete(request: LLMRequest) -> Any:
        await deliver(env, private_message(OWNER, "/privacy revoke"))
        await press(env, "managed_delete", "delete")
        return text_response(f"Kamola said: {SAID}")

    fake = FakeLLM(tool_response(search()), revoke_and_delete)
    await runtime(env, fake, FakeChannel(), flags=FLAGS).run(turn)
    await run_deletion_jobs(env)
    assert await hits(env, "CHATK") == set()


async def test_a_call_held_by_a_run_cut_off_before_its_answer_goes_with_the_chats_deletion(
    env: Env,
) -> None:
    """Fix round 3, own inspection: a run read the chat, held `remember` with
    its text, and was cut off (a shutdown) before writing its answer — the
    turn whose refs name the chat. The held call, still confirmable, names no
    chat either; its refs are unknown, so a deletion of any chat clears it
    (fail safe) instead of leaving the text for a later Confirm."""
    await seed(env)
    turn = await ask(env, "Remember what Kamola said")

    async def cut_off(request: LLMRequest) -> Any:
        raise asyncio.CancelledError  # the worker shuts down mid-run

    fake = FakeLLM(
        tool_response(search()),
        tool_response(remember_call(f"Kamola: {SAID}")),
        cut_off,
    )
    with pytest.raises(asyncio.CancelledError):
        await runtime(env, fake, FakeChannel(), flags=FLAGS).run(turn)
    assert "tool_confirmations.args" in await hits(env, "CHATK")
    await tap(env, panels.PANEL_CARD, "delete")
    await tap(env, panels.DELETE_CARD, "chat")
    await run_deletion_jobs(env)
    assert await hits(env, "CHATK") == set()
    async with env.factory() as session:
        assert (await session.execute(select(ToolConfirmation.status))).scalar_one() == "expired"


async def test_a_question_whose_text_was_deleted_before_its_run_is_not_answered_blind(
    env: Env,
) -> None:
    """Item 2, every path: a deletion that ran before the question's run (a
    confirmed deletion tool, a sweep) left the turn without text. The run
    calls no model and asks the owner to send it again."""
    await seed(env)
    turn = await ask(env, "OWNSYN a question")
    async with env.factory() as session, session.begin():
        await session.execute(
            text("UPDATE app.control_turns SET text = NULL WHERE id = :t"), {"t": turn}
        )
    fake = FakeLLM()
    channel = FakeChannel()
    outcome = await runtime(env, fake, channel, flags=FLAGS).run(turn)
    assert fake.requests == []
    assert outcome.note == "turn_text_deleted"
    [said] = channel.sends
    assert "send it again" in said
