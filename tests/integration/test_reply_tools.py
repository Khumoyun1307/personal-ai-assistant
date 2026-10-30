"""S04.2 §3.5: `propose_reply`, `propose_message` (ARCHITECTURE §8.3; AGENTS §8 list).

Calls go through the real `ToolExecutor` (offer check, argument validation,
"not found" for foreign ids) with real `ToolServices`: the `Drafter` over a
scripted `FakeLLM`, F0's recording presenter and an `ActionsConfig`. One test
runs the whole owner runtime for the content-free step rows. Synthetic text.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from structlog.testing import capture_logs

from assistant.accounts.models import Account
from assistant.actions.models import Action
from assistant.agent.models import AgentRunStep, ToolConfirmation
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime
from assistant.agent.tools.common import ONE_TARGET_RULE
from assistant.agent.tools.executor import ToolExecutor
from assistant.agent.tools.registry import (
    CHAT_ACCESS_OFF,
    CapabilityTier,
    OwnerTurn,
    ToolContext,
    ToolServices,
    default_registry,
)
from assistant.agent.tools.replies import GUIDANCE_CHARS, ProposeReplyArgs, propose_reply_tool
from assistant.conversations.models import Contact, Conversation, Message
from assistant.llm.port import StopReason, TextItem, ToolCall
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import text_response, tool_response
from tests.integration.actions_env import seed_run, seed_turn
from tests.integration.agent_env import PRODUCTION_FLAGS, PROPOSE_FLAGS, ask, gated
from tests.integration.business_env import (
    OTHER_CONTACT,
    business_message,
    deleted_messages,
    deliver,
)
from tests.integration.drafting_env import (
    DraftEnv,
    actions_of,
    contact_deletes,
    contact_says,
    contact_shares_location,
    drafting_environment,
    later,
    message_row_id,
    owner_says,
    seed_chat,
)
from tests.unit.test_draft_owner_units import owner_output

pytestmark = pytest.mark.integration

GUIDANCE_CANARY = "SYNTH-GUIDANCE-CANARY offer Monday"
SENT = re.compile(r"\bsent\b", re.IGNORECASE)


@pytest.fixture
async def denv(migrated_url: str) -> AsyncIterator[DraftEnv]:
    async with drafting_environment(migrated_url) as built:
        yield built


async def run_and_turn(denv: DraftEnv, account_id: int) -> tuple[int, int]:
    async with denv.env.factory() as session, session.begin():
        return await seed_run(session, account_id), await seed_turn(session, account_id)


def services(denv: DraftEnv, **changes: Any) -> ToolServices:
    values: dict[str, Any] = {
        "session_factory": denv.env.factory,
        "clock": denv.env.clock,
        "monthly_budget_usd": None,
        "drafter": denv.drafter(),
        "approval_cards": denv.presenter,
        "actions_config": denv.config,
    }
    values.update(changes)
    return ToolServices(**values)


#: The owner's own words in the turn these tests answer: it dictates every
#: synthetic text the tests below propose (`propose_message` checks that a
#: dictated text is the owner's, PF-12).
OWNER_SAID = (
    "Tell them: SYNTH-DICTATED. Also SYNTH-ONE, SYNTH-X, SYNTH-Y, SYNTH-BY-CONTACT and "
    "SYNTH-AND-ONE-MORE-THING."
)


def owner_turn(turn_id: int | None, said: str, **flags: bool) -> OwnerTurn:
    values = {"third_party": False, "contains_managed": False, "quoted": False} | flags
    return OwnerTurn(id=turn_id or 0, text=said, **values)


def context(
    denv: DraftEnv,
    account_id: int,
    run_id: int,
    turn_id: int | None,
    *,
    tainted: bool = False,
    said: str | None = OWNER_SAID,
    turn: OwnerTurn | None = None,
    **service_changes: Any,
) -> ToolContext:
    """A tool context as the runtime builds it; `said` is the owner's turn
    (None: no owner turn, as for a call applied after a confirmation tap)."""
    return ToolContext(
        account_id=account_id,
        run_id=run_id,
        run_type="owner_request",
        tainted=tainted,
        services=services(denv, **service_changes),
        turn_id=turn_id,
        owner_turn=turn or (owner_turn(turn_id, said) if said is not None else None),
        managed_ok=True,
    )


async def call(
    ctx: ToolContext,
    name: str,
    *,
    call_id: str = "c1",
    managed: bool = True,
    flags: frozenset[str] = PROPOSE_FLAGS,
    **arguments: Any,
) -> dict[str, Any]:
    """One call through the executor, as the runtime makes it."""
    registry = default_registry()
    offered = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=flags,
        managed_consent=managed,
        stub_managed_tools=True,
    )

    async def record(*_step: object) -> None:
        return None

    executor = ToolExecutor(
        context=ctx, offered=offered, record_step=record, clock=ctx.services.clock
    )
    item = await executor.execute(ToolCall(call_id, name, json.dumps(arguments)))
    result: dict[str, Any] = json.loads(item.output)
    return result


def reply_args(
    conversation_id: int | None,
    *,
    contact_id: int | None = None,
    guidance: Any = None,
    mode: str = "reply",
    answered: int | None = None,
    min_words: int | None = None,
    max_words: int | None = None,
) -> dict[str, Any]:
    """Every field: the strict schema requires them all (null = not given)."""
    return {
        "conversation_id": conversation_id,
        "contact_id": contact_id,
        "mode": mode,
        "answered_message_id": answered,
        "guidance": guidance,
        "min_words": min_words,
        "max_words": max_words,
    }


def message_args(
    conversation_id: int | None,
    words: str,
    *,
    contact_id: int | None = None,
    answered: int | None = None,
) -> dict[str, Any]:
    """Every field: the strict schema requires them all (null = not given)."""
    return {
        "conversation_id": conversation_id,
        "contact_id": contact_id,
        "text": words,
        "answered_message_id": answered,
    }


async def close_window(denv: DraftEnv, conversation_id: int) -> None:
    async with denv.env.factory() as session, session.begin():
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(last_contact_msg_at=denv.env.clock.now() - timedelta(hours=30))
        )


async def possibly_paused(denv: DraftEnv, conversation_id: int) -> None:
    """A proposal-phase block that is not the window (P2-O20 lifted that one
    for owner-approved actions): the chat looks paused."""
    async with denv.env.factory() as session, session.begin():
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(availability="possibly_paused")
        )


def never_says_sent(result: dict[str, Any]) -> None:
    assert not SENT.search(json.dumps(result)), result


def draft_prompts(denv: DraftEnv) -> list[str]:
    return [
        request.instructions + "\n".join(i.text for i in request.items if isinstance(i, TextItem))
        for request in denv.fake.requests
        if request.purpose == "draft_reply"
    ]


# ------------------------------------------------------------ RG-20 and statuses
async def test_rg20_propose_results_never_say_sent(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)

    denv.script(owner_output(text="Synthetic reply; it was sent? No, a draft."))
    awaiting = await call(
        context(denv, account_id, run_id, turn_id), "propose_reply", **reply_args(conversation)
    )
    assert awaiting["status"] == "awaiting_approval"
    assert awaiting["card"] == "queued for the owner"
    assert "text" not in awaiting  # the owner reads the draft on the card
    assert awaiting["sendable_until"] is not None
    never_says_sent(awaiting)

    # An owner's request is never "no reply needed" (D1): only the provider's
    # refusal or an unusable output ends without a card, said as it is.
    denv.script(StopReason.REFUSAL)
    declined = await call(
        context(denv, account_id, run_id, None),
        "propose_reply",
        call_id="c2",
        **reply_args(conversation),
    )
    assert declined == {
        "status": "declined_by_provider",
        "conversation_id": conversation,
        "reason": "provider_refused",
    }
    never_says_sent(declined)
    denv.script(owner_output(text="   "))
    not_drafted = await call(
        context(denv, account_id, run_id, None),
        "propose_reply",
        call_id="c2b",
        **reply_args(conversation),
    )
    assert not_drafted == {
        "status": "not_drafted",
        "conversation_id": conversation,
        "reason": "invalid",
    }
    never_says_sent(not_drafted)

    await possibly_paused(denv, conversation)
    blocked = await call(
        context(denv, account_id, run_id, None),
        "propose_message",
        call_id="c3",
        **message_args(conversation, "SYNTH-DICTATED"),
    )
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "policy_denied"
    assert blocked["sendable_until"] is None
    never_says_sent(blocked)


async def test_propose_results_are_logged_as_not_run_never_as_failures(denv: DraftEnv) -> None:
    """ICR-3: a card awaiting approval, a blocked proposal and "not drafted"
    are expected outcomes, logged at info like a held confirmation — a
    warning would page operations for every normal proposal."""
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    with capture_logs() as logs:
        denv.script(owner_output(text="Synthetic reply."))
        await call(
            context(denv, account_id, run_id, turn_id), "propose_reply", **reply_args(conversation)
        )
        denv.script(owner_output(text=""))
        await call(
            context(denv, account_id, run_id, None),
            "propose_reply",
            call_id="c2",
            **reply_args(conversation),
        )
        denv.script(StopReason.REFUSAL)
        await call(
            context(denv, account_id, run_id, None),
            "propose_reply",
            call_id="c2b",
            **reply_args(conversation),
        )
        await possibly_paused(denv, conversation)
        await call(
            context(denv, account_id, run_id, None),
            "propose_message",
            call_id="c3",
            **message_args(conversation, "SYNTH-DICTATED"),
        )
    lines = [
        (entry["event"], entry["log_level"], entry["status"])
        for entry in logs
        if entry["event"] in {"agent_tool_call_failed", "agent_tool_call_not_run"}
    ]
    assert lines == [
        ("agent_tool_call_not_run", "info", "awaiting_approval"),
        ("agent_tool_call_not_run", "info", "not_drafted"),
        ("agent_tool_call_not_run", "info", "declined_by_provider"),
        ("agent_tool_call_not_run", "info", "blocked"),
    ]


async def test_with_sending_off_proposals_are_normal_awaiting_cards(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    off = replace(denv.config, sending_enabled=False)
    result = await call(
        context(denv, account_id, run_id, turn_id, actions_config=off),
        "propose_message",
        **message_args(conversation, "SYNTH-DICTATED"),
    )
    assert result["status"] == "awaiting_approval"
    [card] = await actions_of(denv.env, conversation)
    assert card.state == "awaiting_approval"
    assert card.approval_token


async def test_propose_message_keeps_the_owners_exact_text(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    words = "SYNTH  exact,  words!\nLine two — ok 👍"
    result = await call(
        context(denv, account_id, run_id, turn_id, said=f"Send them exactly this: {words}"),
        "propose_message",
        **message_args(conversation, f"  {words}\n"),
    )
    assert result["status"] == "awaiting_approval"
    [card] = await actions_of(denv.env, conversation)
    assert card.text == words
    assert denv.fake.requests == []  # no model run for dictated text


async def test_propose_message_has_no_base_message(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    await owner_says(denv, "SYNTH-OWNER-LATEST")
    run_id, turn_id = await run_and_turn(denv, account_id)
    await call(
        context(denv, account_id, run_id, turn_id),
        "propose_message",
        **message_args(conversation, "SYNTH-DICTATED"),
    )
    [card] = await actions_of(denv.env, conversation)
    async with denv.env.factory() as session:
        newest = await session.scalar(
            select(Message.id)
            .where(Message.conversation_id == conversation)
            .order_by(Message.id.desc())
            .limit(1)
        )
    assert card.origin == "owner_dictated"
    assert card.base_message_id is None
    assert card.shown_through_message_id == newest  # newest of any direction
    assert card.draft_flags is None
    assert card.draft_run_id is None
    assert card.trigger_turn_id == turn_id
    assert (card.run_id, card.tool_call_id) == (run_id, "c1")


async def test_a_dictated_message_right_after_the_owners_manual_reply_is_a_normal_card(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    replied = await owner_says(denv, "SYNTH-OWNER-REPLIED-MANUALLY", date=later(120))
    # A minute after the owner's own manual reply (decisions R3).
    denv.env.clock.set(datetime.fromtimestamp(later(180), tz=UTC))
    run_id, turn_id = await run_and_turn(denv, account_id)
    result = await call(
        context(denv, account_id, run_id, turn_id),
        "propose_message",
        **message_args(conversation, "SYNTH-AND-ONE-MORE-THING"),
    )
    assert result["status"] == "awaiting_approval"
    [card] = await actions_of(denv.env, conversation)
    assert card.state == "awaiting_approval"
    assert card.base_message_id is None
    assert card.shown_through_message_id == await message_row_id(denv.env, replied)


# ------------------------------------------------------------ guidance and taint
async def test_propose_reply_in_a_tainted_run_ignores_the_guidance_and_says_so(
    denv: DraftEnv,
) -> None:
    # Decisions R2, kept for the MODEL's words: a tainted run's guidance may
    # restate another chat's text and never reaches the draft. (Owner
    # decision D2 of 2026-10-30 adds the owner's own turn, supplied by the
    # application: `test_owner_drafting.py`.) Without an owner turn nothing
    # of the owner's reached the draft, and the result says so.
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    result = await call(
        context(denv, account_id, run_id, turn_id, tainted=True, said=None),
        "propose_reply",
        **reply_args(conversation, guidance=GUIDANCE_CANARY),
    )
    assert result["status"] == "awaiting_approval"
    assert result["guidance_ignored"] == "tainted_run"
    assert result["owner_message_used"] is False
    [prompt] = draft_prompts(denv)
    assert "SYNTH-GUIDANCE-CANARY" not in prompt


async def test_a_tainted_runs_guidance_is_dropped_even_when_the_owner_turn_is_passed(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    result = await call(
        context(denv, account_id, run_id, turn_id, tainted=True, said="SYNTH-OWNER-OWN-WORDS"),
        "propose_reply",
        **reply_args(conversation, guidance=GUIDANCE_CANARY),
    )
    assert result["status"] == "awaiting_approval"
    assert "guidance_ignored" not in result  # the owner's own words did reach it
    assert result["owner_message_used"] is True
    [prompt] = draft_prompts(denv)
    assert "SYNTH-GUIDANCE-CANARY" not in prompt
    assert "SYNTH-OWNER-OWN-WORDS" in prompt


async def test_untainted_guidance_reaches_the_draft(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    result = await call(
        context(denv, account_id, run_id, turn_id),
        "propose_reply",
        **reply_args(conversation, guidance=f"  {GUIDANCE_CANARY}  "),
    )
    assert "guidance_ignored" not in result
    [prompt] = draft_prompts(denv)
    assert f"The assistant's note on what the owner wants:\n{GUIDANCE_CANARY}" in prompt


async def test_a_tainted_run_can_propose_but_only_a_card_results(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    sends_before = len(denv.env.sender.sends)
    result = await call(
        context(denv, account_id, run_id, turn_id, tainted=True),
        "propose_message",
        **message_args(conversation, "SYNTH-DICTATED"),
    )
    assert result["status"] == "awaiting_approval"  # not held: the card is the gate
    [card] = await actions_of(denv.env, conversation)
    assert card.state == "awaiting_approval"
    assert denv.presenter.presented == [card.id]
    # Nothing went anywhere but the card queue: no Telegram call at all.
    assert len(denv.env.sender.sends) == sends_before
    async with denv.env.factory() as session:
        assert (await session.scalars(select(ToolConfirmation))).all() == []


# ------------------------------------------------------------ ids, schema, idempotency
async def test_propose_tools_refuse_a_foreign_conversation_or_contact(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    async with denv.env.factory() as session, session.begin():
        other = Account(telegram_user_id=7_000_000_077, control_chat_id=7_000_000_077)
        session.add(other)
        await session.flush()
        stranger = Contact(account_id=other.id, telegram_user_id=OTHER_CONTACT)
        session.add(stranger)
        await session.flush()
        foreign = Conversation(
            account_id=other.id, contact_id=stranger.id, business_chat_id=OTHER_CONTACT
        )
        session.add(foreign)
        await session.flush()
        foreign_ids = (foreign.id, stranger.id)
    ctx = context(denv, account_id, run_id, turn_id)
    for name, arguments in (
        ("propose_reply", reply_args(foreign_ids[0])),
        ("propose_reply", reply_args(None, contact_id=foreign_ids[1])),
        ("propose_message", message_args(foreign_ids[0], "SYNTH")),
        ("propose_message", message_args(None, "SYNTH", contact_id=foreign_ids[1])),
        ("propose_message", message_args(999_999_999, "SYNTH")),
    ):
        assert await call(ctx, name, **arguments) == {"status": "error", "reason": "not found"}
    assert denv.fake.requests == []
    async with denv.env.factory() as session:
        assert (await session.scalars(select(Action))).all() == []
    assert conversation


async def test_schema_valid_but_both_or_neither_target_is_an_error(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    contact = await _contact_of(denv, conversation)
    for arguments in (
        reply_args(conversation, contact_id=contact),
        reply_args(None),
    ):
        result = await call(ctx, "propose_reply", **arguments)
        assert result["status"] == "error"
        assert {"field": "(root)", "rule": ONE_TARGET_RULE} in result["problems"]
    too_long = await call(ctx, "propose_reply", **reply_args(conversation, guidance="x" * 401))
    assert too_long["status"] == "error"
    assert too_long["fields"] == ["guidance"]
    assert GUIDANCE_CHARS == 400
    blank = await call(ctx, "propose_message", **message_args(conversation, "   "))
    assert blank["fields"] == ["text"]
    assert denv.fake.requests == []
    assert await actions_of(denv.env, conversation) == []


async def test_a_repeated_call_id_creates_no_second_action(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    first = await call(
        context(denv, account_id, run_id, None), "propose_reply", **reply_args(conversation)
    )
    # Another executor (a re-run), the same model call id: no second draft.
    again = await call(
        context(denv, account_id, run_id, None), "propose_reply", **reply_args(conversation)
    )
    assert again["action_id"] == first["action_id"]
    assert again["reused"] is True  # nothing new was created
    assert "reused" not in first
    assert len(denv.fake.requests) == 1
    dictated = await call(
        context(denv, account_id, run_id, None),
        "propose_message",
        call_id="c9",
        **message_args(conversation, "SYNTH-ONE"),
    )
    repeat = await call(
        context(denv, account_id, run_id, None),
        "propose_message",
        call_id="c9",
        **message_args(conversation, "SYNTH-ONE"),
    )
    assert repeat["action_id"] == dictated["action_id"]
    assert repeat["reused"] is True
    assert "reused" not in dictated
    assert len(await actions_of(denv.env, conversation)) == 2
    assert turn_id


async def test_a_retried_owner_turn_reuses_the_awaiting_card_without_drafting_again(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    first = await call(
        context(denv, account_id, run_id, turn_id), "propose_reply", **reply_args(conversation)
    )
    # The worker died; the turn is run again as a new run with new call ids.
    async with denv.env.factory() as session, session.begin():
        second_run = await seed_run(session, account_id)
    again = await call(
        context(denv, account_id, second_run, turn_id),
        "propose_reply",
        call_id="other",
        **reply_args(conversation),
    )
    assert again["status"] == "awaiting_approval"
    assert again["action_id"] == first["action_id"]
    assert again["reused"] is True
    assert len(denv.fake.requests) == 1  # no second draft, no cost
    assert len(await actions_of(denv.env, conversation)) == 1


async def test_a_second_proposal_in_the_same_turn_says_it_reused_the_first_card(
    denv: DraftEnv,
) -> None:
    # PR #106 review (Neryn): "tell them X ... also tell them Y" in one turn
    # gives one card (the turn's live card); the second result must not read
    # as a second queued message.
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    first = await call(ctx, "propose_message", **message_args(conversation, "SYNTH-X"))
    second = await call(
        ctx, "propose_message", call_id="c2", **message_args(conversation, "SYNTH-Y")
    )
    assert first["status"] == second["status"] == "awaiting_approval"
    assert second["action_id"] == first["action_id"]
    assert second["reused"] is True
    assert "reused" not in first
    [card] = await actions_of(denv.env, conversation)
    assert card.text == "SYNTH-X"


# ------------------------------------------------------------ a newest message with nothing to show
@pytest.mark.parametrize("newest", ["location", "deleted"])
async def test_propose_reply_after_a_location_or_a_deleted_message_drafts_once(
    denv: DraftEnv, newest: str
) -> None:
    # PR #106 review (Neryn): such a newest contact message made every draft
    # stale: two model calls, then always `not_drafted`/`chat_changed`.
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)
    await contact_says(denv, "SYNTH-QUESTION")
    if newest == "location":
        await contact_shares_location(denv)
    else:
        await contact_deletes(denv)
    denv.script(owner_output(text="Synthetic reply."))
    result = await call(
        context(denv, account_id, run_id, turn_id), "propose_reply", **reply_args(conversation)
    )
    assert result["status"] == "awaiting_approval"
    assert len(draft_prompts(denv)) == 1
    [card] = await actions_of(denv.env, conversation)
    assert card.id == result["action_id"]
    async with denv.env.factory() as session:
        newest_id = await session.scalar(
            select(func.max(Message.id)).where(Message.conversation_id == conversation)
        )
    assert card.shown_through_message_id == newest_id


# ------------------------------------------------------------ the offer
async def test_propose_tools_are_absent_without_their_flags(denv: DraftEnv) -> None:
    registry = default_registry()
    without = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=frozenset({"tool.get_status"}),
        managed_consent=True,
    )
    assert {"propose_reply", "propose_message"}.isdisjoint(without)
    only_message = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=frozenset({"tool.propose_message"}),
        managed_consent=True,
    )
    assert set(only_message) == {"propose_message"}
    for contact_run in ("draft_reply", "triage", "summarize"):
        assert (
            registry.for_run(
                contact_run,
                tier=CapabilityTier.CONTROL_ONLY,
                enabled_flags=PROPOSE_FLAGS,
                managed_consent=True,
            )
            == {}
        )
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    refused = await call(
        context(denv, account_id, run_id, turn_id),
        "propose_message",
        flags=frozenset(),
        **message_args(conversation, "SYNTH"),
    )
    assert refused == {"status": "error", "reason": "this tool is not available"}


async def test_propose_tools_are_blocked_stubs_without_managed_consent(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    for name, arguments in (
        ("propose_reply", reply_args(conversation)),
        ("propose_message", message_args(conversation, "SYNTH")),
    ):
        result = await call(ctx, name, managed=False, **arguments)
        assert result["status"] == CHAT_ACCESS_OFF
    assert denv.fake.requests == []
    assert await actions_of(denv.env, conversation) == []


async def test_propose_reply_into_an_ignore_mode_or_bot_chat_is_refused(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="ignore")
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    assert await call(ctx, "propose_reply", **reply_args(conversation)) == {
        "status": "not_drafted",
        "conversation_id": conversation,
        "reason": "ignored_chat",
    }
    async with denv.env.factory() as session, session.begin():
        contact = await session.scalar(
            select(Conversation.contact_id).where(Conversation.id == conversation)
        )
        await session.execute(update(Contact).where(Contact.id == contact).values(is_bot=True))
        await session.execute(
            update(Conversation).where(Conversation.id == conversation).values(mode="observe")
        )
    for name, arguments in (
        ("propose_reply", reply_args(conversation)),
        ("propose_message", message_args(conversation, "SYNTH")),
    ):
        result = await call(ctx, name, call_id=name, **arguments)
        assert (result["status"], result["reason"]) == ("not_drafted", "bot_chat")
    assert denv.fake.requests == []
    assert await actions_of(denv.env, conversation) == []


@pytest.mark.parametrize("tool", ["propose_reply", "propose_message"])
async def test_a_proposal_with_the_window_closed_is_still_an_approval_card(
    denv: DraftEnv, tool: str
) -> None:
    """Invariant 8 as amended (owner decision 2026-10-30, P2-O20): our 24 h
    estimate does not block an owner-requested draft or dictated message;
    the card (with its warning line) goes to the owner, Telegram decides."""
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    await close_window(denv, conversation)
    denv.script(owner_output(text="Synthetic reply."))
    arguments = (
        reply_args(conversation)
        if tool == "propose_reply"
        else message_args(conversation, "SYNTH-DICTATED")
    )
    result = await call(context(denv, account_id, run_id, turn_id), tool, **arguments)
    assert result["status"] == "awaiting_approval"
    assert result["sendable_until"] is not None
    [card] = await actions_of(denv.env, conversation)
    assert card.state == "awaiting_approval"
    assert card.approval_token is not None
    assert denv.presenter.presented == [card.id]
    never_says_sent(result)


async def test_a_tool_without_its_services_is_not_available(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    for missing in ("drafter", "approval_cards", "actions_config"):
        ctx = context(denv, account_id, run_id, turn_id, **{missing: None})
        name = "propose_message" if missing != "drafter" else "propose_reply"
        arguments = (
            message_args(conversation, "SYNTH")
            if name == "propose_message"
            else reply_args(conversation)
        )
        assert await call(ctx, name, call_id=missing, **arguments) == {
            "status": "error",
            "reason": "not available",
        }
    assert await actions_of(denv.env, conversation) == []


async def test_a_refused_proposal_is_not_available_and_logs_its_code(denv: DraftEnv) -> None:
    # PR #106 review (Neryn): the refusal code reaches the executor's log line.
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, _ = await run_and_turn(denv, account_id)
    await owner_says(denv, "SYNTH-OWNER-ONLY")
    # The contact's only message is gone: the draft has no base message, and
    # without an owner turn S04.1 refuses it (`base_required`).
    await deliver(denv.env, deleted_messages(message_ids=[1]))
    denv.script(owner_output(text="Synthetic reply."))
    outcome = await propose_reply_tool(
        context(denv, account_id, run_id, None),
        ProposeReplyArgs.model_validate(reply_args(conversation)),
    )
    assert (outcome.status, dict(outcome.data)) == ("error", {"reason": "not available"})
    assert outcome.log_code == "proposal_base_required"
    assert await actions_of(denv.env, conversation) == []


# ------------------------------------------------------------ through the runtime
async def test_tool_steps_are_content_free(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    canary = "SYNTH-DICTATED-CANARY 4410"
    turn = await ask(denv.env, f"Tell them {canary}")  # the owner dictates it (PF-12)
    denv.fake.queue(
        tool_response(("c1", "propose_message", json.dumps(message_args(conversation, canary)))),
        text_response("A card is waiting for your approval."),
    )
    runtime = OwnerRequestRuntime(
        session_factory=denv.env.factory,
        llm=gated(denv.env, denv.fake),
        channel=FakeChannel(),
        registry=default_registry(),
        clock=denv.env.clock,
        config=AgentConfig(model="gpt-6-luna", enabled_flags=PRODUCTION_FLAGS),
        drafter=denv.drafter(),
        approval_cards=denv.presenter,
        actions_config=denv.config,
    )
    await runtime.run(turn)
    [card] = await actions_of(denv.env, conversation)
    assert card.text == canary
    assert card.trigger_turn_id == turn
    async with denv.env.factory() as session:
        steps = (await session.scalars(select(AgentRunStep))).all()
        dumped = (
            (await session.execute(text("SELECT row_to_json(s)::text FROM app.agent_run_steps s")))
            .scalars()
            .all()
        )
    assert [(s.tool_name, s.status) for s in steps] == [("propose_message", "awaiting_approval")]
    assert all("SYNTH-DICTATED-CANARY" not in row for row in dumped)
    assert account_id


async def _contact_of(denv: DraftEnv, conversation_id: int) -> int:
    async with denv.env.factory() as session:
        found = await session.scalar(
            select(Conversation.contact_id).where(Conversation.id == conversation_id)
        )
        assert found is not None
        return int(found)


async def test_propose_by_contact_id_finds_the_chat(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    contact = await _contact_of(denv, conversation)
    result = await call(
        context(denv, account_id, run_id, turn_id),
        "propose_message",
        **message_args(None, "SYNTH-BY-CONTACT", contact_id=contact),
    )
    assert result["status"] == "awaiting_approval"
    assert result["conversation_id"] == conversation
    assert result["contact_name"].startswith('<untrusted source="contact_name"')
    await deliver(denv.env, business_message(message_id=999, message_text="SYNTH-LATER"))
    [card] = await actions_of(denv.env, conversation)
    assert card.state == "outdated"  # a newer contact message outdates a dictated card too
