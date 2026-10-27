"""The notification delivery policy (S05.2 §3.1; PRD §6.7, §14, FR-TRI-07).

Times are fixed instants built from the owner's local wall clock, so each
case reads the way the owner would describe it ("an urgent alert at 23:30").
Asia/Tashkent has no daylight saving time; Europe/Berlin covers the DST edges.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from assistant.inbox.notification_policy import (
    MUTE_THRESHOLD,
    MUTE_WINDOW,
    DeliveryDecision,
    FeedbackMark,
    NotificationFacts,
    OwnerAlertSettings,
    RecentLoad,
    Verdict,
    decide_delivery,
    effective_priority,
    muted_pairs,
)
from assistant.inbox.notification_types import AlertClass, NotificationKind, Priority

TASHKENT = "Asia/Tashkent"
BERLIN = "Europe/Berlin"
CONTACT = 7
OTHER_CONTACT = 8


def local(
    month: int, day: int, hour: int, minute: int = 0, zone: str = TASHKENT, fold: int = 0
) -> datetime:
    """A UTC instant from the owner's wall clock in `zone`."""
    wall = datetime(2026, month, day, hour, minute, tzinfo=ZoneInfo(zone), fold=fold)
    return wall.astimezone(UTC)


AFTERNOON = local(10, 27, 14)
LATE_EVENING = local(10, 27, 23, 30)
SMALL_HOURS = local(10, 28, 3)
QUIET_ENDS = local(10, 28, 8)

FACTS = NotificationFacts(
    kind=NotificationKind.TRIAGE_DECISION,
    alert_class=AlertClass.STANDARD,
    priority=Priority.HIGH,
    contact_id=CONTACT,
    contact_priority=None,
    bypass_quiet_hours=False,
)
SETTINGS = OwnerAlertSettings(
    timezone=TASHKENT,
    quiet_hours=(time(23), time(8)),  # PRD Q-07 default
    urgent_bypass=False,
    cap_per_hour=6,
    muted=frozenset(),
)
LOAD = RecentLoad(immediate_last_hour=0, suspicious_from_contact_24h=0)

IMMEDIATE = DeliveryDecision(Verdict.IMMEDIATE, None, "immediate")


def decide(
    now: datetime,
    facts: NotificationFacts = FACTS,
    settings: OwnerAlertSettings = SETTINGS,
    load: RecentLoad = LOAD,
) -> DeliveryDecision:
    return decide_delivery(facts, settings, load, now)


def batch(reason: str) -> DeliveryDecision:
    return DeliveryDecision(Verdict.BATCH, None, reason)


def mark(feedback: str, days_ago: float, *, kind: str = "triage_decision") -> FeedbackMark:
    return FeedbackMark(
        kind=kind,
        contact_id=CONTACT,
        feedback=feedback,
        marked_at=AFTERNOON - timedelta(days=days_ago),
    )


# --- rule 1: system ----------------------------------------------------------


@pytest.mark.parametrize("now", [AFTERNOON, LATE_EVENING, SMALL_HOURS])
def test_system_alerts_are_always_immediate(now: datetime) -> None:
    system = replace(
        FACTS,
        kind=NotificationKind.SYSTEM,
        alert_class=AlertClass.SYSTEM,
        priority=Priority.LOW,
        contact_priority="low",
    )
    hostile = replace(SETTINGS, cap_per_hour=0, muted=frozenset({("system", None)}))
    overloaded = RecentLoad(immediate_last_hour=100, suspicious_from_contact_24h=0)

    assert decide(now, system, hostile, overloaded) == DeliveryDecision(
        Verdict.IMMEDIATE, None, "system"
    )


# --- rule 2: effective priority ----------------------------------------------


@pytest.mark.parametrize(
    ("priority", "contact_priority", "expected"),
    [
        (Priority.LOW, "vip", Priority.NORMAL),
        (Priority.NORMAL, "vip", Priority.HIGH),
        (Priority.HIGH, "vip", Priority.URGENT),
        (Priority.URGENT, "vip", Priority.URGENT),
        (Priority.URGENT, "low", Priority.HIGH),
        (Priority.HIGH, "low", Priority.NORMAL),
        (Priority.NORMAL, "low", Priority.LOW),
        (Priority.LOW, "low", Priority.LOW),
        (Priority.HIGH, "normal", Priority.HIGH),
        (Priority.HIGH, None, Priority.HIGH),
    ],
)
def test_vip_raises_and_low_lowers_priority_one_step(
    priority: Priority, contact_priority: str | None, expected: Priority
) -> None:
    facts = replace(FACTS, priority=priority, contact_priority=contact_priority)
    assert effective_priority(facts, LOAD) == expected


def test_the_contact_priority_changes_the_verdict() -> None:
    vip_normal = replace(FACTS, priority=Priority.NORMAL, contact_priority="vip")
    low_high = replace(FACTS, priority=Priority.HIGH, contact_priority="low")

    assert decide(AFTERNOON, vip_normal) == IMMEDIATE
    assert decide(AFTERNOON, low_high) == batch("quiet_by_default")


def test_a_low_priority_contact_does_not_demote_a_monitor_alert() -> None:
    monitor = replace(
        FACTS, kind=NotificationKind.MONITOR_MATCH, priority=Priority.HIGH, contact_priority="low"
    )
    triage = replace(monitor, kind=NotificationKind.TRIAGE_DECISION)

    assert effective_priority(monitor, LOAD) == Priority.HIGH
    assert decide(AFTERNOON, monitor) == IMMEDIATE
    assert decide(AFTERNOON, triage) == batch("quiet_by_default")


def test_a_vip_contact_still_raises_a_monitor_alert() -> None:
    monitor = replace(
        FACTS, kind=NotificationKind.MONITOR_MATCH, priority=Priority.NORMAL, contact_priority="vip"
    )
    assert effective_priority(monitor, LOAD) == Priority.HIGH


def test_a_repeated_suspicious_alert_is_raised() -> None:
    suspicious = replace(FACTS, kind=NotificationKind.TRIAGE_SUSPICIOUS, priority=Priority.NORMAL)
    first = LOAD
    repeat = RecentLoad(immediate_last_hour=0, suspicious_from_contact_24h=1)

    assert decide(AFTERNOON, suspicious, load=first) == batch("quiet_by_default")
    assert effective_priority(suspicious, repeat) == Priority.HIGH
    assert decide(AFTERNOON, suspicious, load=repeat) == IMMEDIATE
    # The floor applies after a low contact's demotion, and never lowers.
    demoted = replace(suspicious, contact_priority="low")
    assert effective_priority(demoted, repeat) == Priority.HIGH
    urgent = replace(suspicious, priority=Priority.URGENT)
    assert effective_priority(urgent, repeat) == Priority.URGENT
    # Only the suspicious kind is raised by a repeat.
    other = replace(suspicious, kind=NotificationKind.TRIAGE_NEEDS_REPLY)
    assert effective_priority(other, repeat) == Priority.NORMAL


# --- rule 3: feedback mute ---------------------------------------------------


def test_two_not_useful_marks_mute_that_kind_for_that_contact() -> None:
    one = muted_pairs([mark("not_useful", 3)], AFTERNOON)
    two = muted_pairs([mark("not_useful", 3), mark("not_useful", 1)], AFTERNOON)

    assert MUTE_THRESHOLD == 2
    assert one == frozenset()
    assert two == frozenset({("triage_decision", CONTACT)})

    settings = replace(SETTINGS, muted=two)
    assert decide(AFTERNOON, settings=settings) == batch("feedback")
    other_contact = replace(FACTS, contact_id=OTHER_CONTACT)
    assert decide(AFTERNOON, other_contact, settings) == IMMEDIATE
    other_kind = replace(FACTS, kind=NotificationKind.TRIAGE_NEEDS_REPLY)
    assert decide(AFTERNOON, other_kind, settings) == IMMEDIATE


def test_a_muted_kind_still_delivers_when_urgent() -> None:
    settings = replace(SETTINGS, muted=frozenset({("triage_decision", CONTACT)}))
    urgent = replace(FACTS, priority=Priority.URGENT)
    vip_high = replace(FACTS, contact_priority="vip")

    assert decide(AFTERNOON, urgent, settings) == IMMEDIATE
    assert decide(AFTERNOON, vip_high, settings) == IMMEDIATE


def test_a_mute_without_a_contact_mutes_only_alerts_without_a_contact() -> None:
    """`(kind, None)` comes from feedback on alerts that name no contact; it
    must not silence that kind for every contact (Jack's reading of §3.1,
    recorded for the TL)."""
    settings = replace(SETTINGS, muted=frozenset({("triage_decision", None)}))

    assert decide(AFTERNOON, settings=settings) == IMMEDIATE
    no_contact = replace(FACTS, contact_id=None)
    assert decide(AFTERNOON, no_contact, settings) == batch("feedback")


def test_a_later_useful_mark_unmutes() -> None:
    unmuted = [mark("not_useful", 5), mark("not_useful", 4), mark("useful", 2)]
    muted_again = [*unmuted, mark("not_useful", 1)]
    useful_first = [mark("useful", 9), mark("not_useful", 5), mark("not_useful", 4)]

    assert muted_pairs(unmuted, AFTERNOON) == frozenset()
    assert muted_pairs(muted_again, AFTERNOON) == frozenset({("triage_decision", CONTACT)})
    assert muted_pairs(useful_first, AFTERNOON) == frozenset({("triage_decision", CONTACT)})


def test_marks_older_than_the_window_are_ignored() -> None:
    days = MUTE_WINDOW / timedelta(days=1)
    stale = [mark("not_useful", days + 1), mark("not_useful", 1)]
    edge = [mark("not_useful", days), mark("not_useful", 1)]

    assert muted_pairs(stale, AFTERNOON) == frozenset()
    assert muted_pairs(edge, AFTERNOON) == frozenset({("triage_decision", CONTACT)})


def test_marks_are_counted_per_kind_and_contact() -> None:
    marks = [
        mark("not_useful", 2),
        mark("not_useful", 1, kind="task_closed"),
        replace(mark("not_useful", 1), contact_id=OTHER_CONTACT),
        replace(mark("not_useful", 1), contact_id=None),
        replace(mark("not_useful", 2), contact_id=None),
    ]
    assert muted_pairs(marks, AFTERNOON) == frozenset({("triage_decision", None)})


# --- rule 4: quiet by default ------------------------------------------------


@pytest.mark.parametrize("priority", [Priority.LOW, Priority.NORMAL])
@pytest.mark.parametrize("now", [AFTERNOON, LATE_EVENING])
def test_normal_and_low_are_batched_by_default(priority: Priority, now: datetime) -> None:
    facts = replace(FACTS, priority=priority, bypass_quiet_hours=True)
    assert decide(now, facts) == batch("quiet_by_default")


# --- rule 5: quiet hours -----------------------------------------------------


@pytest.mark.parametrize("now", [LATE_EVENING, SMALL_HOURS])
def test_quiet_hours_batch_non_urgent_into_the_digest(now: datetime) -> None:
    assert decide(AFTERNOON) == IMMEDIATE
    assert decide(now) == batch("quiet_hours")


@pytest.mark.parametrize("now", [LATE_EVENING, SMALL_HOURS])
def test_quiet_hours_defer_urgent_until_they_end_without_bypass(now: datetime) -> None:
    urgent = replace(FACTS, priority=Priority.URGENT)
    assert decide(now, urgent) == DeliveryDecision(Verdict.DEFER, QUIET_ENDS, "quiet_hours")


@pytest.mark.parametrize(
    ("now", "deliver_after"),
    [
        (local(10, 27, 22, 59), None),
        (local(10, 27, 23, 0), QUIET_ENDS),
        (local(10, 27, 23, 59), QUIET_ENDS),
        (local(10, 28, 0, 0), QUIET_ENDS),
        (local(10, 28, 7, 59), QUIET_ENDS),
        (local(10, 28, 8, 0), None),
    ],
)
def test_quiet_hours_that_wrap_midnight(now: datetime, deliver_after: datetime | None) -> None:
    decision = decide(now, replace(FACTS, priority=Priority.URGENT))

    if deliver_after is None:
        assert decision == IMMEDIATE
    else:
        assert decision == DeliveryDecision(Verdict.DEFER, deliver_after, "quiet_hours")


@pytest.mark.parametrize(
    ("now", "inside"),
    [
        (local(10, 28, 0, 59), False),
        (local(10, 28, 1, 0), True),
        (local(10, 28, 5, 59), True),
        (local(10, 28, 6, 0), False),
        (local(10, 27, 23, 30), False),
    ],
)
def test_quiet_hours_that_do_not_wrap(now: datetime, inside: bool) -> None:
    settings = replace(SETTINGS, quiet_hours=(time(1), time(6)))
    decision = decide(now, replace(FACTS, priority=Priority.URGENT), settings)

    expected = DeliveryDecision(Verdict.DEFER, local(10, 28, 6), "quiet_hours")
    assert decision == (expected if inside else IMMEDIATE)


@pytest.mark.parametrize("quiet_hours", [None, (time(8), time(8))])
def test_no_quiet_hours_when_unset_or_empty(quiet_hours: tuple[time, time] | None) -> None:
    settings = replace(SETTINGS, quiet_hours=quiet_hours)
    assert decide(SMALL_HOURS, settings=settings) == IMMEDIATE


def test_urgent_bypasses_quiet_hours_only_when_enabled() -> None:
    urgent = replace(FACTS, priority=Priority.URGENT)
    bypass = replace(SETTINGS, urgent_bypass=True)

    assert decide(SMALL_HOURS, urgent, bypass) == IMMEDIATE
    assert decide(SMALL_HOURS, urgent).verdict == Verdict.DEFER
    # The owner's bypass is for urgent alerts only.
    assert decide(SMALL_HOURS, settings=bypass) == batch("quiet_hours")
    # Bypassing quiet hours does not bypass the cap.
    capped = RecentLoad(immediate_last_hour=6, suspicious_from_contact_24h=0)
    assert decide(SMALL_HOURS, urgent, bypass, capped) == batch("cap")


def test_a_monitor_spec_can_bypass_quiet_hours() -> None:
    monitor = replace(FACTS, kind=NotificationKind.MONITOR_MATCH, priority=Priority.HIGH)
    asked = replace(monitor, bypass_quiet_hours=True)

    assert decide(SMALL_HOURS, asked) == IMMEDIATE
    assert decide(SMALL_HOURS, monitor) == batch("quiet_hours")
    urgent_asked = replace(asked, priority=Priority.URGENT)
    assert decide(SMALL_HOURS, urgent_asked) == IMMEDIATE


@pytest.mark.parametrize(
    ("now", "deliver_after"),
    [
        # Autumn: quiet hours start in CEST (+2) and end in CET (+1).
        (local(10, 24, 23, 30, BERLIN), datetime(2026, 10, 25, 7, tzinfo=UTC)),
        # Spring: they start in CET (+1) and end in CEST (+2).
        (local(3, 28, 23, 30, BERLIN), datetime(2026, 3, 29, 6, tzinfo=UTC)),
    ],
)
def test_the_deferral_ends_at_the_local_end_across_a_dst_change(
    now: datetime, deliver_after: datetime
) -> None:
    settings = replace(SETTINGS, timezone=BERLIN)
    decision = decide(now, replace(FACTS, priority=Priority.URGENT), settings)
    assert decision == DeliveryDecision(Verdict.DEFER, deliver_after, "quiet_hours")


@pytest.mark.parametrize(
    ("now", "deliver_after"),
    [
        # 02:15 happens twice on 2026-10-25; each pass ends at the 02:30 after it.
        (local(10, 25, 2, 15, BERLIN, fold=0), datetime(2026, 10, 25, 0, 30, tzinfo=UTC)),
        (local(10, 25, 2, 15, BERLIN, fold=1), datetime(2026, 10, 25, 1, 30, tzinfo=UTC)),
    ],
)
def test_an_end_in_the_repeated_hour_is_the_one_after_now(
    now: datetime, deliver_after: datetime
) -> None:
    settings = replace(SETTINGS, timezone=BERLIN, quiet_hours=(time(1), time(2, 30)))
    decision = decide(now, replace(FACTS, priority=Priority.URGENT), settings)
    assert decision == DeliveryDecision(Verdict.DEFER, deliver_after, "quiet_hours")


def test_an_end_in_the_skipped_hour_is_never_early() -> None:
    # 02:30 does not exist on 2026-03-29 in Berlin (02:00 CET jumps to 03:00
    # CEST at 01:00 UTC). The deferral must not release the alert early.
    settings = replace(SETTINGS, timezone=BERLIN, quiet_hours=(time(1), time(2, 30)))
    now = local(3, 29, 1, 45, BERLIN)
    decision = decide(now, replace(FACTS, priority=Priority.URGENT), settings)

    assert decision.verdict == Verdict.DEFER
    assert decision.deliver_after is not None
    assert decision.deliver_after >= datetime(2026, 3, 29, 1, tzinfo=UTC)


# --- rule 6: the hourly cap --------------------------------------------------


@pytest.mark.parametrize("priority", [Priority.HIGH, Priority.URGENT])
def test_the_hourly_cap_batches_every_standard_alert_over_it(priority: Priority) -> None:
    facts = replace(FACTS, priority=priority)
    under = RecentLoad(immediate_last_hour=5, suspicious_from_contact_24h=0)
    at_cap = RecentLoad(immediate_last_hour=6, suspicious_from_contact_24h=0)

    assert decide(AFTERNOON, facts, load=under) == IMMEDIATE
    assert decide(AFTERNOON, facts, load=at_cap) == batch("cap")


# --- inputs ------------------------------------------------------------------


def test_a_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        decide(AFTERNOON.replace(tzinfo=None))


def test_an_unknown_timezone_reads_as_utc() -> None:
    """Fail safe, like every other owner-time reader: a bad stored zone never
    breaks delivery; quiet hours are then read on UTC."""
    settings = replace(SETTINGS, timezone="Nowhere/City", quiet_hours=(time(23), time(8)))
    urgent = replace(FACTS, priority=Priority.URGENT)
    night = datetime(2026, 10, 27, 23, 30, tzinfo=UTC)
    assert decide(night, urgent, settings) == DeliveryDecision(
        Verdict.DEFER, datetime(2026, 10, 28, 8, 0, tzinfo=UTC), "quiet_hours"
    )
    assert decide(AFTERNOON, urgent, settings) == IMMEDIATE
