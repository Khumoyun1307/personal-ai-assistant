"""I9: preserve every triage urgency through delivery policy."""

from dataclasses import replace
from datetime import timedelta

import pytest

from assistant.inbox.notification_policy import FeedbackMark, Verdict, muted_pairs
from assistant.inbox.notification_types import NotificationKind, Priority
from assistant.inbox.triage_store import TriageVerdict, verdict_alerts
from tests.fakes.clock import FakeClock
from tests.unit.test_notification_policy import (
    AFTERNOON,
    FACTS,
    LATE_EVENING,
    LOAD,
    QUIET_ENDS,
    SETTINGS,
    decide,
)


def triage_verdict(urgency: str) -> TriageVerdict:
    return TriageVerdict(
        account_id=1,
        conversation_id=1,
        run_id=1,
        prompt_version="synthetic",
        model="synthetic",
        source={},
        task_revisions={},
        covers_through_message_id=1,
        burst_message_ids=(1,),
        needs_reply=True,
        contains_question=True,
        requests_decision=False,
        urgency=urgency,
        deadline_at=None,
        escalation_category="needs_owner",
        reasons=(),
        language="en",
        script="latin",
        tasks=(),
        resolved_task_ids=(),
    )


@pytest.mark.parametrize("urgency", ["low", "normal", "high", "urgent"])
def test_every_triage_urgency_keeps_its_delivery_semantics(urgency: str) -> None:
    [alert] = verdict_alerts(triage_verdict(urgency))
    assert alert.priority == Priority(urgency)
    facts = replace(FACTS, kind=alert.kind, priority=alert.priority)
    clock = FakeClock(AFTERNOON)
    important = urgency in ("high", "urgent")
    expected = Verdict.IMMEDIATE if important else Verdict.BATCH
    assert decide(clock.now(), facts).verdict == expected
    clock.set(LATE_EVENING)
    held = decide(clock.now(), facts)
    assert held.verdict == (Verdict.DEFER if important else Verdict.BATCH)
    assert held.deliver_after == (QUIET_ENDS if important else None)
    clock.set(AFTERNOON)
    retry_at = clock.now() + timedelta(minutes=10)
    capped = replace(LOAD, immediate_last_hour=SETTINGS.cap_per_hour, cap_retry_at=retry_at)
    result = decide(clock.now(), facts, load=capped)
    assert result.verdict == (Verdict.DEFER if important else Verdict.BATCH)
    assert result.deliver_after == (retry_at if important else None)


@pytest.mark.parametrize("urgency", ["low", "normal", "high", "urgent"])
def test_decision_and_suspicious_priority_floors_preserve_high_urgency(urgency: str) -> None:
    decision = replace(triage_verdict(urgency), requests_decision=True)
    [alert] = verdict_alerts(decision)
    assert alert.priority == (Priority.URGENT if urgency == "urgent" else Priority.HIGH)
    suspicious = replace(triage_verdict(urgency), escalation_category="suspicious")
    alerts = verdict_alerts(suspicious)
    assert alerts[-1].kind == NotificationKind.TRIAGE_SUSPICIOUS
    expected = Priority(urgency) if urgency in {"high", "urgent"} else Priority.NORMAL
    assert alerts[-1].priority == expected


def test_high_fyi_does_not_claim_that_a_reply_is_needed() -> None:
    verdict = replace(triage_verdict("high"), needs_reply=False, escalation_category="none")
    assert verdict_alerts(verdict) == ()


def test_monitor_feedback_only_mutes_the_same_monitor() -> None:
    marks = [
        FeedbackMark("monitor_match", FACTS.contact_id, "not_useful", AFTERNOON, monitor_id=11),
        FeedbackMark("monitor_match", FACTS.contact_id, "not_useful", AFTERNOON, monitor_id=11),
    ]
    settings = replace(SETTINGS, muted=muted_pairs(marks, AFTERNOON))
    monitor = replace(FACTS, kind=NotificationKind.MONITOR_MATCH, monitor_id=11)
    assert decide(AFTERNOON, monitor, settings).reason == "feedback"
    assert decide(AFTERNOON, replace(monitor, monitor_id=12), settings).verdict == Verdict.IMMEDIATE
    legacy = replace(monitor, monitor_id=None)
    assert decide(AFTERNOON, legacy, settings).verdict == Verdict.IMMEDIATE
    urgent = replace(monitor, priority=Priority.URGENT)
    assert decide(AFTERNOON, urgent, settings).verdict == Verdict.IMMEDIATE


def test_legacy_unscoped_monitor_feedback_does_not_mute_new_monitors() -> None:
    mark = FeedbackMark("monitor_match", FACTS.contact_id, "not_useful", AFTERNOON)
    assert muted_pairs([mark, mark], AFTERNOON) == frozenset()
