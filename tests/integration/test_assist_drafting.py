"""S04.2 §3.6, §3.9: the Assist trigger, the `replies.draft` job and its reconciler.

Real Postgres and the real business pipeline with the actions listener; the
model is a scripted `FakeLLM`, cards are recorded (F0's presenter fake), and
the job body runs directly (`AssistDraftJob.run`), so every step is explicit.
Synthetic text only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry, Consent
from assistant.actions.drafts import assist_trigger, request_draft
from assistant.actions.models import Action
from assistant.actions.policy_loader import load_context
from assistant.actions.states import ActionKind, ActionOrigin, ActionState, StateReason
from assistant.agent.drafting import DraftOutcome, DraftStatus, Stale, propose_draft
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.jobs import DraftReason
from assistant.db.uow import transaction
from assistant.jobs.reconcile_actions import UnqueuedDrafts
from assistant.llm.port import LLMRequest, StopReason, TextItem
from assistant.policy import rules
from assistant.policy.context import ActionFacts, CheckResult, Phase
from tests.integration.drafting_env import (
    DraftEnv,
    actions_of,
    contact_deletes,
    contact_says,
    contact_shares_location,
    draft_jobs,
    drafting_environment,
    finish_draft_jobs,
    output,
    pending_flag,
    seed_chat,
)

pytestmark = pytest.mark.integration

DEBOUNCE = timedelta(seconds=21)


@pytest.fixture
async def denv(migrated_url: str) -> AsyncIterator[DraftEnv]:
    async with drafting_environment(migrated_url) as built:
        yield built


async def audits(denv: DraftEnv, event: str) -> list[AuditLogEntry]:
    async with denv.env.factory() as session:
        return list(
            await session.scalars(
                select(AuditLogEntry).where(AuditLogEntry.event == event).order_by(AuditLogEntry.id)
            )
        )


async def set_columns(denv: DraftEnv, conversation_id: int, **values: object) -> None:
    """Write conversation columns directly (bypassing `set_mode`, which would
    clear the flag itself): the job and reconciler must cope on their own."""
    async with denv.env.factory() as session, session.begin():
        await session.execute(
            update(Conversation).where(Conversation.id == conversation_id).values(**values)
        )


async def run_job(denv: DraftEnv, conversation_id: int, reason: str = "assist_ingestion") -> str:
    return await denv.job().run(conversation_id, reason)


def prompt_text(request: LLMRequest) -> str:
    return request.instructions + "\n".join(
        item.text for item in request.items if isinstance(item, TextItem)
    )


# ------------------------------------------------------------ one card per burst
async def test_a_burst_of_contact_messages_produces_one_card(denv: DraftEnv) -> None:
    _, conversation = await seed_chat(denv)
    for words in ("SYNTH-ONE", "SYNTH-TWO", "SYNTH-THREE"):
        await contact_says(denv, words)
    jobs = await draft_jobs(denv.env, conversation)
    assert len(jobs) == 1  # the queueing lock coalesces the burst
    assert jobs[0]["status"] == "todo"
    assert jobs[0]["args"] == {"conversation_id": conversation, "reason": "assist_ingestion"}
    assert jobs[0]["queueing_lock"] == f"draft:{conversation}"
    assert await pending_flag(denv.env, conversation) is not None

    # Still inside the debounce: no model call, nothing proposed.
    assert await run_job(denv, conversation) == "debounced"
    assert denv.fake.requests == []

    denv.env.clock.advance(seconds=DEBOUNCE.total_seconds())
    denv.script(output(text="Synthetic reply to the burst."))
    assert await run_job(denv, conversation) == "proposed_awaiting_approval"
    assert len(denv.fake.requests) == 1
    assert "SYNTH-THREE" in prompt_text(denv.fake.requests[0])
    [card] = await actions_of(denv.env, conversation)
    assert card.state == ActionState.AWAITING_APPROVAL.value
    assert card.origin == ActionOrigin.AGENT_PROPOSAL.value
    assert card.trigger_turn_id is None
    assert card.base_message_id is not None
    assert card.draft_run_id is not None
    assert denv.presenter.presented == [card.id]
    assert await pending_flag(denv.env, conversation) is None


async def test_no_card_for_a_burst_that_needs_no_reply(denv: DraftEnv) -> None:
    _, conversation = await seed_chat(denv)
    await contact_says(denv, "SYNTH-THANKS")
    await contact_says(denv, "SYNTH-OK")
    denv.env.clock.advance(seconds=DEBOUNCE.total_seconds())
    denv.script(output(needs_reply=False, skip_reason="no_reply_needed", text=""))

    assert await run_job(denv, conversation) == "no_reply_needed"
    assert await actions_of(denv.env, conversation) == []
    assert denv.presenter.presented == []
    assert await pending_flag(denv.env, conversation) is None
    [skipped] = await audits(denv, "draft_skipped")
    assert skipped.subject_type == "conversation"
    assert skipped.subject_id == conversation
    assert skipped.details == {"code": "no_reply_needed", "reason": "assist_ingestion"}


async def test_observe_mode_never_drafts_proactively(denv: DraftEnv) -> None:
    _, conversation = await seed_chat(denv, mode="observe")
    await contact_says(denv, "SYNTH-QUESTION?")
    assert await draft_jobs(denv.env, conversation) == []
    assert await pending_flag(denv.env, conversation) is None


async def test_rg02_bot_chats_and_ignore_mode_never_get_a_draft_job(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="ignore")
    await contact_says(denv, "SYNTH-IGNORED")
    assert await draft_jobs(denv.env, conversation) == []
    assert await pending_flag(denv.env, conversation) is None

    # A recognised bot chat is never ingested, so the listener never runs;
    # a flag left on one (by any route) is cleared by the job, with no call.
    async with denv.env.factory() as session, session.begin():
        contact_id = await session.scalar(
            select(Conversation.contact_id).where(Conversation.id == conversation)
        )
        await session.execute(update(Contact).where(Contact.id == contact_id).values(is_bot=True))
    await set_columns(denv, conversation, mode="assist", pending_draft_at=denv.env.clock.now())
    await contact_says(denv, "SYNTH-BOT-SAYS")
    assert await draft_jobs(denv.env, conversation) == []
    denv.env.clock.advance(seconds=DEBOUNCE.total_seconds())
    assert await run_job(denv, conversation) == "bot_chat"
    assert denv.fake.requests == []
    assert await pending_flag(denv.env, conversation) is None
    assert account_id


async def test_a_closed_window_skips_the_draft(denv: DraftEnv) -> None:
    _, conversation = await seed_chat(denv)
    await contact_says(denv)
    await set_columns(
        denv, conversation, last_contact_msg_at=denv.env.clock.now() - timedelta(hours=30)
    )
    denv.env.clock.advance(seconds=DEBOUNCE.total_seconds())

    assert await run_job(denv, conversation) == "window_closed"
    assert denv.fake.requests == []
    assert await actions_of(denv.env, conversation) == []
    assert await pending_flag(denv.env, conversation) is None
    [skipped] = await audits(denv, "draft_skipped")
    assert skipped.details == {"code": "window_closed", "reason": "assist_ingestion"}


# ------------------------------------------------------------ crash and recovery
async def test_crash_after_propose_then_job_retry_leaves_exactly_one_live_card(
    denv: DraftEnv,
) -> None:
    _, conversation = await seed_chat(denv)
    await contact_says(denv)
    denv.env.clock.advance(seconds=DEBOUNCE.total_seconds())
    denv.script(output())
    assert await run_job(denv, conversation) == "proposed_awaiting_approval"

    # The worker died after the commit; procrastinate runs the job again.
    assert await run_job(denv, conversation) == "no_flag"
    # A flag set again with nothing new (e.g. a redelivered update): the
    # live card already saw everything, so no second card.
    await set_columns(denv, conversation, pending_draft_at=denv.env.clock.now())
    assert await run_job(denv, conversation) == "nothing_new"
    assert len(denv.fake.requests) == 1
    live = [a for a in await actions_of(denv.env, conversation) if a.state == "awaiting_approval"]
    assert len(live) == 1
    assert await pending_flag(denv.env, conversation) is None


async def test_a_lost_defer_is_recovered_by_the_reconciler(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    await contact_says(denv)
    reconcile = UnqueuedDrafts(denv.config, denv.config.draft_route_model)

    async def collect() -> list[tuple[str, dict[str, object], str | None]]:
        async with transaction(denv.env.factory) as uow:
            found = await reconcile(uow.session, denv.env.clock.now())
        return [(r.task, dict(r.args), r.queueing_lock) for r in found]

    denv.env.clock.advance(seconds=31)
    assert await collect() == []  # its job is still queued

    await finish_draft_jobs(denv.env, conversation)  # ended without clearing the flag
    assert await collect() == [
        (
            "replies.draft",
            {"conversation_id": conversation, "reason": "assist_ingestion"},
            f"draft:{conversation}",
        )
    ]
    # Without consent the flag waits: nothing is re-deferred.
    async with denv.env.factory() as session, session.begin():
        await session.execute(
            update(Consent)
            .where(Consent.account_id == account_id, Consent.kind == "ai_managed")
            .values(revoked_at=denv.env.clock.now())
        )
    assert await collect() == []
    assert await pending_flag(denv.env, conversation) is not None


# ------------------------------------------------------------ races
async def test_an_assist_draft_created_while_an_owner_requested_card_is_live_marks_it_outdated_superseded(  # noqa: E501
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    await contact_says(denv)
    denv.env.clock.advance(seconds=DEBOUNCE.total_seconds())
    owner_card: list[int] = []

    async def owner_dictates_while_the_model_runs(request: LLMRequest) -> dict[str, object]:
        async with transaction(denv.env.factory) as uow:
            proposal = await propose_draft(
                uow.session,
                account_id=account_id,
                conversation_id=conversation,
                outcome=DraftOutcome(DraftStatus.DRAFTED),
                origin=ActionOrigin.OWNER_DICTATED,
                run_id=None,
                tool_call_id=None,
                trigger_turn_id=None,
                clock=denv.env.clock,
                config=denv.config,
                presenter=denv.presenter,
                dictated_text="SYNTH-DICTATED",
            )
            assert not isinstance(proposal, Stale)
            owner_card.append(proposal.action_id)
        return output(text="Synthetic assist draft.")

    denv.script(owner_dictates_while_the_model_runs)
    assert await run_job(denv, conversation) == "proposed_awaiting_approval"
    first, second = await actions_of(denv.env, conversation)
    assert first.id == owner_card[0]
    assert (first.state, first.state_reason) == ("outdated", StateReason.SUPERSEDED.value)
    assert second.state == "awaiting_approval"
    assert second.origin == "agent_proposal"


async def test_a_contact_message_during_drafting_discards_the_stale_draft_and_redrafts(
    denv: DraftEnv,
) -> None:
    _, conversation = await seed_chat(denv)
    await contact_says(denv, "SYNTH-FIRST-QUESTION")
    denv.env.clock.advance(seconds=DEBOUNCE.total_seconds())

    async def contact_writes_again(request: LLMRequest) -> dict[str, object]:
        await contact_says(denv, "SYNTH-SECOND-QUESTION")
        return output(text="Synthetic answer to the first question only.")

    denv.script(contact_writes_again)
    assert await run_job(denv, conversation) == "stale"
    assert await actions_of(denv.env, conversation) == []  # nothing proposed
    assert await pending_flag(denv.env, conversation) is not None  # kept for the new burst
    assert [j["status"] for j in await draft_jobs(denv.env, conversation)] == ["todo"]

    denv.env.clock.advance(seconds=DEBOUNCE.total_seconds())
    denv.script(output(text="Synthetic answer to both."))
    assert await run_job(denv, conversation) == "proposed_awaiting_approval"
    assert "SYNTH-SECOND-QUESTION" in prompt_text(denv.fake.requests[-1])
    [card] = await actions_of(denv.env, conversation)
    assert card.state == "awaiting_approval"


# ------------------------------------------------------------ a newest message with nothing to show
# PR #106 review (Neryn): the window shows only messages with text or media,
# so a location (or a poll, a contact, …) or a deleted contact message as the
# newest one made every draft `Stale` — the job called the model again every
# debounce, and a re-draft back to back, while no card ever appeared.
async def not_stale_at_approval(denv: DraftEnv, card: Action) -> CheckResult:
    """S04.1's `not_stale`, as Tx A of an Approve would evaluate it now."""
    async with denv.env.factory() as session:
        context = await load_context(
            session,
            account_id=card.account_id,
            conversation_id=card.conversation_id,
            facts=ActionFacts(
                action_id=card.id,
                kind=ActionKind(card.kind),
                origin=ActionOrigin(card.origin),
                state=ActionState.APPROVED,
                version=card.version,
                text_length=len(card.text or ""),
                created_at=card.created_at,
                expires_at=card.expires_at,
                shown_through_message_id=card.shown_through_message_id,
                owner_retry=False,
            ),
            base_message_id=card.base_message_id,
            target_message_id=None,
            owner_requested=card.trigger_turn_id is not None,
            phase=Phase.EXECUTION,
            now=denv.env.clock.now(),
            config=denv.config,
            ai_generated=True,
            process_run_id="synthetic-run",
        )
    result = rules.not_stale(context)
    assert result is not None
    return result


async def newest_message_id(denv: DraftEnv, conversation_id: int) -> int:
    async with denv.env.factory() as session:
        newest = await session.scalar(
            select(func.max(Message.id)).where(Message.conversation_id == conversation_id)
        )
    assert newest is not None
    return int(newest)


async def assert_one_card_from_one_call(denv: DraftEnv, conversation_id: int) -> Action:
    """Exactly one model call and one live card that Approve would not find
    stale; the flag is cleared and nothing is queued to draft again."""
    assert len(denv.fake.requests) == 1
    [card] = await actions_of(denv.env, conversation_id)
    assert card.state == ActionState.AWAITING_APPROVAL.value
    assert card.shown_through_message_id == await newest_message_id(denv, conversation_id)
    assert (await not_stale_at_approval(denv, card)).passed
    assert await pending_flag(denv.env, conversation_id) is None
    todo = [job for job in await draft_jobs(denv.env, conversation_id) if job["status"] == "todo"]
    assert todo == []
    return card


async def test_a_location_as_the_newest_contact_message_gets_one_card_from_one_call(
    denv: DraftEnv,
) -> None:
    _, conversation = await seed_chat(denv)
    await contact_says(denv, "SYNTH-WHERE-SHALL-WE-MEET")
    await contact_shares_location(denv)
    await finish_draft_jobs(denv.env, conversation)  # a worker took the queued job
    denv.env.clock.advance(seconds=DEBOUNCE.total_seconds())
    denv.script(output(text="Synthetic reply about the place."))

    assert await run_job(denv, conversation) == "proposed_awaiting_approval"
    card = await assert_one_card_from_one_call(denv, conversation)
    assert "SYNTH-WHERE-SHALL-WE-MEET" in prompt_text(denv.fake.requests[0])
    assert card.base_message_id is not None
    # Run again (a redelivered job): nothing new, no second call.
    assert await run_job(denv, conversation) == "no_flag"
    assert len(denv.fake.requests) == 1


async def test_a_contact_message_deleted_before_the_run_gets_one_card_from_one_call(
    denv: DraftEnv,
) -> None:
    _, conversation = await seed_chat(denv)
    await contact_says(denv, "SYNTH-QUESTION")
    await contact_deletes(denv)
    await finish_draft_jobs(denv.env, conversation)
    denv.env.clock.advance(seconds=DEBOUNCE.total_seconds())
    denv.script(output(text="Synthetic answer."))

    assert await run_job(denv, conversation) == "proposed_awaiting_approval"
    await assert_one_card_from_one_call(denv, conversation)
    assert "SYNTH-SOON-DELETED" not in prompt_text(denv.fake.requests[0])


async def test_a_redraft_with_a_location_as_the_newest_message_drafts_once(
    denv: DraftEnv,
) -> None:
    _, conversation = await seed_chat(denv, mode="observe")
    await contact_says(denv, "SYNTH-QUESTION")
    await contact_shares_location(denv)
    denv.script(output(text="Synthetic re-draft."))

    assert await run_job(denv, conversation, "redraft") == "proposed_awaiting_approval"
    await assert_one_card_from_one_call(denv, conversation)


# ------------------------------------------------------------ the switch and the flag
async def test_the_ingestion_trigger_is_off_when_the_triage_trigger_is_on(
    migrated_url: str,
) -> None:
    assert assist_trigger({"assist.drafts"}) == "ingestion"
    assert assist_trigger({"assist.drafts", "triage.run", "triage.assist_trigger"}) == "triage"
    assert assist_trigger({"triage.run", "triage.assist_trigger"}) == "off"
    async with drafting_environment(migrated_url, trigger="triage") as denv:
        _, conversation = await seed_chat(denv)
        await contact_says(denv)
        assert await draft_jobs(denv.env, conversation) == []
        assert await pending_flag(denv.env, conversation) is None
        # A queued ingestion job finds the trigger moved: no call, flag cleared.
        await set_columns(denv, conversation, pending_draft_at=denv.env.clock.now())
        denv.env.clock.advance(seconds=DEBOUNCE.total_seconds())
        assert await run_job(denv, conversation) == "trigger_off"
        assert denv.fake.requests == []
        assert await pending_flag(denv.env, conversation) is None


async def test_the_job_clears_the_flag_when_the_chat_left_assist_or_the_window_closed(
    denv: DraftEnv,
) -> None:
    _, conversation = await seed_chat(denv)
    await contact_says(denv)
    denv.env.clock.advance(seconds=DEBOUNCE.total_seconds())
    await set_columns(denv, conversation, mode="observe")
    assert await run_job(denv, conversation) == "not_assist"
    assert await pending_flag(denv.env, conversation) is None

    await set_columns(
        denv,
        conversation,
        mode="assist",
        pending_draft_at=denv.env.clock.now(),
        last_contact_msg_at=denv.env.clock.now() - timedelta(days=2),
    )
    assert await run_job(denv, conversation) == "window_closed"
    assert await pending_flag(denv.env, conversation) is None
    assert denv.fake.requests == []
    codes = [row.details["code"] for row in await audits(denv, "draft_skipped")]
    assert codes == ["not_assist", "window_closed"]


async def test_the_reconciler_clears_a_flag_on_a_chat_that_left_assist(denv: DraftEnv) -> None:
    _, conversation = await seed_chat(denv)
    await contact_says(denv)
    await finish_draft_jobs(denv.env, conversation)
    await set_columns(denv, conversation, mode="observe")
    denv.env.clock.advance(seconds=31)
    async with transaction(denv.env.factory) as uow:
        found = await UnqueuedDrafts(denv.config, denv.config.draft_route_model)(
            uow.session, denv.env.clock.now()
        )
    assert found == []
    assert await pending_flag(denv.env, conversation) is None

    # And every flag while the trigger is off.
    await set_columns(denv, conversation, mode="assist", pending_draft_at=denv.env.clock.now())
    denv.env.clock.advance(seconds=31)
    off = replace(denv.config, assist_draft_trigger="off")
    async with transaction(denv.env.factory) as uow:
        assert (
            await UnqueuedDrafts(off, off.draft_route_model)(uow.session, denv.env.clock.now())
            == []
        )
    assert await pending_flag(denv.env, conversation) is None


async def test_request_draft_for_a_redraft_sets_no_flag_and_defers_in_the_transaction(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")

    async def redraft(session: AsyncSession) -> None:
        await request_draft(
            session,
            account_id=account_id,
            conversation_id=conversation,
            reason=DraftReason.REDRAFT,
            scheduler=denv.scheduler,
            clock=denv.env.clock,
            debounce=denv.config.draft_debounce,
        )

    # Rolled back with the press: the job goes too (it was in the transaction).
    async with denv.env.factory() as session:
        await session.begin()
        await redraft(session)
        visible = await session.scalar(
            text(
                "SELECT count(*) FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = 'replies.draft' AND args->>'reason' = 'redraft'"
            )
        )
        assert visible == 1
        await session.rollback()
    assert await draft_jobs(denv.env, conversation) == []

    # Committed with the press; a second tap while one is queued rolls back
    # only its savepoint and the press transaction still commits.
    async with transaction(denv.env.factory) as uow:
        await redraft(uow.session)
        await redraft(uow.session)
        uow.session.add(
            AuditLogEntry(
                account_id=account_id,
                at=denv.env.clock.now(),
                actor="owner",
                event="synthetic_press",
            )
        )
    jobs = await draft_jobs(denv.env, conversation)
    assert [(j["status"], j["queueing_lock"], j["scheduled_at"]) for j in jobs] == [
        ("todo", f"redraft:{conversation}", None)
    ]
    assert jobs[0]["args"] == {"conversation_id": conversation, "reason": "redraft"}
    assert len(await audits(denv, "synthetic_press")) == 1
    assert await pending_flag(denv.env, conversation) is None

    # The re-draft job runs in Observe (any mode but Ignore), without a flag.
    denv.script(output(text="Synthetic re-draft."))
    assert await run_job(denv, conversation, "redraft") == "proposed_awaiting_approval"
    assert await pending_flag(denv.env, conversation) is None


async def test_draft_flags_are_stored_on_the_action_not_in_the_policy_decision(
    denv: DraftEnv,
) -> None:
    _, conversation = await seed_chat(denv)
    await contact_says(denv)
    denv.env.clock.advance(seconds=DEBOUNCE.total_seconds())
    denv.script(
        output(
            language="uz-Latn",
            confidence="medium",
            sensitive_topic=True,
            makes_commitment=True,
        )
    )
    await run_job(denv, conversation)
    [card] = await actions_of(denv.env, conversation)
    assert card.draft_flags == {
        "needs_reply": True,
        "confidence": "medium",
        "sensitive_topic": True,
        "makes_commitment": True,
        "should_escalate": False,
        "uses_only_permitted_information": True,
        "language": "uz-Latn",
    }
    decision = card.policy_decision or {}
    for flag in ("confidence", "sensitive_topic", "makes_commitment", "language"):
        assert flag not in decision


async def test_a_job_with_an_unknown_reason_does_nothing(denv: DraftEnv) -> None:
    _, conversation = await seed_chat(denv)
    await contact_says(denv)
    for reason in ("owner_request", "eval", "SYNTH-NOT-A-REASON"):
        assert await run_job(denv, conversation, reason) == "bad_reason"
    assert denv.fake.requests == []
    assert await pending_flag(denv.env, conversation) is not None


async def test_refused_or_failed_drafts_clear_the_flag_and_are_audited(denv: DraftEnv) -> None:
    _, conversation = await seed_chat(denv)
    await contact_says(denv)
    denv.env.clock.advance(seconds=DEBOUNCE.total_seconds())
    denv.script(StopReason.REFUSAL)
    assert await run_job(denv, conversation) == "refused"
    assert await pending_flag(denv.env, conversation) is None
    assert await actions_of(denv.env, conversation) == []
    [failed] = await audits(denv, "draft_failed")
    assert failed.details == {"code": "refused", "reason": "assist_ingestion"}
