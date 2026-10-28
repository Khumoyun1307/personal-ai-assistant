"""Triage journeys: burst coalescing, open loops, isolation and consent."""

import pytest

from assistant.accounts.models import AuditLogEntry
from assistant.actions.models import Action
from assistant.conversations.modes import set_mode
from assistant.db.uow import transaction
from assistant.inbox.models import ConversationTriage, Notification, Task
from tests.integration.agent_env import request_texts
from tests.integration.business_env import CONTACT
from tests.scenarios.m05_drive import M05Scenario, advance, run_queued, triage, triage_result

pytestmark = pytest.mark.integration


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S04.2b/S04.3/S05.1 (CP4)")
async def test_with_the_triage_trigger_a_burst_still_produces_one_card(m05: M05Scenario) -> None:
    from assistant.conversations.modes import SETTABLE_MODES

    assert "assist" in SETTABLE_MODES, "awaiting S04.2b and CP4 wiring"
    async with transaction(m05.env.factory) as uow:
        await set_mode(
            uow.session,
            account_id=m05.account_id,
            conversation_id=m05.conversation_id,
            mode="assist",
            actor="owner",
            clock=m05.env.clock,
        )
    for number in range(102, 105):
        await m05.message(number)
        await advance(m05, 1)
    from tests.fakes.llm import FakeLLM

    assert isinstance(m05.fake, FakeLLM)
    m05.fake.parsed.extend(
        [
            triage_result(needs_reply=True),
            {
                "language": "en",
                "needs_reply": True,
                "skip_reason": None,
                "revision_kind": "new",
                "text": "Synthetic proposed reply.",
                "confidence": "medium",
                "uses_only_permitted_information": True,
                "sensitive_topic": False,
                "makes_commitment": False,
                "should_escalate": False,
                "escalation_reason": None,
            },
        ]
    )
    await advance(m05, m05.settings.triage_debounce_seconds + 1)
    assert await run_queued(m05, "inbox.triage") == 1
    await advance(m05, m05.settings.draft_debounce_seconds + 1)
    assert await run_queued(m05, "replies.draft") == 1
    [row] = await m05.rows(Action)
    assert row.state == "awaiting_approval"
    assert row.card_message_id is not None
    assert sum(r.purpose == "triage" for r in m05.fake.requests) == 1
    assert sum(r.purpose == "draft_reply" for r in m05.fake.requests) == 1


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S05.1")
async def test_an_urgent_decision_request_alerts_the_owner(m05: M05Scenario) -> None:
    await m05.preferences(quiet_hours="off")
    await m05.message(102, "Synthetic urgent decision needed.")
    await triage(
        m05,
        triage_result(
            needs_reply=True,
            requests_decision=True,
            urgency="urgent",
            escalation_category="urgent_decision",
            reasons=["Synthetic urgent decision."],
        ),
    )
    await run_queued(m05, "notify.deliver")
    [row] = await m05.rows(Notification)
    assert (row.kind, row.priority, row.status) == ("triage_urgent", "urgent", "sent")
    assert "urgent" in m05.output().lower()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S05.1")
async def test_a_repeated_promise_creates_one_task(m05: M05Scenario) -> None:
    first = await m05.message(102, "Synthetic promise: I will send the sample.")
    promise = {
        "by": "contact",
        "text": "Send the synthetic sample",
        "due": None,
        "source_message_id": first,
        "existing_task_id": None,
    }
    await triage(m05, triage_result(commitments=[promise]))
    [original] = await m05.rows(Task)
    second = await m05.message(103, "Synthetic repeated promise: the same sample.")
    await triage(
        m05,
        triage_result(
            commitments=[{**promise, "source_message_id": second, "existing_task_id": original.id}]
        ),
    )
    [row] = await m05.rows(Task)
    assert row.id == original.id
    assert row.status == "open"
    assert set(row.source_message_ids) == {first, second}
    assert sum(e.event == "task_created" for e in await m05.rows(AuditLogEntry)) == 1


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S05.1")
async def test_the_owners_manual_answer_closes_the_open_question(m05: M05Scenario) -> None:
    source = await m05.message(102, "Synthetic question: which sample should I use?")
    await triage(
        m05,
        triage_result(
            open_questions=[
                {
                    "text": "Choose the synthetic sample",
                    "source_message_id": source,
                    "is_decision": False,
                }
            ]
        ),
    )
    [task] = await m05.rows(Task)
    await m05.message(103, "Use synthetic sample A.", owner=True)
    await triage(m05, triage_result(resolved_task_ids=[task.id]))
    [closed] = await m05.rows(Task)
    assert (closed.status, closed.closed_reason) == ("done", "resolved_by_triage")
    assert closed.closed_at is not None
    events = [
        e
        for e in await m05.rows(AuditLogEntry)
        if e.event == "task_closed" and e.subject_id == task.id
    ]
    assert len(events) == 1
    alerts = [n for n in await m05.rows(Notification) if n.kind == "task_closed"]
    assert len(alerts) == 1
    assert alerts[0].refs["task_ids"] == [task.id]


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S05.1")
async def test_triage_runs_have_no_tools_and_see_one_conversation(m05: M05Scenario) -> None:
    m05.require("inbox.triage")
    await m05.message(102, "P2-QA-CONVERSATION-A-CANARY")
    await m05.message(202, "P2-QA-CONVERSATION-B-CANARY", chat=CONTACT + 1)
    m05.fake.parsed.extend([triage_result(), triage_result()])
    await advance(m05, m05.settings.triage_debounce_seconds + 1)
    assert await run_queued(m05, "inbox.triage") == 2
    calls = [r for r in m05.fake.requests if r.purpose == "triage"]
    assert len(calls) == 2
    for request in calls:
        assert not request.tools
        rendered = request_texts(request)
        assert ("P2-QA-CONVERSATION-A-CANARY" in rendered) != (
            "P2-QA-CONVERSATION-B-CANARY" in rendered
        )
    assert len(await m05.rows(ConversationTriage)) == 2


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S05.1")
async def test_revoked_consent_stops_a_queued_triage_run(m05: M05Scenario) -> None:
    m05.require("inbox.triage")
    await m05.message(102)
    await m05.revoke()
    await advance(m05, m05.settings.triage_debounce_seconds + 1)
    assert await run_queued(m05, "inbox.triage") == 1
    assert m05.fake.requests == []
    assert await m05.rows(Task) == []
    assert await m05.rows(Notification) == []
    assert await m05.rows(ConversationTriage) == []
