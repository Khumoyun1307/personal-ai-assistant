"""Schedule maths: next occurrence, DST, and timezone policies.

Pure module: no database access, no clock reads, no I/O.
Every function takes the time it needs as an argument.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr  # type: ignore[import-untyped]

TzPolicy = Literal["follow_owner", "fixed"]

_HHMM_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


class ScheduleError(ValueError):
    """An invalid schedule (bad rule, unknown timezone, wrong combination)."""


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    timezone: str
    tz_policy: TzPolicy
    rrule: str | None = None
    dtstart_local: datetime | None = None
    run_at: datetime | None = None


def validate(spec: ScheduleSpec) -> None:
    """Validate schedule specification, raising ScheduleError on failure."""
    if spec.tz_policy not in ("follow_owner", "fixed"):
        raise ScheduleError("tz_policy must be 'follow_owner' or 'fixed'")

    try:
        ZoneInfo(spec.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError("Unknown timezone") from exc

    has_rrule = spec.rrule is not None
    has_run_at = spec.run_at is not None

    if not has_rrule and not has_run_at:
        raise ScheduleError("Schedule must specify either rrule or run_at")
    if has_rrule and has_run_at:
        raise ScheduleError("Schedule cannot specify both rrule and run_at")

    run_at = spec.run_at
    if run_at is not None:
        if run_at.tzinfo is None or run_at.tzinfo.utcoffset(run_at) is None:
            raise ScheduleError("run_at must be timezone-aware")
        if spec.dtstart_local is not None:
            raise ScheduleError("run_at cannot be combined with dtstart_local")

    rrule_str = spec.rrule
    if rrule_str is not None:
        if spec.dtstart_local is None:
            raise ScheduleError("rrule requires dtstart_local")
        if (
            spec.dtstart_local.tzinfo is not None
            and spec.dtstart_local.tzinfo.utcoffset(spec.dtstart_local) is not None
        ):
            raise ScheduleError("dtstart_local must be naive")

        upper_rule = rrule_str.upper()
        if "DTSTART" in upper_rule:
            raise ScheduleError("rrule must not contain DTSTART")
        if "TZID" in upper_rule:
            raise ScheduleError("rrule must not contain TZID")
        if "FREQ=SECONDLY" in upper_rule or "FREQ=MINUTELY" in upper_rule:
            raise ScheduleError("SECONDLY and MINUTELY frequencies are not allowed")

        try:
            rrulestr(rrule_str, dtstart=spec.dtstart_local)
        except ValueError as exc:
            raise ScheduleError("Invalid rrule string") from exc


def to_utc(local: datetime, timezone: str) -> datetime:
    """Convert naive local wall time to aware UTC datetime.

    Handles DST transitions:
    - Ambiguous time (autumn fold / fall back): fold=0 is used so a daily rule
      fires once on the fall-back day (first occurrence).
    - Non-existent time (spring gap / spring forward): finds the earliest UTC instant
      (to the second) whose local wall time is >= local via binary search.
      A gap time maps to the first valid instant so a reminder is never lost or early.
    """
    if local.tzinfo is not None and local.tzinfo.utcoffset(local) is not None:
        raise ValueError("local datetime must be naive")

    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError("Unknown timezone") from exc

    # First attempt: fold=0 (first occurrence for ambiguous times)
    cand0 = local.replace(tzinfo=zone, fold=0)
    utc0 = cand0.astimezone(UTC)
    back0 = utc0.astimezone(zone).replace(tzinfo=None)

    if back0 == local:
        return utc0

    # Non-existent spring-forward gap: binary search between fold=1 and fold=0 conversions
    cand1 = local.replace(tzinfo=zone, fold=1)
    utc1 = cand1.astimezone(UTC)

    t_low = int(min(utc0, utc1).timestamp())
    t_high = int(max(utc0, utc1).timestamp())

    # Find the earliest UTC instant (to the second) whose local wall time in zone is >= local
    best_t = t_high
    while t_low <= t_high:
        mid = (t_low + t_high) // 2
        inst = datetime.fromtimestamp(mid, tz=UTC)
        local_wall = inst.astimezone(zone).replace(tzinfo=None)
        if local_wall >= local:
            best_t = mid
            t_high = mid - 1
        else:
            t_low = mid + 1

    return datetime.fromtimestamp(best_t, tz=UTC)


def next_occurrence(spec: ScheduleSpec, *, after: datetime) -> datetime | None:
    """Return the next aware UTC instant strictly after `after`, or None.

    The search starts a day before `after` so a candidate shifted forward by a
    gap is not missed; the 1 000-candidate cap exists so a rule that never matches
    cannot loop forever.
    """
    if after.tzinfo is None or after.tzinfo.utcoffset(after) is None:
        raise ValueError("after must be timezone-aware")

    validate(spec)

    after_utc = after.astimezone(UTC)

    run_at = spec.run_at
    if run_at is not None:
        run_at_utc = run_at.astimezone(UTC)
        return run_at_utc if run_at_utc > after_utc else None

    rrule_str = spec.rrule
    dtstart_local = spec.dtstart_local
    if rrule_str is None or dtstart_local is None:
        return None

    zone = ZoneInfo(spec.timezone)
    after_local = after_utc.astimezone(zone).replace(tzinfo=None)
    start_search = after_local - timedelta(days=1)
    if start_search < dtstart_local:
        start_search = dtstart_local

    rule = rrulestr(rrule_str, dtstart=dtstart_local)
    for count, cand_local in enumerate(rule.xafter(start_search, inc=True), start=1):
        if count > 1000:
            return None
        cand_utc = to_utc(cand_local, spec.timezone)
        if cand_utc > after_utc:
            return cand_utc

    return None


def rezone(spec: ScheduleSpec, new_timezone: str) -> ScheduleSpec:
    """Rezone a schedule spec according to its timezone policy."""
    try:
        ZoneInfo(new_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError("Unknown timezone") from exc

    if spec.run_at is not None or spec.tz_policy == "fixed":
        return spec

    return ScheduleSpec(
        timezone=new_timezone,
        tz_policy=spec.tz_policy,
        rrule=spec.rrule,
        dtstart_local=spec.dtstart_local,
        run_at=spec.run_at,
    )


def daily_at(hhmm: str, timezone: str, *, starting: date) -> ScheduleSpec:
    """Create a daily recurring schedule spec with follow_owner policy."""
    if not _HHMM_RE.match(hhmm):
        raise ScheduleError("Invalid hhmm format")

    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError("Unknown timezone") from exc

    hour = int(hhmm[:2])
    minute = int(hhmm[3:])
    dtstart_local = datetime(  # noqa: DTZ001 - naive wall time is required
        starting.year, starting.month, starting.day, hour, minute
    )

    spec = ScheduleSpec(
        timezone=timezone,
        tz_policy="follow_owner",
        rrule="FREQ=DAILY",
        dtstart_local=dtstart_local,
    )
    validate(spec)
    return spec


def next_local_time(hhmm: str, timezone: str, *, after: datetime) -> datetime:
    """Return the next instant strictly after `after` matching local wall time `hhmm`."""
    if after.tzinfo is None or after.tzinfo.utcoffset(after) is None:
        raise ValueError("after must be timezone-aware")

    if not _HHMM_RE.match(hhmm):
        raise ScheduleError("Invalid hhmm format")

    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError("Unknown timezone") from exc

    hour = int(hhmm[:2])
    minute = int(hhmm[3:])

    after_in_tz = after.astimezone(zone)
    local_date = after_in_tz.date()

    cand_today = datetime(  # noqa: DTZ001 - naive wall time is required
        local_date.year, local_date.month, local_date.day, hour, minute
    )
    cand_today_utc = to_utc(cand_today, timezone)
    if cand_today_utc > after:
        return cand_today_utc

    next_date = local_date + timedelta(days=1)
    cand_tomorrow = datetime(  # noqa: DTZ001 - naive wall time is required
        next_date.year, next_date.month, next_date.day, hour, minute
    )
    return to_utc(cand_tomorrow, timezone)


class Lateness(StrEnum):
    ON_TIME = "on_time"
    LATE = "late"
    SKIP = "skip"


def lateness(
    occurrence_at: datetime,
    now: datetime,
    *,
    grace: timedelta,
    max_lateness: timedelta,
) -> Lateness:
    """Evaluate whether an occurrence is on time, late, or should be skipped.

    Follows FR-FUP-08:
    - Within grace period: ON_TIME.
    - Past grace period up to max_lateness: LATE (executed with a late note).
    - Beyond max_lateness: SKIP (skipped with a note).
    """
    delay = now - occurrence_at
    if delay <= grace:
        return Lateness.ON_TIME
    if delay <= max_lateness:
        return Lateness.LATE
    return Lateness.SKIP
