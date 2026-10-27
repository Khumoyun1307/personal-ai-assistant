"""Held tool calls: the edges of hold → tap → apply (S03.1 review fixes).

Every failure path must end in a state the owner is told about and that a
later tap cannot turn into a change (§6 invariants 3b, 11). Synthetic text.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import pytest
from sqlalchemy import select, text, update
from structlog.testing import capture_logs

from assistant.accounts.models import PendingInput
from assistant.agent import confirmations
from assistant.agent.confirmations import (
    CARD_ARGS_LIMIT,
    ConfirmationApplier,
    card_text,
    render_args,
)
from assistant.agent.models import ToolConfirmation
from assistant.agent.tools.registry import ToolContext, ToolOutcome, ToolServices, default_registry
from assistant.core.channel import Delivery
from assistant.inbox.notify import OwnerAlerts
from assistant.jobs.tasks import JobHandlers
from assistant.llm.port import LLMRequest, ToolResultItem
from tests.fakes.channel import FakeChannel
from tests.fakes.deletions import RecordingDeleter
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.fakes.summaries import RecordingRefresher
from tests.integration.agent_env import (
    ALL_FLAGS,
    NOTE_CANCEL,
    NOTE_EXPLODE,
    NOTE_KEY_ERROR,
    NOTE_NOT_IN_ACCOUNT,
    NOTE_ODD_STATUS,
    SET_NOTE,
    applier,
    args,
    ask,
    note_rows,
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


async def test_an_id_not_in_the_account_after_confirm_is_reported_not_found(env: Env) -> None:
    """The one exception that means "not found" (§8.3, M03 fix round C2)."""
    _, channel = await held_call(env, NOTE_NOT_IN_ACCOUNT)
    confirmation_id = await confirm(env)

    with capture_logs() as logs:
        outcome = await applier(env, channel).apply(confirmation_id)

    assert outcome.status == "failed"
    assert "not found" in channel.sends[-1]
    assert not [e for e in logs if e["event"] == "tool_confirmation_handler_failed"]


async def test_a_lookup_bug_after_confirm_is_a_failure_not_not_found(env: Env) -> None:
    """A `KeyError` in a confirmed handler is a bug, which may have stopped
    half-way: the owner hears it may not have been applied — not "not found.
    Nothing was changed" — and the log names its type (§8.3, invariant 11)."""
    _, channel = await held_call(env, NOTE_KEY_ERROR)
    confirmation_id = await confirm(env)

    with capture_logs() as logs:
        outcome = await applier(env, channel).apply(confirmation_id)

    assert outcome.status == "failed"
    assert "may or may not have been applied" in channel.sends[-1]
    assert "not found" not in channel.sends[-1]
    assert await status_of(env) == ("failed", None)
    failures = [e for e in logs if e["event"] == "tool_confirmation_handler_failed"]
    assert [e["exc_type"] for e in failures] == ["KeyError"]


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


#: Review item 2: every invisible/format/control/private-use/unassigned
#: character, not a deny-list of a few.
HIDDEN = [
    "\U000e0041",  # tag LATIN CAPITAL A (Cf)
    "\U000e007f",  # cancel tag
    "⁠",  # word joiner
    "⁤",  # invisible plus
    "؜",  # arabic letter mark
    "­",  # soft hyphen
    "️",  # variation selector 16
    "\U000e0100",  # variation selector 17
    "ㅤ",  # hangul filler
    "ᅟ",
    "ᅠ",
    "ﾠ",
    "\u0085",  # C1 control (NEL)
    "\u009b",  # C1 control (CSI)
    "\x7f",  # DEL (C0 range end; json.dumps leaves it unescaped)
    "\u034f",  # combining grapheme joiner (Mn)
    "\u180b",  # Mongolian free variation selectors (Mn)
    "\u180c",
    "\u180d",
    "\u180f",
    "\u17b4",  # Khmer inherent vowels (Mn, render as nothing)
    "\u17b5",
    "\u2800",  # braille blank (So)
    "\u2028",  # line separator (Zl)
    "\u2029",  # paragraph separator (Zp)
    "",  # private use
    "\U000f0000",  # supplementary private use
    "͸",  # unassigned
    "‮",
    "​",
    "﻿",
]


@pytest.mark.parametrize("char", HIDDEN)
def test_no_invisible_or_control_character_reaches_the_card(char: str) -> None:
    shown = card_text(SET_NOTE, {"note": f"pay{char}evil"}, tainted=True)
    assert char not in shown
    assert f"\\u{ord(char):04x}" in shown or f"\\U{ord(char):08x}" in shown
    assert "pay" in shown
    assert "evil" in shown


def test_ordinary_text_in_any_script_is_shown_as_is() -> None:
    shown = render_args({"note": "Salom, Камола! oʻzbek ✓ 漢字"})
    assert shown == json.dumps({"note": "Salom, Камола! oʻzbek ✓ 漢字"}, ensure_ascii=False)


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


FORWARD = {"forward_origin": {"type": "hidden_user", "date": T_BASE, "sender_user_name": "N"}}


async def forwarded_note_run(env: Env, channel: FakeChannel, call_id: str) -> dict[str, Any]:
    """A run tainted by its OWN forwarded turn (independent of reply taint)."""
    turn = await ask(env, "Synthetic forwarded request", **FORWARD)
    fake = FakeLLM(
        tool_response((call_id, "test_set_note", args(note="synthetic"))), text_response("ok")
    )
    await runtime(env, fake, channel).run(turn)
    [held] = results(fake.requests[1])
    return held


async def cards(env: Env) -> list[PendingInput]:
    async with env.factory() as session:
        return list(
            (await session.execute(select(PendingInput).where(PendingInput.kind == "tool_confirm")))
            .scalars()
            .all()
        )


async def test_the_same_pending_change_from_another_run_reuses_its_live_card(env: Env) -> None:
    await connect(env)
    channel = FakeChannel()
    first = await forwarded_note_run(env, channel, "a1")
    second = await forwarded_note_run(env, channel, "b1")
    assert (first["status"], second["status"]) == ("awaiting_confirmation",) * 2
    assert len(await cards(env)) == 1
    async with env.factory() as session:
        assert len((await session.execute(select(ToolConfirmation))).scalars().all()) == 1


async def test_a_pending_change_whose_card_is_gone_gets_a_new_card(env: Env) -> None:
    """Review item 5: never 'awaiting a tap' with nothing to tap."""
    await connect(env)
    channel = FakeChannel()
    await forwarded_note_run(env, channel, "a1")
    async with env.factory() as session, session.begin():
        await session.execute(text("DELETE FROM app.pending_inputs WHERE kind = 'tool_confirm'"))
    again = await forwarded_note_run(env, channel, "b1")
    assert again["status"] == "awaiting_confirmation"
    assert len(await cards(env)) == 1
    async with env.factory() as session:
        statuses = sorted((await session.execute(select(ToolConfirmation.status))).scalars().all())
    assert statuses == ["expired", "pending"]


# ------------------------------------------------------------ C, 7: lost and interrupted applies
def handlers(env: Env, channel: FakeChannel) -> JobHandlers:
    assert env.scheduler is not None

    class NoRuntime:
        async def run(self, turn_id: int) -> Any:
            raise AssertionError("not used")

    return JobHandlers(
        session_factory=env.factory,
        clock=env.clock,
        scheduler=env.scheduler,
        owner_runtime=NoRuntime(),
        alerts=OwnerAlerts(env.factory, channel, env.clock),
        monthly_budget_usd=None,
        heartbeat_url=None,
        confirmation_applier=applier(env, channel),
        summary_refresher=RecordingRefresher(),
        data_deleter=RecordingDeleter(),
    )


async def test_a_confirmed_call_whose_job_was_lost_is_re_deferred_and_applied(env: Env) -> None:
    _, channel = await held_call(env, "synthetic")
    confirmation_id = await confirm(env)
    async with env.factory() as session, session.begin():
        # The job vanished (e.g. a dump restored without it).
        await session.execute(
            text(
                "DELETE FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = 'agent.apply_confirmation'"
            )
        )
    jobs = handlers(env, channel)
    await jobs.reconcile_pending()
    assert "agent.apply_confirmation" not in await job_names(env)  # inside the grace period

    env.clock.advance(minutes=5)
    await jobs.reconcile_pending()
    assert (await job_names(env)).count("agent.apply_confirmation") == 1
    assert (await jobs.apply_confirmation(confirmation_id)).status == "applied"
    assert await note_rows(env) == 1
    await jobs.reconcile_pending()  # nothing left to re-defer
    assert (await job_names(env)).count("agent.apply_confirmation") == 1


async def test_an_apply_cancelled_mid_way_is_failed_and_the_owner_told(env: Env) -> None:
    """Review item 7: a CancelledError after the claim leaves `applying`, and
    the reconciler fails it and tells the owner; it is never 'applied'."""
    _, channel = await held_call(env, NOTE_CANCEL)
    confirmation_id = await confirm(env)
    with pytest.raises(asyncio.CancelledError):
        await applier(env, channel).apply(confirmation_id)
    assert (await status_of(env))[0] == "applying"

    jobs = handlers(env, channel)
    await jobs.reconcile_pending()
    assert (await status_of(env))[0] == "applying"  # still inside the grace period
    env.clock.advance(minutes=15)
    await jobs.reconcile_pending()
    assert await status_of(env) == ("failed", None)
    assert "interrupted" in channel.sends[-1]
    assert (await applier(env, channel).apply(confirmation_id)).status == "skipped"


async def test_an_apply_the_reconciler_failed_meanwhile_is_not_reported_done(env: Env) -> None:
    """Review 3 item 3: the handler outlived APPLYING_TIMEOUT and the
    reconciler failed the row (and told the owner) while it ran. The final
    `applying → applied` matches nothing, so the owner hears "may or may not
    have been applied", never "Done"."""
    _, channel = await held_call(env, "synthetic")
    confirmation_id = await confirm(env)
    jobs = handlers(env, channel)

    async def slow_note(ctx: ToolContext, args: Any) -> ToolOutcome:
        env.clock.advance(minutes=15)
        await jobs.reconcile_pending()  # fails the row left `applying`
        return ToolOutcome("ok")

    registry = default_registry()
    registry.register(dataclasses.replace(SET_NOTE, handler=slow_note))
    slow = ConfirmationApplier(
        session_factory=env.factory,
        clock=env.clock,
        channel=channel,
        registry=registry,
        services=ToolServices(
            session_factory=env.factory, clock=env.clock, monthly_budget_usd=None, channel=channel
        ),
        enabled_flags=ALL_FLAGS,
    )

    outcome = await slow.apply(confirmation_id)

    assert outcome.status == "failed"
    assert await status_of(env) == ("failed", None)
    assert "interrupted" in channel.sends[-2]
    assert "may or may not have been applied" in channel.sends[-1]
    assert "Done" not in channel.sends[-1]


async def test_a_pending_change_whose_card_expired_gets_a_new_card(env: Env) -> None:
    """Review 3 gap (a): the card still exists but its `expires_at` passed, so
    nothing can confirm it; the same change from a later run is not reported
    as awaiting that dead card."""
    await connect(env)
    channel = FakeChannel()
    await forwarded_note_run(env, channel, "a1")
    env.clock.advance(hours=25)  # past CARD_TTL
    again = await forwarded_note_run(env, channel, "b1")
    assert again["status"] == "awaiting_confirmation"
    assert "already awaits" not in json.dumps(again)
    async with env.factory() as session:
        statuses = sorted((await session.execute(select(ToolConfirmation.status))).scalars().all())
        live = (
            (
                await session.execute(
                    select(PendingInput)
                    .where(PendingInput.kind == "tool_confirm")
                    .where(PendingInput.expires_at > env.clock.now())
                )
            )
            .scalars()
            .all()
        )
    assert statuses == ["expired", "pending"]
    assert len(live) == 1
