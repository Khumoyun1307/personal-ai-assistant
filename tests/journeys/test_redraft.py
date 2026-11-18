"""H8 / J07 / J21: Re-draft resumes the card's own request in the main owner model.

The tap queues ids only; after a runtime restart the request's operation,
owner sources, recipient, explicit older target and constraints are reloaded.
Every no-card outcome is told once and the tapped card keeps its choices.
"""

import asyncio
from typing import Any

import pytest
from sqlalchemy import select, text, update

from assistant.accounts.models import Consent
from assistant.actions.request_models import OwnerRequest
from assistant.agent.runtime import TEXT_PROVIDER_DOWN, RunStatus
from assistant.conversations.intent import advance_intent
from assistant.conversations.models import Conversation
from assistant.conversations.modes import set_mode
from assistant.core.jobs import OWNER_REQUEST_TASK
from assistant.db.uow import transaction
from assistant.llm.port import LLMRequest
from tests.fakes.llm import text_response
from tests.journeys.grading import assert_equal
from tests.journeys.harness import Journey, ModelBarrier, call
from tests.journeys.locales import TEXT
from tests.journeys.matrix import owner_input
from tests.journeys.models import (
    LOCALES,
    Contact,
    CountCheck,
    DeleteMessage,
    Fail,
    Locale,
    Restart,
    Revoke,
)
from tests.scenarios.p2_drive import draft_result, run_queued

pytestmark = [pytest.mark.integration, pytest.mark.journey]

#: Newer than the contact page the model would read by default (30).
NEWER_MESSAGES = 35


async def redraft_jobs(j: Journey) -> list[dict[str, Any]]:
    async with j.s.env.factory() as session:
        rows = await session.execute(
            text(
                "SELECT status, args, queueing_lock FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = :task AND args->>'action_id' IS NOT NULL ORDER BY id"
            ),
            {"task": OWNER_REQUEST_TASK},
        )
        return [dict(row._mapping) for row in rows.all()]


async def request_row(j: Journey, request_id: int | None = None) -> OwnerRequest:
    """The focused/newest request row, or a given one (structural columns only)."""
    async with j.s.env.factory() as session:
        statement = select(OwnerRequest).where(OwnerRequest.account_id == j.s.account_id)
        if request_id is not None:
            statement = statement.where(OwnerRequest.id == request_id)
        row = await session.scalar(
            statement.order_by(OwnerRequest.focused.desc(), OwnerRequest.id.desc()).limit(1)
        )
    assert row is not None, f"request {request_id}: expected a row, actual None"
    return row


def proposal_for(j: Journey, request_id: int, body: str) -> Any:
    """`generated_proposal` for a named request (another one may be focused)."""

    async def proposal(_request: LLMRequest) -> Any:
        request = await j.request(request_id)
        fences = [o["context_fence_id"] for o in j.outputs() if "context_fence_id" in o]
        assert fences, "managed read: expected at least one context fence, actual 0"
        return call(
            "propose_owner_message",
            request_id=request.request_id,
            request_version=request.version,
            conversation_id=j.chats["k"],
            mode="new_message",
            answered_message_id=None,
            text=body,
            context_fence_id=fences[-1],
        )

    return proposal


async def older_target_card(j: Journey, locale: Locale) -> int:
    """A reply card answering an explicitly read message outside the newest page."""
    await j.event(Contact(alias="older", text="CANARY-SYNTH older book question?"))
    older = await j.message_id("older")
    for index in range(NEWER_MESSAGES):
        await j.event(Contact(alias=f"later-{index}", text="CANARY-SYNTH newer invoice."))
    await j.owner(
        owner_input("J08", locale)[0],
        j.read(message_id=older),
        j.generated_proposal(TEXT[locale].generated, target=older),
        text_response(TEXT[locale].outcome),
    )
    row = await j.bind_card("k")
    assert row.base_message_id == older, j.outputs()[-1]
    assert (await j.request()).target_message_id == older
    return older


@pytest.mark.parametrize("locale", LOCALES)
async def test_h8_redraft_keeps_the_older_retained_reply_target(
    journey: Journey, locale: Locale
) -> None:
    j = journey
    t = TEXT[locale]
    await j.language(locale)
    older = await older_target_card(j, locale)
    row = await j.bind_card("k")
    request = await j.request()
    await j.event(Contact(alias="newest", text="CANARY-SYNTH newest unrelated question?"))
    newest = await j.message_id("newest")
    warned = await j.bind_card("k")
    assert warned.state == "outdated"
    await j.event(Restart())
    j.s.fake.queue(
        j.read(message_id=older),
        j.generated_proposal(t.revised, target=older),
        text_response(t.revised),
    )
    await j.tap("k", warned.version, "redraft")
    [job] = await redraft_jobs(j)
    # Ids and the reason enum only: never text (H8-1). The tap id keys one
    # attempt per tap; the owner lock is separate from the Assist writer's.
    assert isinstance(job["args"]["tap_id"], int)
    assert job["args"] == {
        "action_id": row.id,
        "request_id": request.request_id,
        "expected_version": warned.version,
        "reason": "redraft",
        "tap_id": job["args"]["tap_id"],
    }
    assert job["queueing_lock"] == f"owner-redraft:{j.chats['k']}"
    assert_equal(await run_queued(j.s, "agent.owner_request"), 1, field="redraft jobs run")
    current = await j.bind_card("k")
    assert current.id != row.id
    assert current.owner_request_id == request.request_id
    assert current.base_message_id == older
    assert current.base_message_id != newest
    assert current.text == t.revised
    assert current.state == "awaiting_approval"
    reloaded = await j.request()
    assert reloaded.request_id == request.request_id
    assert reloaded.operation == "reply"
    assert reloaded.target_message_id == older
    assert reloaded.action_id == current.id
    await j.check(CountCheck(artifact="nested_writers", count=0))
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.parametrize("locale", LOCALES)
async def test_h8_redraft_of_a_deleted_target_is_reported_never_replaced(
    journey: Journey, locale: Locale
) -> None:
    j = journey
    await j.language(locale)
    await older_target_card(j, locale)
    row = await j.bind_card("k")
    await j.event(DeleteMessage(alias="older"))
    warned = await j.bind_card("k")
    assert warned.id == row.id
    assert warned.state == "outdated"
    requests = len(j.s.fake.requests)
    sends = len(j.s.env.sender.delivered_control_sends)
    await j.tap("k", warned.version, "redraft")
    assert_equal(await run_queued(j.s, "agent.owner_request"), 1, field="redraft jobs run")
    # No model call, no new card, one factual notice; the card keeps its choices.
    assert len(j.s.fake.requests) == requests
    assert [r.id for r in await j.rows()] == [row.id]
    notices = [
        item["text"]
        for item in j.s.env.sender.delivered_control_sends[sends:]
        if "could not create a replacement draft" in item["text"]
    ]
    assert len(notices) == 1
    assert "no longer available" in notices[0]
    assert "Approve anyway" in notices[0]
    restored = await j.action("k")
    assert restored.state == "outdated"
    assert restored.approval_token == warned.approval_token
    assert restored.text == warned.text
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("failure", ["provider", "control_consent"])
async def test_h8_redraft_no_card_outcomes_are_told_once(
    journey: Journey, locale: Locale, failure: str
) -> None:
    j = journey
    t = TEXT[locale]
    await j.language(locale)
    brief = owner_input("J04", locale)[0]
    await j.owner(brief, j.read(), j.generated_proposal(t.generated), text_response(t.generated))
    row = await j.bind_card("k")
    first = await j.request()
    await j.event(Contact(alias="new", text="CANARY-SYNTH newer question."))
    warned = await j.bind_card("k")
    # Another unfinished task is waiting on its question meanwhile.
    await j.owner(t.empty, text_response(t.content_question))
    other = await request_row(j)
    assert other.id != first.request_id
    assert other.state == "collecting"
    assert other.focused
    before = await request_row(j, first.request_id)
    if failure == "provider":
        await j.event(Fail(stage="model", outcome="definite"))
    else:
        await j.event(Revoke(scope="ai_control"))
    requests = len(j.s.fake.requests)
    sends = len(j.s.env.sender.delivered_control_sends)
    await j.tap("k", warned.version, "redraft")
    [job] = await redraft_jobs(j)
    assert_equal(await run_queued(j.s, "agent.owner_request"), 1, field="redraft jobs run")
    delivered = [item["text"] for item in j.s.env.sender.delivered_control_sends[sends:]]
    if failure == "provider":
        assert len(j.s.fake.requests) == requests + 1
        assert sum(TEXT_PROVIDER_DOWN in item for item in delivered) == 1
    else:
        assert len(j.s.fake.requests) == requests
        notices = [item for item in delivered if "could not create a replacement" in item]
        assert len(notices) == 1
        assert "AI permission is unavailable" in notices[0]
    assert [r.id for r in await j.rows()] == [row.id]
    restored = await j.action("k")
    assert restored.state == "outdated"
    assert restored.approval_token == warned.approval_token
    # No card, so no request change: neither this task nor the other one's
    # focus and question moved (C1).
    after = await request_row(j, first.request_id)
    assert (after.state, after.version, after.focused, after.expires_at) == (
        before.state,
        before.version,
        before.focused,
        before.expires_at,
    )
    kept = await request_row(j, other.id)
    assert (kept.state, kept.focused, kept.missing_slot, kept.question_input_id) == (
        "collecting",
        True,
        other.missing_slot,
        other.question_input_id,
    )
    # A retried job of the same tap (stalled worker) finds it answered.
    outcome = await j.s.handlers.owner_request(**job["args"])
    assert outcome.status is RunStatus.SKIPPED
    assert outcome.note == "already_answered"
    assert len(j.s.env.sender.delivered_control_sends) == sends + len(delivered)
    # A new tap after the cause is fixed is a real second attempt.
    if failure == "control_consent":
        async with transaction(j.s.env.factory) as uow:
            await uow.session.execute(
                update(Consent)
                .where(Consent.account_id == j.s.account_id, Consent.kind == "ai_control")
                .values(revoked_at=None)
            )
    j.s.fake.queue(j.read(), proposal_for(j, first.request_id, t.revised), text_response(t.revised))
    await j.tap("k", warned.version, "redraft")
    assert_equal(await run_queued(j.s, "agent.owner_request"), 1, field="second tap runs")
    current = await j.bind_card("k")
    assert current.id != row.id
    assert current.text == t.revised
    assert current.owner_request_id == first.request_id
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.parametrize("locale", LOCALES)
async def test_h8_double_tap_retry_and_stale_token_stage_one_replacement(
    journey: Journey, locale: Locale
) -> None:
    j = journey
    t = TEXT[locale]
    await j.language(locale)
    brief = owner_input("J04", locale)[0]
    await j.owner(brief, j.read(), j.generated_proposal(t.generated), text_response(t.generated))
    row = await j.bind_card("k")
    old_token = row.approval_token
    await j.event(Contact(alias="new", text="CANARY-SYNTH newer question."))
    warned = await j.bind_card("k")
    assert warned.approval_token != old_token
    # The pre-warning controls are stale: they queue nothing.
    j.tokens[("stale", row.version)] = str(old_token)
    j.cards["stale"] = row.id
    await j.tap("stale", row.version, "redraft")
    assert await redraft_jobs(j) == []
    await j.tap("k", warned.version, "redraft")
    await j.tap("k", warned.version, "redraft")
    assert len(await redraft_jobs(j)) == 1
    [job] = await redraft_jobs(j)
    j.s.fake.queue(j.read(), j.generated_proposal(t.revised), text_response(t.revised))
    assert_equal(await run_queued(j.s, "agent.owner_request"), 1, field="redraft jobs run")
    request = await j.request()
    outcome = await j.s.handlers.owner_request(**job["args"])
    assert outcome.status is RunStatus.SKIPPED
    rows = await j.rows()
    assert len(rows) == 2
    assert rows[0].id == row.id
    assert rows[1].text == t.revised
    assert rows[1].owner_request_id == request.request_id
    assert request.action_id == rows[1].id
    # The replaced card offers no Re-draft; a late tap is told, never revived.
    replaced = await j.s.action(row.id)
    assert replaced.state_reason == "superseded"
    assert "Re-draft" not in [b["text"] for b in j.s.card_buttons(replaced)]
    sends = len(j.s.env.sender.delivered_control_sends)
    late = await j.s.handlers.owner_request(**{**job["args"], "tap_id": job["args"]["tap_id"] + 1})
    assert late.note == "already_replaced"
    delivered = [item["text"] for item in j.s.env.sender.delivered_control_sends[sends:]]
    assert sum("newer draft already replaced" in item for item in delivered) == 1
    assert len(await j.rows()) == 2
    await j.check(CountCheck(artifact="nested_writers", count=0))
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.parametrize("locale", LOCALES)
async def test_h8_closed_window_estimate_does_not_pre_veto_owner_redraft(
    journey: Journey, locale: Locale
) -> None:
    """P2-O20: the replacement is approvable; Telegram's refusal is not sent."""
    j = journey
    t = TEXT[locale]
    await j.language(locale)
    await j.event(Contact(alias="base", text="CANARY-SYNTH book question?"))
    j.s.env.clock.advance(seconds=23 * 3600 + 30 * 60)
    brief = owner_input("J04", locale)[0]
    await j.owner(brief, j.read(), j.generated_proposal(t.generated), text_response(t.generated))
    row = await j.bind_card("k")
    assert row.state == "awaiting_approval"
    # The contact's last message is now older than the estimated 24 h window.
    j.s.env.clock.advance(seconds=3600)
    j.s.fake.queue(j.read(), j.generated_proposal(t.revised), text_response(t.revised))
    await j.tap("k", row.version, "redraft")
    assert_equal(await run_queued(j.s, "agent.owner_request"), 1, field="redraft jobs run")
    current = await j.bind_card("k")
    assert current.id != row.id
    assert current.text == t.revised
    assert current.state == "awaiting_approval"
    assert (await j.s.action(row.id)).state == "outdated"
    await j.event(Fail(stage="send", outcome="definite"))
    await j.tap("k", current.version, "approve")
    await run_queued(j.s, "actions.execute")
    final = await j.action("k")
    assert final.state == "failed"
    await j.check(CountCheck(artifact="business_attempts", count=1))


@pytest.mark.parametrize("locale", LOCALES)
async def test_h8_second_proposal_outside_a_redraft_is_a_conflict(
    journey: Journey, locale: Locale
) -> None:
    """AGENTS §8: without a Re-draft tap, an existing card is never replaced."""
    j = journey
    t = TEXT[locale]
    await j.language(locale)
    brief = owner_input("J04", locale)[0]
    await j.owner(
        brief,
        j.read(),
        j.generated_proposal(t.generated),
        j.generated_proposal(t.revised),
        text_response(t.generated),
    )
    rows = await j.rows()
    assert len(rows) == 1
    assert rows[0].text == t.generated
    conflict = j.outputs()[-1]
    assert conflict["proposal_status"] == "proposal_conflict"
    assert conflict["requested_change_applied"] is False
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.parametrize("locale", LOCALES)
async def test_h8_approval_racing_the_redraft_keeps_the_approved_card(
    journey: Journey, locale: Locale
) -> None:
    """`replaced_changed`: an Approve anyway that wins the race is never undone."""
    j = journey
    t = TEXT[locale]
    await j.language(locale)
    brief = owner_input("J04", locale)[0]
    await j.owner(brief, j.read(), j.generated_proposal(t.generated), text_response(t.generated))
    row = await j.bind_card("k")
    await j.event(Contact(alias="new", text="CANARY-SYNTH newer question."))
    warned = await j.bind_card("k")
    barrier = ModelBarrier()
    proposal = j.generated_proposal(t.revised)

    async def held(request: LLMRequest) -> Any:
        barrier.entered.set()
        await barrier.release.wait()
        assert callable(proposal)
        return await proposal(request)

    j.s.fake.queue(j.read(), held, text_response(t.revised))
    await j.tap("k", warned.version, "redraft")
    task = asyncio.create_task(run_queued(j.s, "agent.owner_request"))
    try:
        await j.hold(task, barrier)
        await j.tap("k", warned.version, "approve")
    finally:
        barrier.release.set()
        await asyncio.gather(task, return_exceptions=True)
    await task
    assert [r.id for r in await j.rows()] == [row.id]
    assert j.outputs()[-1]["reason"] == "replaced_changed"
    await run_queued(j.s, "actions.execute")
    sent = await j.action("k")
    assert sent.id == row.id
    assert sent.text == t.generated
    await j.check(CountCheck(artifact="business_attempts", count=1))


@pytest.mark.parametrize("locale", LOCALES)
async def test_h8_h11_earlier_intent_change_does_not_warn_the_owner_redraft(
    journey: Journey, locale: Locale
) -> None:
    """The owner's own tap records the chat's current intent on its request."""
    j = journey
    t = TEXT[locale]
    await j.language(locale)
    brief = owner_input("J04", locale)[0]
    await j.owner(brief, j.read(), j.generated_proposal(t.generated), text_response(t.generated))
    row = await j.bind_card("k")
    await j.event(Contact(alias="new", text="CANARY-SYNTH newer question."))
    warned = await j.bind_card("k")
    # Another explicit owner operation in this chat advanced its intent since
    # the request was selected (for example, rejecting an Assist card).
    async with transaction(j.s.env.factory) as uow:
        await uow.session.execute(
            select(Conversation.id).where(Conversation.id == j.chats["k"]).with_for_update()
        )
        await advance_intent(uow.session, account_id=j.s.account_id, conversation_id=j.chats["k"])
    j.s.fake.queue(j.read(), j.generated_proposal(t.revised), text_response(t.revised))
    await j.tap("k", warned.version, "redraft")
    assert_equal(await run_queued(j.s, "agent.owner_request"), 1, field="redraft jobs run")
    current = await j.bind_card("k")
    assert current.id != row.id
    assert current.state == "awaiting_approval"
    assert current.freshness_warning is None
    await j.check(CountCheck(artifact="business_attempts", count=0))


@pytest.mark.parametrize("locale", LOCALES)
async def test_h8_h11_assist_draft_in_flight_loses_to_the_owner_redraft(
    journey: Journey, locale: Locale
) -> None:
    j = journey
    t = TEXT[locale]
    await j.language(locale)
    async with transaction(j.s.env.factory) as uow:
        await set_mode(
            uow.session,
            account_id=j.s.account_id,
            conversation_id=j.chats["k"],
            mode="assist",
            actor="owner",
            clock=j.s.env.clock,
        )
    brief = owner_input("J04", locale)[0]
    await j.owner(brief, j.read(), j.generated_proposal(t.generated), text_response(t.generated))
    row = await j.bind_card("k")
    await j.event(Contact(alias="burst", text="CANARY-SYNTH Assist question?"))
    warned = await j.bind_card("k")
    j.s.env.clock.advance(seconds=30)
    barrier = ModelBarrier()

    async def held(_request: LLMRequest) -> dict[str, Any]:
        barrier.entered.set()
        await barrier.release.wait()
        return draft_result(text="CANARY-SYNTH obsolete Assist reply.")

    j.s.fake.parsed.append(held)
    task = asyncio.create_task(run_queued(j.s, "replies.draft"))
    try:
        await j.hold(task, barrier)
        j.s.fake.queue(j.read(), j.generated_proposal(t.revised), text_response(t.revised))
        await j.tap("k", warned.version, "redraft")
        assert_equal(await run_queued(j.s, "agent.owner_request"), 1, field="redraft jobs run")
    finally:
        barrier.release.set()
        await asyncio.gather(task, return_exceptions=True)
    await task
    rows = await j.rows()
    assert len(rows) == 2, [(r.id, r.state, r.text) for r in rows]
    assert rows[0].id == row.id
    assert rows[1].text == t.revised
    assert rows[1].state == "awaiting_approval"
    assert rows[1].freshness_warning is None
    await j.check(CountCheck(artifact="business_attempts", count=0))
