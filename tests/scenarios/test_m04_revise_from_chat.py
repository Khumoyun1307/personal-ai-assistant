"""PF-13 (ISSUE-006, -008, -009): the owner edits and reads the live card from chat.

Owner decision D3 of 2026-10-30. `revise_draft` changes the chat's live card
through the card's own revision machinery — the **same** action, version + 1,
a new approval token, never a supersede and never a send — and `get_draft`
reads it back with exact counts. Through the production wiring (`_wire_m04`,
the real inbox, the owner runtime, `RevisionJob`); only the clock, the
transport and the model are fake. Every text is synthetic.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select

from assistant.actions import lifecycle
from assistant.actions.models import Action
from assistant.agent.models import AgentRun, AgentRunStep
from assistant.agent.tools.executor import ToolExecutor
from assistant.agent.tools.registry import (
    CapabilityTier,
    OwnerTurn,
    ToolContext,
    ToolServices,
    default_registry,
)
from assistant.agent.untrusted import Source, wrap
from assistant.db.uow import transaction
from assistant.llm.port import LLMRequest, Provenance, TextItem, ToolCall
from tests.integration.agent_env import ask
from tests.integration.business_env import business_message, deliver
from tests.integration.p2_factories import seed_account, seed_action, seed_conversation
from tests.scenarios.p2_drive import (
    DRAFT,
    FLAGS,
    Scenario,
    approve,
    edit,
    entered_or_finished,
    m04_environment,
    press_action,
    revision_result,
    run_queued,
    tool_outputs,
)

pytestmark = pytest.mark.integration

DRAFT_FLAGS = frozenset({"tool.revise_draft", "tool.get_draft"})
#: A synthetic 8-word Uzbek (Latin) sentence with typographic apostrophes:
#: whitespace splits it into exactly 8 words.
EIGHT_WORDS = "Ertaga ertalab o‘nda ofisda ko‘rishamiz, iltimos kechikmang do‘stim."
PHRASE = "SYNTH see you at noon"
SENT = re.compile(r"\bsent\b", re.IGNORECASE)


@pytest.fixture
async def s(migrated_url: str) -> AsyncIterator[Scenario]:
    async with m04_environment(migrated_url, flags=FLAGS | DRAFT_FLAGS) as scenario:
        yield scenario


def revise_args(
    s: Scenario,
    *,
    action_id: int | None = None,
    append_text: str | None = None,
    min_words: int | None = None,
    max_words: int | None = None,
) -> dict[str, Any]:
    """Every field: the strict schema requires them all (null = not given)."""
    return {
        "conversation_id": None if action_id is not None else s.conversation_id,
        "action_id": action_id,
        "append_text": append_text,
        "min_words": min_words,
        "max_words": max_words,
    }


def last_output(s: Scenario) -> dict[str, Any]:
    outputs = tool_outputs(s)
    assert outputs, "the owner run must have called a tool"
    return outputs[-1]


def draft_requests(s: Scenario) -> list[LLMRequest]:
    return [request for request in s.fake.requests if request.purpose == "draft_reply"]


def text_items(request: LLMRequest) -> list[TextItem]:
    return [item for item in request.items if isinstance(item, TextItem)]


async def owner_runs(s: Scenario) -> list[AgentRun]:
    async with s.env.factory() as session:
        return list(
            await session.scalars(
                select(AgentRun)
                .where(AgentRun.account_id == s.account_id, AgentRun.run_type == "owner_request")
                .order_by(AgentRun.id)
            )
        )


def never_says_sent(output: dict[str, Any]) -> None:
    assert not SENT.search(json.dumps(output))  # RG-20


# ------------------------------------------------------------ append
async def test_add_this_phrase_appends_to_the_same_card_without_a_model_call(
    s: Scenario,
) -> None:
    row = await s.propose()
    drafts_before = len(draft_requests(s))
    await s.owner(
        "revise_draft",
        said=f"Alviro, add this to the draft: {PHRASE}",
        **revise_args(s, append_text=PHRASE),
    )
    rows = await s.rows()
    assert [r.id for r in rows] == [row.id]  # the same card: never a supersede
    revised = rows[0]
    assert (revised.state, revised.version, revised.text) == (
        "awaiting_approval",
        2,
        f"{DRAFT} {PHRASE}",
    )
    assert revised.approval_token
    assert revised.approval_token != row.approval_token
    assert revised.revision_turn_id is None
    assert len(draft_requests(s)) == drafts_before  # deterministic: no model call
    events = await s.events(row.id)
    assert "action_superseded" not in events
    assert events[-2:] == ["action_revision_requested", "action_edited"]
    output = last_output(s)
    assert output["status"] == "revised"
    assert (output["action_id"], output["version"], output["changed"]) == (row.id, 2, "appended")
    assert output["word_count"] == len(f"{DRAFT} {PHRASE}".split())
    assert "text" not in output  # facts only; get_draft reads the text
    never_says_sent(output)
    assert PHRASE in s.final_card(revised)["text"]
    # The old card's Approve no longer works; the new version's does.
    await press_action(s, row.id, "ok", token=row.approval_token)
    assert (await s.action(row.id)).state == "awaiting_approval"
    await approve(s, row.id)
    assert (await s.action(row.id)).state == "approved"
    assert s.business_sends() == []


async def test_append_text_must_be_the_owners_own_words(s: Scenario) -> None:
    row = await s.propose()
    await s.owner(
        "revise_draft",
        said="Alviro, add a friendly sign-off to the draft.",
        **revise_args(s, append_text="SYNTH words the owner never wrote"),
    )
    output = last_output(s)
    assert output["status"] == "error"
    assert output["fields"] == ["append_text"]
    after = await s.action(row.id)
    assert (after.state, after.version, after.text) == ("awaiting_approval", 1, DRAFT)
    assert after.approval_token == row.approval_token
    assert "action_revision_requested" not in await s.events(row.id)


# ------------------------------------------------------------ instruction
async def test_an_instruction_revises_through_draft_owner_with_the_current_text_and_owner_words(
    s: Scenario,
) -> None:
    row = await s.propose()
    said = "Make the draft shorter and warmer, SYNTH-CANARY-SHORTER"
    s.fake.parsed.append(revision_result(text="SYNTH shorter warm reply."))
    await s.owner("revise_draft", said=said, **revise_args(s))
    [revised] = await s.rows()
    assert revised.id == row.id
    assert (revised.state, revised.version, revised.text) == (
        "awaiting_approval",
        2,
        "SYNTH shorter warm reply.",
    )
    items = text_items(draft_requests(s)[-1])
    current = next(item for item in items if "The current draft" in item.text)
    assert wrap(DRAFT, Source.DRAFT) in current.text
    assert current.provenance is Provenance.MANAGED
    owner_words = next(item for item in items if said in item.text)
    assert owner_words.provenance is Provenance.OWNER_CONTROL
    assert any(item.text.startswith("Task: revise.") for item in items)
    assert not any("tapped Edit" in item.text for item in items)  # the chat, not the button
    async with s.env.factory() as session:
        draft_run = await session.scalar(
            select(AgentRun).where(AgentRun.id == revised.draft_run_id)
        )
    assert draft_run is not None
    assert draft_run.prompt_version == "draft_owner.v2"
    assert draft_run.trigger_ref.startswith(f"revise:action:{row.id}:turn:")
    output = last_output(s)
    assert (output["status"], output["version"], output["changed"]) == ("revised", 2, "rewritten")
    assert output["word_count"] == 4
    never_says_sent(output)
    assert "action_superseded" not in await s.events(row.id)
    assert "Revising…" in s.control_text()
    assert s.business_sends() == []


async def test_from_chat_the_draft_writers_text_is_used_even_for_a_replacement(
    s: Scenario,
) -> None:
    """After Edit a replacement keeps the owner's whole message verbatim;
    from the chat the message is addressed to the assistant, so it never
    becomes the text itself."""
    await s.propose()
    said = "Alviro, change the draft to: SYNTH new wording please"
    s.fake.parsed.append(
        revision_result(text="SYNTH new wording please", revision_kind="replacement")
    )
    await s.owner("revise_draft", said=said, **revise_args(s))
    revised = await s.action()
    assert revised.text == "SYNTH new wording please"
    assert said not in (revised.text or "")


async def test_a_word_range_is_counted_and_retried_once(s: Scenario) -> None:
    await s.propose()
    s.fake.parsed.append(revision_result(text="one two three four five six seven eight nine"))
    s.fake.parsed.append(revision_result(text="one two three four five six"))
    await s.owner(
        "revise_draft", said="Make it 5-7 words.", **revise_args(s, min_words=5, max_words=7)
    )
    revised = await s.action()
    assert (revised.version, revised.text) == (2, "one two three four five six")
    output = last_output(s)
    assert (output["word_count"], output["length_met"]) == (6, True)
    assert output["asked_length"] == "between 5 and 7 words"
    retry = draft_requests(s)[-1]
    assert any("That draft has 9 words" in item.text for item in text_items(retry))


async def test_a_range_missed_twice_stages_the_closer_draft_with_its_honest_count(
    s: Scenario,
) -> None:
    await s.propose()
    s.fake.parsed.append(revision_result(text="one two three four five six seven eight nine"))
    s.fake.parsed.append(revision_result(text="one two three four five six seven eight"))
    await s.owner("revise_draft", said="At most 7 words.", **revise_args(s, max_words=7))
    output = last_output(s)
    assert (output["status"], output["word_count"], output["length_met"]) == ("revised", 8, False)


async def test_approve_while_revising_from_chat_is_rejected(s: Scenario) -> None:
    row = await s.propose()
    old_token = row.approval_token
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(_request: LLMRequest) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return revision_result(text="SYNTH revised while waiting.")

    s.fake.parsed.append(held)
    task = asyncio.create_task(
        s.owner("revise_draft", said="Make it more formal.", **revise_args(s))
    )
    try:
        await entered_or_finished(task, entered)
        assert (await s.action(row.id)).state == "revising"
        await press_action(s, row.id, "ok", token=old_token)
        assert (await s.action(row.id)).state == "revising"
        assert s.business_sends() == []
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    await task
    revised = await s.action(row.id)
    assert (revised.state, revised.version) == ("awaiting_approval", 2)
    assert revised.approval_token != old_token
    assert s.business_sends() == []


async def test_a_forwarded_turn_is_never_a_revision_instruction(s: Scenario) -> None:
    row = await s.propose()
    await s.owner("revise_draft", forwarded=True, said="SYNTH forwarded text.", **revise_args(s))
    output = last_output(s)
    assert (output["status"], output["reason"]) == ("not_revised", "owner_message_not_usable")
    after = await s.action(row.id)
    assert (after.state, after.version, after.approval_token) == (
        "awaiting_approval",
        1,
        row.approval_token,
    )


async def test_no_live_card_and_a_card_being_edited_are_said_as_they_are(s: Scenario) -> None:
    await s.owner("revise_draft", said="Shorten the draft.", **revise_args(s))
    assert last_output(s)["status"] == "no_live_card"
    row = await s.propose()
    # The owner edited it with the card's button; that revision is queued.
    await edit(s, row.id, "Synthetic: make it formal.")
    await s.owner("revise_draft", said="Alviro, shorten the draft.", **revise_args(s))
    assert last_output(s)["status"] == "already_being_revised"
    after = await s.action(row.id)
    assert (after.state, after.version) == ("revising", 1)


async def test_a_card_past_its_approval_time_expires_instead_of_revising(s: Scenario) -> None:
    row = await s.seed(expires_at=s.env.clock.now() - timedelta(minutes=1))
    await s.owner("revise_draft", said=f"Add: {PHRASE}", **revise_args(s, append_text=PHRASE))
    output = last_output(s)
    assert (output["status"], output["reason"]) == ("no_live_card", "expired")
    after = await s.action(row.id)
    assert (after.state, after.version, after.text) == ("expired", 1, DRAFT)


# ------------------------------------------------------------ the Edit button (A18)
async def test_the_edit_button_revises_through_the_same_draft_owner_path(s: Scenario) -> None:
    row = await s.propose()
    s.fake.parsed.append(revision_result(text="SYNTH button revision."))
    await edit(s, row.id, "Synthetic: make it shorter.")
    assert await run_queued(s, "replies.revise") == 1
    revised = await s.action(row.id)
    assert (revised.id, revised.version, revised.text) == (row.id, 2, "SYNTH button revision.")
    assert any("tapped Edit" in item.text for item in text_items(draft_requests(s)[-1]))
    async with s.env.factory() as session:
        run = await session.scalar(select(AgentRun).where(AgentRun.id == revised.draft_run_id))
    assert run is not None
    assert run.prompt_version == "draft_owner.v2"


async def test_a_chat_revision_recovered_by_the_job_keeps_its_chat_semantics(s: Scenario) -> None:
    """The owner's run died after the card entered `revising`: the revising
    reconciler re-defers `replies.revise`, which still treats the turn as a
    request to the assistant, never as the new text itself."""
    row = await s.propose()
    said = "Alviro, change the draft to: SYNTH recovered wording"
    turn = await _owner_turn(s, said)
    async with transaction(s.env.factory) as uow:
        started = await lifecycle.start_revision(
            uow.session,
            account_id=s.account_id,
            action_id=row.id,
            expected_version=1,
            turn_id=turn.id,
            now=s.env.clock.now(),
            details={"tool": "revise_draft", "run_id": 9201, "call_id": "c-r", "turn_id": turn.id},
        )
    assert started is lifecycle.RevisionStart.STARTED
    assert s.env.scheduler is not None
    await s.env.scheduler.defer("replies.revise", args={"action_id": row.id, "turn_id": turn.id})
    s.fake.parsed.append(
        revision_result(text="SYNTH recovered wording", revision_kind="replacement")
    )
    assert await run_queued(s, "replies.revise") == 1
    revised = await s.action(row.id)
    assert (revised.version, revised.text) == (2, "SYNTH recovered wording")
    assert not any("tapped Edit" in item.text for item in text_items(draft_requests(s)[-1]))


# ------------------------------------------------------------ get_draft
async def test_get_draft_wraps_the_text_taints_the_run_and_counts_exactly(s: Scenario) -> None:
    row = await s.seed(text=EIGHT_WORDS)
    await s.owner(
        "get_draft",
        said="How many words is the draft?",
        conversation_id=s.conversation_id,
        action_id=None,
    )
    output = last_output(s)
    assert output["status"] == "ok"
    assert output["text"] == wrap(EIGHT_WORDS, Source.DRAFT)
    assert '<untrusted source="draft"' in output["text"]
    assert (output["word_count"], output["char_count"]) == (8, len(EIGHT_WORDS))
    assert (output["action_id"], output["state"], output["version"], output["live"]) == (
        row.id,
        "awaiting_approval",
        1,
        True,
    )
    assert output["origin"] == "agent_proposal"
    assert output["answered_message_id"] == row.base_message_id
    never_says_sent(output)
    run = (await owner_runs(s))[-1]
    assert run.tainted is True
    async with s.env.factory() as session:
        steps = list(
            await session.scalars(select(AgentRunStep).where(AgentRunStep.run_id == run.id))
        )
    assert steps
    for step in steps:
        assert EIGHT_WORDS not in json.dumps(
            {c.name: str(getattr(step, c.name)) for c in step.__table__.columns}
        )


async def test_get_draft_without_a_live_card_reads_the_latest_and_says_it(s: Scenario) -> None:
    await s.owner(
        "get_draft",
        said="What does the draft say?",
        conversation_id=s.conversation_id,
        action_id=None,
    )
    assert last_output(s)["status"] == "no_draft"
    row = await s.propose()
    await deliver(s.env, business_message(message_id=4501, message_text="SYNTH", date=s.date + 1))
    assert (await s.action(row.id)).state == "outdated"
    await s.owner(
        "get_draft", said="And the draft?", conversation_id=s.conversation_id, action_id=None
    )
    output = last_output(s)
    assert (output["action_id"], output["state"], output["live"]) == (row.id, "outdated", False)


# ------------------------------------------------------------ ids and retries
def _context(s: Scenario, *, run_id: int, turn: OwnerTurn, call_id: str) -> ToolContext:
    wiring = s.wiring
    return ToolContext(
        account_id=s.account_id,
        run_id=run_id,
        run_type="owner_request",
        tainted=True,
        call_id=call_id,
        turn_id=turn.id,
        owner_turn=turn,
        managed_ok=True,
        services=ToolServices(
            session_factory=s.env.factory,
            clock=s.env.clock,
            monthly_budget_usd=None,
            drafter=wiring.drafter,
            approval_cards=wiring.approval_cards,
            actions_config=wiring.actions_config,
            card_reviser=wiring.card_reviser,
        ),
    )


async def _call(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """One call through a fresh executor (no in-memory replay between calls)."""
    offered = default_registry().for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=DRAFT_FLAGS,
        managed_consent=True,
    )

    async def record(*_step: object) -> None:
        return None

    executor = ToolExecutor(
        context=ctx, offered=offered, record_step=record, clock=ctx.services.clock
    )
    item = await executor.execute(ToolCall(ctx.call_id, name, json.dumps(arguments)))
    result: dict[str, Any] = json.loads(item.output)
    return result


async def _owner_turn(s: Scenario, said: str) -> OwnerTurn:
    turn_id = await ask(s.env, said)
    return OwnerTurn(id=turn_id, text=said, third_party=False, contains_managed=False, quoted=False)


async def test_a_retried_call_or_owner_turn_never_bumps_the_version_twice(s: Scenario) -> None:
    row = await s.propose()
    turn = await _owner_turn(s, f"Add: {PHRASE}")
    arguments = revise_args(s, append_text=PHRASE)
    call = _context(s, run_id=9001, turn=turn, call_id="c-1")
    first = await _call(call, "revise_draft", arguments)
    assert (first["status"], first["version"]) == ("revised", 2)
    again = await _call(call, "revise_draft", arguments)
    retried_turn = await _call(
        _context(s, run_id=9002, turn=turn, call_id="c-9"), "revise_draft", arguments
    )
    for replay in (again, retried_turn):
        assert (replay["status"], replay["version"], replay["reused"]) == ("revised", 2, True)
    after = await s.action(row.id)
    assert (after.version, after.text) == (2, f"{DRAFT} {PHRASE}")
    assert (await s.events(row.id)).count("action_edited") == 1
    # A new call in the same run is a new request: a second change is made.
    second = await _call(
        _context(s, run_id=9001, turn=turn, call_id="c-2"), "revise_draft", arguments
    )
    assert (second["status"], second["version"]) == ("revised", 3)


async def test_foreign_and_unknown_ids_read_not_found(s: Scenario) -> None:
    async with transaction(s.env.factory) as uow:
        other = await seed_account(uow.session)
        chat = await seed_conversation(uow.session, account_id=other.id)
        foreign = await seed_action(
            uow.session,
            account_id=other.id,
            conversation_id=chat.id,
            state="awaiting_approval",
            approval_token="QAseed9999",
            text=EIGHT_WORDS,
        )
        foreign_id, foreign_chat = foreign.id, chat.id
    turn = await _owner_turn(s, f"Add: {PHRASE}")
    ctx = _context(s, run_id=9101, turn=turn, call_id="c-f")
    for name, arguments in (
        ("get_draft", {"conversation_id": None, "action_id": foreign_id}),
        ("get_draft", {"conversation_id": foreign_chat, "action_id": None}),
        ("get_draft", {"conversation_id": None, "action_id": 987_654_321}),
        ("revise_draft", revise_args(s, action_id=foreign_id, append_text=PHRASE)),
        ("revise_draft", {**revise_args(s, append_text=PHRASE), "conversation_id": foreign_chat}),
    ):
        result = await _call(ctx, name, arguments)
        assert result == {"status": "error", "reason": "not found"}, (name, arguments)
        assert EIGHT_WORDS not in json.dumps(result)
    async with s.env.factory() as session:
        untouched = await session.scalar(select(Action).where(Action.id == foreign_id))
    assert untouched is not None
    assert (untouched.state, untouched.version) == ("awaiting_approval", 1)
