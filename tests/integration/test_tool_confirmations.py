"""Held tool calls: the edges of hold → tap → apply (S03.1 review fixes).

Every failure path must end in a state the owner is told about and that a
later tap cannot turn into a change (§6 invariants 3b, 11). Synthetic text.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update

from assistant.accounts.models import PendingInput
from assistant.agent import confirmations
from assistant.agent.confirmations import CARD_ARGS_LIMIT, card_text, render_args
from assistant.agent.models import ToolConfirmation
from assistant.agent.tools.registry import ToolContext, ToolServices
from assistant.core.channel import Delivery
from assistant.jobs.reconcile import unapplied_confirmations
from assistant.llm.port import LLMRequest, ToolResultItem
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import (
    NOTE_EXPLODE,
    NOTE_ODD_STATUS,
    SET_NOTE,
    applier,
    args,
    ask,
    one_id,
    runtime,
)
from tests.integration.business_env import (
    CONTACT,
    T_BASE,
    Env,
    business_message,
    callback,
    connect,
    deliver,
    grant_managed,
    job_names,
)

pytestmark = pytest.mark.integration


def results(request: LLMRequest) -> list[dict[str, Any]]:
    return [json.loads(i.output) for i in request.items if isinstance(i, ToolResultItem)]


async def held_call(
    env: Env, note: str, *, channel: FakeChannel | None = None, call_id: str = "c9"
) -> tuple[FakeLLM, FakeChannel]:
    """A tainted run (it read the contact's chat) that calls the configure tool."""
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message(message_id=11, message_text="Synthetic", date=T_BASE))
    conversation_id = await one_id(
        env, "SELECT id FROM app.conversations WHERE business_chat_id = :c", c=CONTACT
    )
    turn = await ask(env, "Read it and note it")
    read = args(conversation_id=conversation_id, contact_id=None, cursor=None, limit=None)
    fake = FakeLLM(
        tool_response(("c1", "get_conversation", read)),
        tool_response((call_id, "test_set_note", args(note=note))),
        text_response("ok"),
    )
    channel = channel or FakeChannel()
    await runtime(env, fake, channel).run(turn)
    return fake, channel


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


async def status_of(env: Env) -> tuple[str, Any]:
    async with env.factory() as session:
        row = (await session.execute(select(ToolConfirmation))).scalar_one()
    return row.status, row.args


# ------------------------------------------------------------ A, F: applying
async def test_a_handler_exception_ends_failed_and_the_owner_is_told(env: Env) -> None:
    _, channel = await held_call(env, NOTE_EXPLODE)
    confirmation_id = await confirm(env)

    outcome = await applier(env, channel).apply(confirmation_id)

    assert outcome.status == "failed"
    assert await status_of(env) == ("failed", None)
    assert "may not have been applied" in channel.sends[-1]
    assert "Done" not in channel.sends[-1]
    assert (await applier(env, channel).apply(confirmation_id)).status == "skipped"


async def test_only_an_explicit_success_status_is_reported_done(env: Env) -> None:
    _, channel = await held_call(env, NOTE_ODD_STATUS)
    confirmation_id = await confirm(env)

    outcome = await applier(env, channel).apply(confirmation_id)

    assert outcome.status == "failed"
    assert "not counting it as done" in channel.sends[-1]
    assert "Done" not in channel.sends[-1]
    assert (await status_of(env))[0] == "failed"


# ------------------------------------------------------------ B: the card
async def test_arguments_too_large_for_the_card_are_never_held(env: Env) -> None:
    fake, channel = await held_call(env, "x" * (CARD_ARGS_LIMIT + 1))
    [_, refused] = results(fake.requests[2])
    assert refused["status"] == "not_executed"
    assert "too large" in refused["reason"]
    async with env.factory() as session:
        assert (await session.execute(select(ToolConfirmation))).scalars().all() == []
    assert not any("Confirm a change?" in text for text in channel.sends)


def test_the_card_shows_every_field_with_invisible_characters_made_visible() -> None:
    hidden = "pay‮evil​⁦x﻿"
    shown = card_text(SET_NOTE, {"note": hidden}, tainted=True)
    for char in ("‮", "​", "⁦", "﻿"):
        assert char not in shown
    assert "\\u202e" in shown
    assert "pay" in shown
    assert "evil" in shown
    assert render_args({"note": "a" * 50}).count("a") == 50


def test_an_untainted_card_does_not_claim_other_peoples_text() -> None:
    assert "other people" not in card_text(SET_NOTE, {"note": "x"}, tainted=False)
    assert "other people" in card_text(SET_NOTE, {"note": "x"}, tainted=True)


# ------------------------------------------------------------ H: hold edges
async def test_a_repeated_call_reports_the_rows_real_state(env: Env) -> None:
    await held_call(env, "synthetic")
    async with env.factory() as session, session.begin():
        await session.execute(update(ToolConfirmation).values(status="rejected", args=None))
    # The same (run, call) id again, as a re-delivered model turn would send.
    run_id = await one_id(env, "SELECT run_id FROM app.tool_confirmations")
    account_id = await one_id(env, "SELECT account_id FROM app.tool_confirmations")
    ctx = ToolContext(
        account_id=account_id,
        run_id=run_id,
        run_type="owner_request",
        tainted=True,
        services=ToolServices(
            session_factory=env.factory,
            clock=env.clock,
            monthly_budget_usd=None,
            channel=FakeChannel(),
        ),
    )
    again = await confirmations.hold(ctx, SET_NOTE, "c9", {"note": "synthetic"})
    assert again.status == "rejected"


class RaisingChannel(FakeChannel):
    """A send whose outcome is unknown (network error after the request)."""

    async def send(
        self, *, account_id: int, chat_id: int, text: str, buttons: Any = ()
    ) -> Delivery:
        if buttons:
            raise TimeoutError("synthetic ambiguous send")
        return await super().send(account_id=account_id, chat_id=chat_id, text=text)


async def test_an_ambiguous_card_send_expires_the_card_and_tells_the_model(env: Env) -> None:
    fake, _ = await held_call(env, "synthetic", channel=RaisingChannel())
    [_, held] = results(fake.requests[2])
    assert held["status"] == "not_executed"
    assert await status_of(env) == ("expired", None)
    async with env.factory() as session:
        cards = (
            (await session.execute(select(PendingInput).where(PendingInput.kind == "tool_confirm")))
            .scalars()
            .all()
        )
    assert cards == []


async def test_a_retried_owner_turn_does_not_send_a_second_card_for_the_same_change(
    env: Env,
) -> None:
    _, channel = await held_call(env, "synthetic")
    cards_before = sum(1 for b in channel.buttons if b)
    # A new run (e.g. the owner asks again) proposing the identical change.
    turn = await ask(env, "Note it again")
    fake2 = FakeLLM(
        tool_response(("d1", "test_set_note", args(note="synthetic"))), text_response("ok")
    )
    await runtime(env, fake2, channel).run(turn)
    [again] = results(fake2.requests[1])
    assert again["status"] == "awaiting_confirmation"
    assert sum(1 for b in channel.buttons if b) == cards_before
    async with env.factory() as session:
        assert len((await session.execute(select(ToolConfirmation))).scalars().all()) == 1


# ------------------------------------------------------------ C: lost apply jobs
async def test_a_confirmed_call_whose_job_was_lost_is_re_deferred(env: Env) -> None:
    await held_call(env, "synthetic")
    confirmation_id = await confirm(env)
    async with env.factory() as session, session.begin():
        await session.execute(
            # The job vanished (e.g. a dump restored without it).
            text(
                "DELETE FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = 'agent.apply_confirmation'"
            )
        )
    async with env.factory() as session:
        assert await unapplied_confirmations(session, env.clock.now()) == []
        later = env.clock.now() + timedelta(minutes=5)
        found = await unapplied_confirmations(session, later)
    assert [row[0] for row in found] == [confirmation_id]
    assert "agent.apply_confirmation" not in await job_names(env)
