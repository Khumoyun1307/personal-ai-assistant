"""PF-10, PF-11, PF-12, PF-04: owner-requested drafting (2026-10-30 owner playtest fixes).

The owner's own words reach the draft writer (`draft_owner`) even in a
tainted run, an explicit owner request is never vetoed as "no reply
needed", a reply can target a named message or be a new message, and a
"dictated" text must really be the owner's. Calls go through the real
`ToolExecutor` (and, where the bug lived, the whole owner runtime) with the
production LLM wiring over a scripted `FakeLLM`. Synthetic text and names
only.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from assistant.accounts.models import Account, ControlTurn
from assistant.agent import prompt_registry
from assistant.agent.draft_owner_schemas import WordBounds
from assistant.agent.drafting import DraftStatus
from assistant.agent.models import AgentRun
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime
from assistant.agent.tools.registry import default_registry
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.errors import NotInAccountError
from assistant.core.jobs import DraftReason
from assistant.llm.port import LLMRequest, Provenance, StopReason, TextItem
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import text_response, tool_response
from tests.integration.agent_env import PRODUCTION_FLAGS, args, ask, gated
from tests.integration.business_env import OTHER_CONTACT
from tests.integration.drafting_env import (
    DraftEnv,
    actions_of,
    contact_says,
    drafting_environment,
    later,
    message_row_id,
    output,
    owner_says,
    seed_chat,
)
from tests.integration.test_reply_tools import (
    call,
    context,
    draft_prompts,
    message_args,
    never_says_sent,
    owner_turn,
    reply_args,
    run_and_turn,
)
from tests.unit.test_draft_owner_units import owner_output

pytestmark = pytest.mark.integration

#: The owner's request in the control chat (synthetic; names invented).
OWNER_WORDS = "Rustamga javob yoz: SYNTH-OWNER-WORDS ertaga soat beshda kelaman, kechikmayman"


@pytest.fixture
async def denv(migrated_url: str) -> AsyncIterator[DraftEnv]:
    async with drafting_environment(migrated_url) as built:
        yield built


def owner_runtime(denv: DraftEnv) -> OwnerRequestRuntime:
    return OwnerRequestRuntime(
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


def draft_requests(denv: DraftEnv) -> list[LLMRequest]:
    return [request for request in denv.fake.requests if request.purpose == "draft_reply"]


async def runs(denv: DraftEnv, run_type: str) -> list[AgentRun]:
    async with denv.env.factory() as session:
        return list(
            await session.scalars(
                select(AgentRun).where(AgentRun.run_type == run_type).order_by(AgentRun.id)
            )
        )


# ------------------------------------------------------------ PF-11: the owner's words arrive
async def test_the_owner_turn_reaches_the_draft_after_resolve_contact_tainted_the_run(
    denv: DraftEnv,
) -> None:
    """ISSUE-005/007/008 (S1): every production run was tainted by a chat
    tool, so the owner's instructions never reached the draft. Through the
    real runtime: `resolve_contact` taints the run, then `propose_reply`."""
    account_id, conversation = await seed_chat(denv, mode="observe")
    turn = await ask(denv.env, OWNER_WORDS)
    denv.fake.queue(
        tool_response(("c1", "resolve_contact", args(name="Synthetic", limit=None))),
        tool_response(("c2", "propose_reply", json.dumps(reply_args(conversation)))),
        text_response("A card is waiting for your approval."),
    )
    denv.script(owner_output(text="SYNTH draft kelaman"))
    await owner_runtime(denv).run(turn)

    [owner_run] = await runs(denv, "owner_request")
    assert owner_run.tainted is True  # the chat tool's result tainted it
    [draft] = draft_requests(denv)
    owner_items = [item for item in draft.items if isinstance(item, TextItem)]
    words = next(item for item in owner_items if OWNER_WORDS in item.text)
    assert words.provenance is Provenance.OWNER_CONTROL  # ai_control, the owner's own text
    chat = next(item for item in owner_items if item.text.startswith("Messages in this chat"))
    assert chat.provenance is Provenance.MANAGED  # ai_managed, the contact's text
    [draft_run] = await runs(denv, "draft_reply")
    assert draft_run.prompt_version == "draft_owner.v2"
    assert prompt_registry.active("draft_owner").text in draft.instructions
    [card] = await actions_of(denv.env, conversation)
    assert (card.origin, card.state, card.trigger_turn_id) == (
        "agent_proposal",
        "awaiting_approval",
        turn,
    )
    assert card.draft_flags is not None
    assert card.draft_flags["source"] == "draft_owner"
    assert account_id


async def test_a_forwarded_owner_turn_never_reaches_the_draft(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    turn = await ask(
        denv.env,
        "SYNTH-FORWARDED-CANARY someone else's words",
        forward_origin={"type": "hidden_user", "date": 1, "sender_user_name": "Synth Other"},
    )
    denv.fake.queue(
        tool_response(("c1", "propose_reply", json.dumps(reply_args(conversation)))),
        text_response("A card is waiting."),
    )
    denv.script(owner_output())
    with capture_logs() as logs:
        await owner_runtime(denv).run(turn)
    [prompt] = draft_prompts(denv)
    assert "SYNTH-FORWARDED-CANARY" not in prompt
    [line] = [entry for entry in logs if entry.get("tool") == "propose_reply" and "code" in entry]
    assert line["instruction_source"] == "dropped_quoted"
    assert account_id


@pytest.mark.parametrize(
    "flags",
    [
        {"third_party": True, "quoted": True},  # quoting another chat's message
        {"contains_managed": True},  # carrying managed-chat content
        {"third_party": True},  # forwarded
    ],
)
async def test_a_quoting_forwarded_or_managed_turn_is_not_passed(
    denv: DraftEnv, flags: dict[str, bool]
) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    with capture_logs() as logs:
        result = await call(
            context(
                denv,
                account_id,
                run_id,
                turn_id,
                tainted=True,
                turn=owner_turn(turn_id, "SYNTH-NOT-PASSED-CANARY", **flags),
            ),
            "propose_reply",
            **reply_args(conversation),
        )
    assert result["status"] == "awaiting_approval"
    assert result["owner_message_used"] is False
    [prompt] = draft_prompts(denv)
    assert "SYNTH-NOT-PASSED-CANARY" not in prompt
    [line] = [entry for entry in logs if entry.get("tool") == "propose_reply" and "code" in entry]
    assert line["instruction_source"] == "dropped_quoted"


# ------------------------------------------------------------ PF-10: no veto, Assist unchanged
async def test_an_owner_request_where_no_reply_seems_needed_still_gets_a_card(
    denv: DraftEnv,
) -> None:
    """ISSUE-002/007 (S2): the draft writer's own "no reply needed" vetoed
    the owner 15 times. The owner's prompt has no such field (D1)."""
    account_id, conversation = await seed_chat(denv, mode="observe", first="SYNTH-THANKS")
    await owner_says(denv, "SYNTH-OWNER-OK", date=later(60))  # the owner wrote last
    denv.env.clock.set(datetime.fromtimestamp(later(120), tz=UTC))  # then asked
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(text="SYNTH new topic for Dilnoza"))
    result = await call(
        context(denv, account_id, run_id, turn_id, said="Dilnozaga yoz: SYNTH new topic"),
        "propose_reply",
        **reply_args(conversation),
    )
    assert result["status"] == "awaiting_approval", result.get("reason")
    never_says_sent(result)
    [card] = await actions_of(denv.env, conversation)
    assert card.text == "SYNTH new topic for Dilnoza"
    assert card.draft_flags is not None
    assert card.draft_flags["needs_reply"] is True
    [request] = draft_requests(denv)
    assert "needs_reply" not in request.instructions
    assert prompt_registry.active("draft_reply").text not in request.instructions


async def test_assist_still_drafts_with_draft_reply_v1_and_its_veto(denv: DraftEnv) -> None:
    """RG-01's path is untouched: Assist (and every non-owner draft) uses
    `draft_reply.v1` with `DraftResult`, and "no reply needed" makes no card."""
    account_id, conversation = await seed_chat(denv)
    denv.script(output(needs_reply=False, skip_reason="no_reply_needed", text=""))
    outcome = await denv.drafter().draft(
        account_id=account_id,
        conversation_id=conversation,
        reason=DraftReason.ASSIST_INGESTION,
    )
    assert outcome.status is DraftStatus.NOT_NEEDED
    assert outcome.owner_result is None
    [request] = draft_requests(denv)
    assert prompt_registry.active("draft_reply").text in request.instructions
    [draft_run] = await runs(denv, "draft_reply")
    assert draft_run.prompt_version == "draft_reply.v1"
    assert await actions_of(denv.env, conversation) == []


# ------------------------------------------------------------ PF-12: modes and targets
async def test_a_new_message_has_no_base_message_and_answers_nothing(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(text="SYNTH standalone hello"))
    result = await call(
        context(denv, account_id, run_id, turn_id, said="Xurshidaga yangi xabar: SYNTH hello"),
        "propose_reply",
        **reply_args(conversation, mode="new_message"),
    )
    assert result["status"] == "awaiting_approval"
    assert result["mode"] == "new_message"
    [card] = await actions_of(denv.env, conversation)
    assert card.base_message_id is None
    assert card.origin == "agent_proposal"
    assert card.draft_flags is not None
    assert (card.draft_flags["mode"], card.draft_flags["target"]) == ("new_message", "none")
    [prompt] = draft_prompts(denv)
    assert "Task: new_message." in prompt
    assert "The message being answered" not in prompt


async def test_a_reply_answers_the_message_the_owner_named(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe", first="SYNTH-EARLIER-ASK")
    await contact_says(denv, "SYNTH-LATER-REMARK")
    earlier = await _first_contact_message(denv, conversation)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(text="SYNTH answer to the earlier one"))
    with capture_logs() as logs:
        result = await call(
            context(denv, account_id, run_id, turn_id),
            "propose_reply",
            **reply_args(conversation, answered=earlier),
        )
    assert result["status"] == "awaiting_approval"
    assert result["answers_message_id"] == earlier
    [card] = await actions_of(denv.env, conversation)
    assert card.base_message_id == earlier  # not the newest contact message
    assert card.draft_flags is not None
    assert card.draft_flags["target"] == "answered"
    [prompt] = draft_prompts(denv)
    assert f"reply to the contact's message [msg:{earlier}]" in prompt
    [line] = [entry for entry in logs if entry.get("tool") == "propose_reply" and "code" in entry]
    assert line["has_answered_target"] is True
    assert line["mode"] == "reply"


async def test_an_answered_id_that_is_not_a_shown_contact_message_is_an_argument_error(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    mine = await message_row_id(denv.env, await owner_says(denv, "SYNTH-OWNER-OWN"))
    run_id, turn_id = await run_and_turn(denv, account_id)
    async with denv.env.factory() as session, session.begin():
        other = Account(telegram_user_id=7_000_000_088, control_chat_id=7_000_000_088)
        session.add(other)
        await session.flush()
        stranger = Contact(account_id=other.id, telegram_user_id=OTHER_CONTACT)
        session.add(stranger)
        await session.flush()
        foreign_chat = Conversation(
            account_id=other.id, contact_id=stranger.id, business_chat_id=OTHER_CONTACT
        )
        session.add(foreign_chat)
        await session.flush()
        foreign = Message(
            account_id=other.id,
            conversation_id=foreign_chat.id,
            telegram_message_id=1,
            direction="contact",
            content_type="text",
            text="SYNTH-FOREIGN",
            sent_at=denv.env.clock.now(),
        )
        session.add(foreign)
        await session.flush()
        foreign_id = foreign.id
    ctx = context(denv, account_id, run_id, turn_id)
    for index, answered in enumerate((foreign_id, mine, 999_999_999)):
        result = await call(
            ctx, "propose_reply", call_id=f"a{index}", **reply_args(conversation, answered=answered)
        )
        assert result["status"] == "error", answered
        assert result["fields"] == ["answered_message_id"]
    both = await call(
        ctx,
        "propose_reply",
        call_id="b",
        **reply_args(conversation, mode="new_message", answered=mine),
    )
    assert both["status"] == "error"
    assert denv.fake.requests == []  # no draft was paid for
    assert await actions_of(denv.env, conversation) == []


async def test_a_foreign_chat_is_not_found_for_the_owner_draft(denv: DraftEnv) -> None:
    account_id, _ = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    result = await call(
        context(denv, account_id, run_id, turn_id),
        "propose_reply",
        **reply_args(987_654_321, mode="new_message"),
    )
    assert result == {"status": "error", "reason": "not found"}
    assert denv.fake.requests == []


# ------------------------------------------------------------ PF-10: word bounds
async def test_a_draft_outside_the_word_range_is_retried_once_then_reported_honestly(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)
    eight = "one two three four five six seven eight"
    nine = "one two three four five six seven eight nine"
    denv.script(owner_output(text=nine), owner_output(text=eight))
    with capture_logs() as logs:
        result = await call(
            context(denv, account_id, run_id, turn_id),
            "propose_reply",
            **reply_args(conversation, min_words=5, max_words=7),
        )
    assert result["status"] == "awaiting_approval"
    assert (result["word_count"], result["asked_length"], result["length_met"]) == (
        8,
        "between 5 and 7 words",
        False,
    )
    [card] = await actions_of(denv.env, conversation)
    assert card.text == eight  # the closer of the two
    first, second = draft_requests(denv)
    retry = "\n".join(i.text for i in second.items if isinstance(i, TextItem))
    assert "That draft has 9 words" in retry
    assert "between 5 and 7 words" in retry
    assert len(second.items) == len(first.items) + 2
    [line] = [entry for entry in logs if entry.get("tool") == "propose_reply" and "code" in entry]
    assert (line["bounds_retry"], line["bounds_met"]) == (True, False)
    async with denv.env.factory() as session:
        [draft_run] = await session.scalars(
            select(AgentRun).where(AgentRun.run_type == "draft_reply")
        )
    assert draft_run.iterations == 2  # one run, two metered calls


async def test_a_draft_inside_the_range_is_kept_without_a_retry(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(text="one two three four five six"))
    result = await call(
        context(denv, account_id, run_id, turn_id),
        "propose_reply",
        **reply_args(conversation, min_words=5, max_words=7),
    )
    assert (result["word_count"], result["length_met"]) == (6, True)
    assert len(draft_requests(denv)) == 1


async def test_a_failed_bounds_retry_keeps_the_first_draft(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    denv.script(owner_output(text="far too many words in this one draft"), StopReason.REFUSAL)
    outcome = await denv.drafter().draft_owner(
        account_id=account_id,
        conversation_id=conversation,
        owner_run_id=1,
        bounds=WordBounds(None, 3),
    )
    assert outcome.status is DraftStatus.DRAFTED
    assert outcome.text == "far too many words in this one draft"
    assert (outcome.word_count, outcome.bounds_retry, outcome.bounds_met) == (8, True, False)


@pytest.mark.parametrize(
    ("words", "rule"),
    [
        ({"min_words": 7, "max_words": 5}, "min_words must not be above max_words"),
        ({"min_words": 0, "max_words": None}, "min_words and max_words are 1 to 1000, or null"),
    ],
)
async def test_bad_word_ranges_are_argument_errors(
    denv: DraftEnv, words: dict[str, Any], rule: str
) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    result = await call(
        context(denv, account_id, run_id, turn_id),
        "propose_reply",
        **(reply_args(conversation) | words),
    )
    assert result["status"] == "error"
    assert any(problem["rule"] == rule for problem in result["problems"])
    assert denv.fake.requests == []


# ------------------------------------------------------------ the revise mode (for B2)
async def test_the_revise_mode_rewrites_a_live_cards_own_text(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(text="SYNTH-CURRENT-CARD-TEXT"))
    created = await call(
        context(denv, account_id, run_id, turn_id), "propose_reply", **reply_args(conversation)
    )
    # draft_owner.v2 (PF-13): a revision also says what the owner's words were.
    denv.script(
        owner_output(text="SYNTH-CURRENT-CARD-TEXT and see you", revision_kind="instruction")
    )
    outcome = await denv.drafter().draft_owner(
        account_id=account_id,
        conversation_id=conversation,
        owner_run_id=run_id,
        mode="revise",
        instruction="SYNTH add: and see you",
        run_tainted=True,
        revise_action_id=created["action_id"],
    )
    assert outcome.status is DraftStatus.DRAFTED
    assert outcome.text == "SYNTH-CURRENT-CARD-TEXT and see you"
    assert outcome.owner_mode == "revise"
    assert outcome.revision_kind == "instruction"
    revise = draft_requests(denv)[-1]
    current = next(
        item
        for item in revise.items
        if isinstance(item, TextItem) and "SYNTH-CURRENT-CARD-TEXT" in item.text
    )
    assert '<untrusted source="draft" trust="untrusted">' in current.text
    assert current.provenance is Provenance.MANAGED
    with pytest.raises(NotInAccountError):  # a chat that is not this account's
        await denv.drafter().draft_owner(
            account_id=account_id,
            conversation_id=conversation + 999,
            owner_run_id=run_id,
            mode="revise",
            revise_action_id=created["action_id"],
        )
    gone = await denv.drafter().draft_owner(
        account_id=account_id,
        conversation_id=conversation,
        owner_run_id=run_id,
        mode="revise",
        revise_action_id=created["action_id"] + 999,
    )
    assert (gone.status, gone.error_class) == (DraftStatus.NOT_DRAFTABLE, "no_live_draft")
    with pytest.raises(ValueError, match="tainted"):
        await denv.drafter().draft_owner(
            account_id=account_id,
            conversation_id=conversation,
            owner_run_id=run_id,
            guidance="SYNTH model words",
            run_tainted=True,
        )


# ------------------------------------------------------------ idempotency
async def test_a_repeated_owner_draft_call_drafts_once(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, _ = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    first = await call(
        context(denv, account_id, run_id, None), "propose_reply", **reply_args(conversation)
    )
    again = await call(
        context(denv, account_id, run_id, None), "propose_reply", **reply_args(conversation)
    )
    assert again["action_id"] == first["action_id"]
    assert again["reused"] is True
    assert "word_count" not in again  # nothing new was drafted
    assert "mode" not in again
    assert len(draft_requests(denv)) == 1


# ------------------------------------------------------------ PF-12: propose_message provenance
async def test_text_in_the_owners_own_words_is_dictated(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    said = "Shuhratga yoz: “Ertaga soat 5 da ko’rishamiz”"
    with capture_logs() as logs:
        result = await call(
            context(denv, account_id, run_id, turn_id, said=said),
            "propose_message",
            **message_args(conversation, "Ertaga soat 5 da ko'rishamiz."),
        )
    assert result["status"] == "awaiting_approval"
    assert result["written_by"] == "owner"
    [card] = await actions_of(denv.env, conversation)
    assert (card.origin, card.text, card.draft_flags) == (
        "owner_dictated",
        "Ertaga soat 5 da ko'rishamiz.",
        None,
    )
    [line] = [entry for entry in logs if entry.get("tool") == "propose_message" and "code" in entry]
    assert line["provenance"] == "owner"


async def test_a_suggestion_the_assistant_made_is_the_assistants_proposal(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    suggestion = "SYNTH Assalomu alaykum, bugun gaplashamizmi?"
    async with denv.env.factory() as session, session.begin():
        session.add(
            ControlTurn(
                account_id=account_id,
                role="assistant",
                text=f"Mana ikki variant:\n1. SYNTH Salom!\n2. «{suggestion}»",
            )
        )
    run_id, turn_id = await run_and_turn(denv, account_id)
    result = await call(
        context(denv, account_id, run_id, turn_id, said="Ikkinchisini yubor"),
        "propose_message",
        **message_args(conversation, suggestion),
    )
    assert result["status"] == "awaiting_approval"
    assert result["written_by"] == "assistant_suggestion"
    [card] = await actions_of(denv.env, conversation)
    assert card.origin == "agent_proposal"  # never "Message you dictated"
    assert card.base_message_id is None
    assert card.draft_flags == {
        "source": "assistant_suggestion",
        "ai_generated": True,
        "target": "none",
    }
    assert card.trigger_turn_id == turn_id
    assert denv.fake.requests == []  # no model run


async def test_a_text_neither_the_owner_nor_the_assistant_wrote_is_refused(
    denv: DraftEnv,
) -> None:
    """ISSUE-007 (S4): a bare command was staged as "Message you dictated"."""
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    with capture_logs() as logs:
        result = await call(
            context(denv, account_id, run_id, turn_id, said="Compose something kind for Rustam"),
            "propose_message",
            **message_args(conversation, "Write a new message"),
        )
    assert result["status"] == "error"
    assert result["fields"] == ["text"]
    [problem] = result["problems"]
    assert "propose_reply with mode 'new_message'" in problem["rule"]
    assert await actions_of(denv.env, conversation) == []
    [line] = [entry for entry in logs if entry.get("tool") == "propose_message" and "code" in entry]
    assert line["code"] == "handler:text_not_dictated"


async def test_an_older_assistant_reply_or_another_accounts_reply_is_no_suggestion(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    old = "SYNTH-OLD-SUGGESTION from long ago"
    async with denv.env.factory() as session, session.begin():
        other = Account(telegram_user_id=7_000_000_099, control_chat_id=7_000_000_099)
        session.add(other)
        await session.flush()
        session.add(ControlTurn(account_id=other.id, role="assistant", text="SYNTH-FOREIGN-TEXT"))
        session.add(ControlTurn(account_id=account_id, role="assistant", text=old))
        for index in range(6):  # six newer replies push it out of reach
            session.add(
                ControlTurn(account_id=account_id, role="assistant", text=f"SYNTH reply {index}")
            )
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id, said="Send that one")
    for index, words in enumerate((old, "SYNTH-FOREIGN-TEXT")):
        result = await call(
            ctx, "propose_message", call_id=f"m{index}", **message_args(conversation, words)
        )
        assert result["status"] == "error", words
    assert await actions_of(denv.env, conversation) == []


async def _first_contact_message(denv: DraftEnv, conversation_id: int) -> int:
    async with denv.env.factory() as session:
        found = await session.scalar(
            select(Message.id)
            .where(Message.conversation_id == conversation_id, Message.direction == "contact")
            .order_by(Message.id)
            .limit(1)
        )
    assert found is not None
    return int(found)
