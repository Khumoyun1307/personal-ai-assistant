"""When an alert reaches the owner (S05.2 §3.1; PRD §6.7, §14, FR-TRI-07).

Pure: no I/O, no clock. Delivery gathers the facts, the owner's settings and
the recent load inside its transaction and asks `decide_delivery`; the rules
run in a fixed order and the first that applies names the reason code, which
delivery stores on the row.

Quiet by default: only `high` and `urgent` alerts interrupt the owner, the
rest wait for the digest. System alerts (ARCHITECTURE §17) are never held.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant.inbox.notification_types import AlertClass, NotificationKind, Priority

#: Feedback older than this no longer mutes; `notifications` rows (and their
#: feedback) are swept after 30 days anyway (ARCHITECTURE §10.4).
MUTE_WINDOW = timedelta(days=30)
#: One "not useful" can be a slip; two of the same kind for the same contact
#: is a pattern (TL decision, S05.2 §3.1).
MUTE_THRESHOLD = 2

FEEDBACK_USEFUL = "useful"
FEEDBACK_NOT_USEFUL = "not_useful"

_LADDER = (Priority.LOW, Priority.NORMAL, Priority.HIGH, Priority.URGENT)


class Verdict(StrEnum):
    IMMEDIATE = "immediate"
    DEFER = "defer"
    BATCH = "batch"
    #: Never returned by `decide_delivery`: delivery uses it when the alert's
    #: source is gone (S05.2 §3.2).
    SUPPRESS = "suppress"


@dataclass(frozen=True, slots=True)
class NotificationFacts:
    kind: NotificationKind
    alert_class: AlertClass
    priority: Priority
    contact_id: int | None
    #: The contact's priority: `vip`, `normal` or `low` (None: not set).
    contact_priority: str | None
    #: A monitor spec that asked to reach the owner in quiet hours (R9).
    bypass_quiet_hours: bool


@dataclass(frozen=True, slots=True)
class OwnerAlertSettings:
    #: IANA name; quiet hours are the owner's wall clock.
    timezone: str
    #: Local (start, end); start inclusive, end exclusive; may wrap midnight.
    quiet_hours: tuple[time, time] | None
    urgent_bypass: bool
    cap_per_hour: int
    #: `(kind, contact_id)` pairs (`None`: alerts that name no contact).
    #: Derived from feedback by `muted_pairs`, never stored.
    muted: frozenset[tuple[str, int | None]]


@dataclass(frozen=True, slots=True)
class RecentLoad:
    #: Standard alerts sent immediately in the last hour (the cap's count).
    immediate_last_hour: int
    #: Earlier suspicious alerts about this contact in the last 24 h.
    suspicious_from_contact_24h: int


@dataclass(frozen=True, slots=True)
class DeliveryDecision:
    verdict: Verdict
    #: Set only for `DEFER`: the UTC instant the alert may go out.
    deliver_after: datetime | None
    reason: str


@dataclass(frozen=True, slots=True)
class FeedbackMark:
    """One notification's feedback (the last press on its card)."""

    kind: str
    contact_id: int | None
    feedback: str
    marked_at: datetime


def decide_delivery(
    facts: NotificationFacts,
    settings: OwnerAlertSettings,
    load: RecentLoad,
    now: datetime,
) -> DeliveryDecision:
    if now.tzinfo is None:
        raise ValueError("naive datetime: `now` must be timezone-aware UTC")
    if facts.alert_class == AlertClass.SYSTEM:
        return _decision(Verdict.IMMEDIATE, "system")

    priority = effective_priority(facts, load)
    if priority != Priority.URGENT and _is_muted(facts, settings.muted):
        return _decision(Verdict.BATCH, "feedback")
    if priority in (Priority.LOW, Priority.NORMAL):
        return _decision(Verdict.BATCH, "quiet_by_default")

    quiet_until = _quiet_period_end(settings, now)
    if quiet_until is not None and not _bypasses_quiet_hours(facts, settings, priority):
        if priority == Priority.HIGH:
            return _decision(Verdict.BATCH, "quiet_hours")
        # Held, never dropped: the owner sees it when the quiet hours end.
        return DeliveryDecision(Verdict.DEFER, quiet_until, "quiet_hours")

    if load.immediate_last_hour >= settings.cap_per_hour:
        return _decision(Verdict.BATCH, "cap")
    return _decision(Verdict.IMMEDIATE, "immediate")


def effective_priority(facts: NotificationFacts, load: RecentLoad) -> Priority:
    """The alert's priority after the contact's priority and repeat rules."""
    step = _LADDER.index(facts.priority)
    if facts.contact_priority == "vip":
        step = min(step + 1, len(_LADDER) - 1)
    elif facts.contact_priority == "low" and facts.kind != NotificationKind.MONITOR_MATCH:
        # The owner asked for that monitor explicitly (R9): never demote it.
        step = max(step - 1, 0)
    if facts.kind == NotificationKind.TRIAGE_SUSPICIOUS and load.suspicious_from_contact_24h >= 1:
        # PRD §11.5 "alert if repeated".
        step = max(step, _LADDER.index(Priority.HIGH))
    return _LADDER[step]


def muted_pairs(marks: Iterable[FeedbackMark], now: datetime) -> frozenset[tuple[str, int | None]]:
    """The `(kind, contact_id)` pairs the owner's feedback has muted.

    Muted: at least `MUTE_THRESHOLD` "not useful" marks within `MUTE_WINDOW`,
    and no "useful" mark newer than the newest of them. Each mark is one
    notification, so the count is of notifications.
    """
    since = now - MUTE_WINDOW
    not_useful: defaultdict[tuple[str, int | None], list[datetime]] = defaultdict(list)
    latest_useful: dict[tuple[str, int | None], datetime] = {}
    for mark in marks:
        if mark.marked_at < since:
            continue
        pair = (mark.kind, mark.contact_id)
        if mark.feedback == FEEDBACK_NOT_USEFUL:
            not_useful[pair].append(mark.marked_at)
        elif mark.feedback == FEEDBACK_USEFUL:
            latest_useful[pair] = max(mark.marked_at, latest_useful.get(pair, mark.marked_at))
    return frozenset(
        pair
        for pair, times in not_useful.items()
        if len(times) >= MUTE_THRESHOLD
        and (pair not in latest_useful or latest_useful[pair] <= max(times))
    )


def _decision(verdict: Verdict, reason: str) -> DeliveryDecision:
    return DeliveryDecision(verdict, None, reason)


def _is_muted(facts: NotificationFacts, muted: frozenset[tuple[str, int | None]]) -> bool:
    """The exact pair only: `(kind, None)` is derived from feedback on alerts
    that name no contact, so it mutes those alerts, never the kind for every
    contact."""
    return (facts.kind.value, facts.contact_id) in muted


def _bypasses_quiet_hours(
    facts: NotificationFacts, settings: OwnerAlertSettings, priority: Priority
) -> bool:
    if facts.bypass_quiet_hours:
        return True  # only high and urgent get this far
    return priority == Priority.URGENT and settings.urgent_bypass


def _quiet_period_end(settings: OwnerAlertSettings, now: datetime) -> datetime | None:
    """When the quiet period `now` falls in ends, in UTC; None outside one.

    Inside-or-not is the owner's wall clock, so it follows DST. A range whose
    start equals its end is empty (no quiet hours).
    """
    if settings.quiet_hours is None:
        return None
    start, end = settings.quiet_hours
    if start == end:
        return None
    try:
        zone = ZoneInfo(settings.timezone)
    except ZoneInfoNotFoundError, ValueError:
        # Fail safe, like every owner-time reader: never break delivery.
        zone = ZoneInfo("UTC")
    wall = now.astimezone(zone).replace(tzinfo=None)
    clock = wall.time()
    if start < end:
        if not start <= clock < end:
            return None
        end_day = wall.date()
    elif clock >= start:
        end_day = wall.date() + timedelta(days=1)
    elif clock < end:
        end_day = wall.date()
    else:
        return None
    return _first_instant_at(end_day, end, zone, now)


def _first_instant_at(day: date, at: time, zone: ZoneInfo, now: datetime) -> datetime:
    """The UTC instant the wall clock shows `day at` next after `now`.

    In the repeated autumn hour the wall time happens twice: take the first
    one after `now`. In the skipped spring hour it never happens: take the
    later reading, so a held alert is released late rather than early.
    """
    wall = datetime.combine(day, at)
    readings = sorted({wall.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)})
    real = [r for r in readings if r.astimezone(zone).replace(tzinfo=None) == wall]
    later = [r for r in real if r > now]
    return later[0] if later else readings[-1]
