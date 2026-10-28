"""Time automations: the occurrence ledger, firing, restores and the digest
schedule (ARCHITECTURE §12.4; S05.3b §3.2, §3.3, §3.7; FR-FUP-08).

Domain rows are the truth for scheduled work; jobs are only triggers
(AGENTS §13). Every due occurrence is first written to
`automation_occurrences` — `claimed` (to fire), or `skipped` with a note —
in the same transaction that advances the automation's `next_run_at`, so a
later reconciler run never claims it again and a restart loses nothing
(`UNIQUE (automation_id, occurrence_at)` backs this up). Firing moves a
claimed occurrence to `fired` with a conditional update; whatever it
delivers deduplicates on its own (a digest on its `digests` row).

The schedule maths is S05.3a's pure `automation/schedule.py`; the rule is
`automation/rules.py::ScheduleRule`. A row whose rule or schedule no longer
validates is paused with a content-free `automation_invalid` audit row and
never fires (fail closed). Monitors (S05.4) are event-only and are never
touched here.

Occurrence instants are whole seconds: the `automation.fire` job names one
by its epoch second (F0-interfaces §6.1).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts import preferences
from assistant.accounts.models import Account, AuditLogEntry
from assistant.automation import repository
from assistant.automation.models import Automation, AutomationOccurrence
from assistant.automation.rules import (
    DTSTART_FORMAT,
    DigestRule,
    ScheduleRule,
    load_schedule_rule,
)
from assistant.automation.schedule import (
    Lateness,
    ScheduleError,
    ScheduleSpec,
    TzPolicy,
    daily_at,
    lateness,
    next_occurrence,
    rezone,
    validate,
)
from assistant.core.clock import Clock

#: An occurrence this close to its time is on time (no "late" note).
GRACE = timedelta(minutes=2)
#: Due automations one reconciler run handles; the rest wait a minute.
RECONCILE_LIMIT = 100
#: Missed occurrences recorded one by one before the jump to the next
#: future one (S05.3b §3.2).
MAX_SKIPPED_ROWS = 10
#: Kinds with a time schedule. A monitor is event-only (S05.4).
SCHEDULED_KINDS = ("reminder", "follow_up", "scheduled_summary", "delegation_end", "digest")

#: The daily digest (S05.3b §3.2 step 1; flag `digest.daily`).
DIGEST_KIND = "digest"
DIGEST_FLAG = "digest.daily"
DIGEST_RRULE = "FREQ=DAILY"
#: A digest more than two hours late is skipped: the next one covers its
#: period anyway (its start follows the last sent digest), and a 21:00
#: digest arriving after midnight is noise.
DIGEST_MAX_LATENESS_MINUTES = 120

EVENT_AUTOMATION_INVALID = "automation_invalid"


class Note(StrEnum):
    """`automation_occurrences.note_code`."""

    LATE = "late"
    SKIPPED_OBSOLETE = "skipped_obsolete"
    SKIPPED_RESTORE = "skipped_restore"
    #: No product handler for this kind yet (M07 adds them).
    NO_HANDLER = "no_handler"
    #: The automation or its account stopped before the occurrence fired.
    INACTIVE = "inactive"
    #: The kind's release flag is off.
    DISABLED = "disabled"


def epoch_of(moment: datetime) -> int:
    """An occurrence instant as the `automation.fire` argument (UTC seconds)."""
    return int(moment.timestamp())


def from_epoch(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def _whole_second(moment: datetime | None) -> datetime | None:
    return None if moment is None else moment.replace(microsecond=0)


@dataclass(frozen=True, slots=True)
class DueOccurrence:
    """A claimed occurrence whose `automation.fire` job is to be deferred."""

    automation_id: int
    account_id: int
    occurrence_at: datetime

    @property
    def epoch(self) -> int:
        return epoch_of(self.occurrence_at)


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """What one `reconcile_due` run did (ids and counts only)."""

    claimed: tuple[DueOccurrence, ...] = ()
    skipped: int = 0
    completed: int = 0
    expired: int = 0
    invalid: int = 0
    rezoned: int = 0
    #: Digest automations created, changed, paused or resumed.
    digests: int = 0


# ------------------------------------------------------------ loading
def schedule_of(automation: Automation, rule: ScheduleRule) -> ScheduleSpec:
    """The row's schedule as S05.3a's `ScheduleSpec`; ScheduleError if it
    no longer validates."""
    try:
        start = rule.start()
    except ValueError as error:
        raise ScheduleError("invalid dtstart_local") from error
    spec = ScheduleSpec(
        timezone=automation.timezone,
        tz_policy=cast(TzPolicy, automation.tz_policy),
        rrule=automation.rrule,
        dtstart_local=start,
        run_at=automation.run_at,
    )
    validate(spec)
    return spec


def _load(automation: Automation) -> tuple[ScheduleRule, ScheduleSpec] | None:
    try:
        rule = load_schedule_rule(automation.kind, automation.spec)
        return rule, schedule_of(automation, rule)
    except ValidationError, ScheduleError:
        return None


def _pause_invalid(session: AsyncSession, automation: Automation, now: datetime) -> None:
    """Fail closed: a stored rule that no longer validates never fires."""
    automation.status = "paused"
    automation.updated_at = now
    session.add(
        AuditLogEntry(
            account_id=automation.account_id,
            at=now,
            actor="system",
            event=EVENT_AUTOMATION_INVALID,
            subject_type="automation",
            subject_id=automation.id,
            details={"kind": automation.kind},
        )
    )


def _end(automation: Automation, status: str, now: datetime) -> None:
    automation.status = status
    automation.ended_at = now
    automation.updated_at = now


async def _record(
    session: AsyncSession,
    automation: Automation,
    occurrence_at: datetime,
    status: str,
    note: Note | None,
    now: datetime,
) -> bool:
    """One ledger row; False when the occurrence is already recorded."""
    inserted = await session.scalar(
        insert(AutomationOccurrence)
        .values(
            account_id=automation.account_id,
            automation_id=automation.id,
            occurrence_at=occurrence_at,
            status=status,
            note_code=None if note is None else note.value,
            created_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_automation_occurrences_automation_id_occurrence_at")
        .returning(AutomationOccurrence.id)
    )
    return inserted is not None


# ------------------------------------------------------------ creating
async def create_scheduled(
    session: AsyncSession,
    *,
    account_id: int,
    kind: str,
    rule: ScheduleRule,
    spec: ScheduleSpec,
    clock: Clock,
    refs: Mapping[str, list[int]] | None = None,
    expires_at: datetime | None = None,
    task_id: int | None = None,
    run_id: int | None = None,
    tool_call_id: str | None = None,
    confirmed_at: datetime | None = None,
) -> Automation:
    """A time automation (for M07's reminders and scheduled summaries; the
    digest uses `ensure_digest_schedule`). The rule's `dtstart_local` is
    taken from `spec`; the first `next_run_at` is computed from now.
    ScheduleError for a schedule with no future occurrence."""
    if kind not in SCHEDULED_KINDS:
        raise ValueError("not a scheduled automation kind")
    validate(spec)
    start = spec.dtstart_local
    if start is not None and (start.second or start.microsecond):
        raise ScheduleError("dtstart_local is to the minute")
    run_at = _whole_second(spec.run_at)
    stored = ScheduleSpec(
        timezone=spec.timezone,
        tz_policy=spec.tz_policy,
        rrule=spec.rrule,
        dtstart_local=start,
        run_at=run_at,
    )
    now = clock.now()
    next_at = next_occurrence(stored, after=now)
    if next_at is None:
        raise ScheduleError("the schedule has no future occurrence")
    stored_rule = rule.model_copy(
        update={"dtstart_local": None if start is None else start.strftime(DTSTART_FORMAT)}
    )
    return await repository.create(
        session,
        account_id=account_id,
        kind=kind,
        spec=stored_rule,
        refs=refs or {},
        timezone=spec.timezone,
        tz_policy=spec.tz_policy,
        clock=clock,
        rrule=spec.rrule,
        run_at=run_at,
        next_run_at=_whole_second(next_at),
        expires_at=expires_at,
        task_id=task_id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        confirmed_at=confirmed_at,
    )


# ------------------------------------------------------------ the digest schedule
@dataclass(frozen=True, slots=True)
class _At:
    """A clock stopped at one instant (the reconciler's `now`)."""

    moment: datetime

    def now(self) -> datetime:
        return self.moment


async def ensure_digest_schedule(
    session: AsyncSession, account: Account, clock: Clock
) -> Automation | None:
    """Bring the account's digest automation in step with its `digest_time`
    preference and timezone (S05.3b §3.2 step 1): create it, move it,
    pause it for `"off"`, resume it. None when it has none (off, or a
    timezone the schedule maths refuses)."""
    automation, _ = await _sync_digest(session, account, clock.now())
    return automation


async def _sync_digest(
    session: AsyncSession, account: Account, now: datetime
) -> tuple[Automation | None, bool]:
    wanted = str(preferences.current(account)["digest_time"])
    automation = await session.scalar(
        select(Automation)
        .where(Automation.account_id == account.id)
        .where(Automation.kind == DIGEST_KIND)
        .where(Automation.status.in_(("active", "paused")))
        .order_by(Automation.id.desc())
        .limit(1)
        .with_for_update()
    )
    if wanted == "off":
        if automation is None or automation.status != "active":
            return automation, False
        automation.status = "paused"
        automation.updated_at = now
        await session.flush()
        return automation, True
    try:
        today = now.astimezone(ZoneInfo(account.timezone)).date()
        spec = daily_at(wanted, account.timezone, starting=today)
        next_at = next_occurrence(spec, after=now)
    except ScheduleError, ZoneInfoNotFoundError, ValueError:
        return automation, False  # no digest for a timezone we cannot schedule in
    start = cast(datetime, spec.dtstart_local)
    rule = DigestRule(
        max_lateness_minutes=DIGEST_MAX_LATENESS_MINUTES,
        dtstart_local=start.strftime(DTSTART_FORMAT),
    )
    if automation is None:
        created = await repository.create(
            session,
            account_id=account.id,
            kind=DIGEST_KIND,
            spec=rule,
            refs={},
            timezone=account.timezone,
            tz_policy="follow_owner",
            clock=_At(now),
            rrule=DIGEST_RRULE,
            next_run_at=next_at,
            confirmed_at=now,
        )
        return created, True
    if automation.status == "active" and _digest_matches(automation, wanted, account.timezone):
        return automation, False
    # Moved, resumed or repaired (the digest's rule holds no owner content,
    # so rewriting it is always safe): the next run is computed from now.
    automation.spec = rule.model_dump(mode="json")
    automation.rrule = DIGEST_RRULE
    automation.run_at = None
    automation.timezone = account.timezone
    automation.tz_policy = "follow_owner"
    automation.next_run_at = next_at
    automation.status = "active"
    automation.updated_at = now
    await session.flush()
    return automation, True


def _digest_matches(automation: Automation, wanted: str, timezone: str) -> bool:
    loaded = _load(automation)
    if loaded is None:
        return False
    rule, _ = loaded
    stored = rule.dtstart_local or ""
    return (
        stored[-5:] == wanted
        and automation.timezone == timezone
        and automation.rrule == DIGEST_RRULE
        and automation.next_run_at is not None
    )


async def _sync_digests(session: AsyncSession, now: datetime) -> int:
    accounts = (
        (
            await session.execute(
                select(Account).where(Account.status == "active").order_by(Account.id)
            )
        )
        .scalars()
        .all()
    )
    changed = 0
    for account in accounts:
        _, moved = await _sync_digest(session, account, now)
        changed += int(moved)
    return changed


# ------------------------------------------------------------ the reconciler
async def reconcile_due(
    session: AsyncSession,
    now: datetime,
    *,
    limit: int = RECONCILE_LIMIT,
    digest_enabled: bool = False,
) -> ReconcileReport:
    """`system.reconcile_schedules` (S05.3b §3.2), in the caller's one
    transaction: (1) digest automations follow the preference (only with
    flag `digest.daily`); (2) follow-owner automations follow the owner's
    timezone; (3) due occurrences are claimed or skipped and `next_run_at`
    advanced. The caller defers `automation.fire` for `claimed` after the
    commit."""
    digests = await _sync_digests(session, now) if digest_enabled else 0
    rezoned, invalid_zones = await _follow_owner_timezones(session, now)
    claimed: list[DueOccurrence] = []
    counts: Counter[str] = Counter()
    rows = (
        (
            await session.execute(
                select(Automation)
                .where(Automation.status == "active")
                .where(Automation.kind.in_(SCHEDULED_KINDS))
                .where(Automation.next_run_at.is_not(None))
                .where(Automation.next_run_at <= now)
                .order_by(Automation.next_run_at, Automation.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for automation in rows:
        if automation.expires_at is not None and automation.expires_at <= now:
            _end(automation, "expired", now)
            counts["expired"] += 1
            continue
        loaded = _load(automation)
        if loaded is None:
            _pause_invalid(session, automation, now)
            counts["invalid"] += 1
            continue
        rule, spec = loaded
        occurrence = await _advance(session, automation, rule, spec, now, claimed, counts)
        automation.next_run_at = _whole_second(occurrence)
        automation.last_run_at = now
        automation.updated_at = now
        if occurrence is None:
            _end(automation, "completed", now)
            counts["completed"] += 1
    await session.flush()
    return ReconcileReport(
        claimed=tuple(claimed),
        skipped=counts["skipped"],
        completed=counts["completed"],
        expired=counts["expired"],
        invalid=counts["invalid"] + invalid_zones,
        rezoned=rezoned,
        digests=digests,
    )


async def _advance(
    session: AsyncSession,
    automation: Automation,
    rule: ScheduleRule,
    spec: ScheduleSpec,
    now: datetime,
    claimed: list[DueOccurrence],
    counts: Counter[str],
) -> datetime | None:
    """Record this automation's missed and due occurrences up to `now`:
    obsolete ones `skipped` (the first `MAX_SKIPPED_ROWS` recorded, the
    rest jumped over), then at most one `claimed` (late or on time). Returns
    the next occurrence still to come, or None when the schedule is done."""
    max_late = timedelta(minutes=rule.max_lateness_minutes)
    tolerance = max(GRACE, max_late)
    occurrence: datetime | None = automation.next_run_at
    recorded = 0
    while occurrence is not None and occurrence <= now:
        verdict = lateness(occurrence, now, grace=GRACE, max_lateness=max_late)
        if verdict is not Lateness.SKIP:
            note = Note.LATE if verdict is Lateness.LATE else None
            if await _record(session, automation, occurrence, "claimed", note, now):
                claimed.append(DueOccurrence(automation.id, automation.account_id, occurrence))
            return next_occurrence(spec, after=occurrence)
        if recorded < MAX_SKIPPED_ROWS:
            if await _record(
                session, automation, occurrence, "skipped", Note.SKIPPED_OBSOLETE, now
            ):
                counts["skipped"] += 1
            recorded += 1
            occurrence = next_occurrence(spec, after=occurrence)
        else:
            # Everything before the tolerance is obsolete too: jump over it.
            occurrence = next_occurrence(spec, after=max(occurrence, now - tolerance))
    return occurrence


async def _follow_owner_timezones(session: AsyncSession, now: datetime) -> tuple[int, int]:
    """Follow-owner automations move with `accounts.timezone`: the local
    rule is kept, the next run recomputed from now; `fixed` ones keep their
    instant (ARCHITECTURE §12.4). Returns (moved, paused as invalid)."""
    rows = (
        await session.execute(
            select(Automation, Account.timezone)
            .join(Account, Account.id == Automation.account_id)
            .where(Automation.status == "active")
            .where(Automation.tz_policy == "follow_owner")
            .where(Automation.kind.in_(SCHEDULED_KINDS))
            .where(Automation.next_run_at.is_not(None))
            .where(Automation.timezone != Account.timezone)
            .order_by(Automation.id)
            .with_for_update(of=Automation, skip_locked=True)
        )
    ).all()
    moved = invalid = 0
    for automation, timezone in rows:
        loaded = _load(automation)
        try:
            if loaded is None:
                raise ScheduleError("invalid automation")
            _, spec = loaded
            rezoned = rezone(spec, timezone)
            next_at = next_occurrence(rezoned, after=now) if spec.run_at is None else None
        except ScheduleError:
            _pause_invalid(session, automation, now)
            invalid += 1
            continue
        automation.timezone = timezone
        automation.updated_at = now
        if spec.run_at is None:  # a one-off keeps its instant whatever the policy
            automation.next_run_at = _whole_second(next_at)
            if next_at is None:
                _end(automation, "completed", now)
        moved += 1
    # The session does not autoflush: the due query must see these.
    await session.flush()
    return moved, invalid


# ------------------------------------------------------------ firing
class FireAction(StrEnum):
    #: Not a claimed occurrence (already fired or skipped, or unknown).
    NONE = "none"
    #: Re-checked and not to fire: recorded `skipped` with a note.
    SKIPPED = "skipped"
    #: Defer `digest.build`, then mark the occurrence fired.
    DIGEST = "digest"


@dataclass(frozen=True, slots=True)
class FirePlan:
    action: FireAction
    account_id: int | None = None
    note: Note | None = None


def _instant(occurrence_at: datetime) -> tuple[datetime, datetime]:
    """The ledger rows the job's epoch second names."""
    start = occurrence_at.replace(microsecond=0)
    return start, start + timedelta(seconds=1)


async def plan_fire(
    session: AsyncSession,
    *,
    automation_id: int,
    occurrence_at: datetime,
    digest_enabled: bool,
) -> FirePlan:
    """`automation.fire` steps 1-3 (S05.3b §3.3): load the occurrence under
    a row lock; anything but `claimed` is a no-op (idempotent); re-check the
    automation (still active, or completed by this very occurrence), the
    account and the kind's flag; a kind without a handler in P2 is skipped
    (`no_handler`)."""
    start, end = _instant(occurrence_at)
    row = (
        await session.execute(
            select(AutomationOccurrence, Automation)
            .join(Automation, Automation.id == AutomationOccurrence.automation_id)
            .where(AutomationOccurrence.automation_id == automation_id)
            .where(AutomationOccurrence.account_id == Automation.account_id)
            .where(AutomationOccurrence.occurrence_at >= start)
            .where(AutomationOccurrence.occurrence_at < end)
            .with_for_update(of=AutomationOccurrence)
        )
    ).first()
    if row is None:
        return FirePlan(FireAction.NONE)
    occurrence, automation = row
    if occurrence.status != "claimed":
        return FirePlan(FireAction.NONE, automation.account_id)
    account_status = await session.scalar(
        select(Account.status).where(Account.id == automation.account_id)
    )
    note: Note | None = None
    if account_status != "active" or not await _still_runs(session, automation, occurrence):
        note = Note.INACTIVE
    elif automation.kind != DIGEST_KIND:
        note = Note.NO_HANDLER
    elif not digest_enabled:
        note = Note.DISABLED
    if note is not None:
        occurrence.status = "skipped"
        occurrence.note_code = note.value
        await session.flush()
        return FirePlan(FireAction.SKIPPED, automation.account_id, note)
    return FirePlan(FireAction.DIGEST, automation.account_id)


async def _still_runs(
    session: AsyncSession, automation: Automation, occurrence: AutomationOccurrence
) -> bool:
    if automation.status == "active":
        return True
    if automation.status != "completed":
        return False
    later = await session.scalar(
        select(AutomationOccurrence.id)
        .where(AutomationOccurrence.automation_id == automation.id)
        .where(AutomationOccurrence.occurrence_at > occurrence.occurrence_at)
        .limit(1)
    )
    return later is None


async def mark_fired(
    session: AsyncSession, *, automation_id: int, occurrence_at: datetime, now: datetime
) -> bool:
    """`automation.fire` step 4: `claimed → fired`, once (conditional)."""
    start, end = _instant(occurrence_at)
    changed = await session.execute(
        update(AutomationOccurrence)
        .where(AutomationOccurrence.automation_id == automation_id)
        .where(AutomationOccurrence.occurrence_at >= start)
        .where(AutomationOccurrence.occurrence_at < end)
        .where(AutomationOccurrence.status == "claimed")
        .values(status="fired", fired_at=now)
        .returning(AutomationOccurrence.id)
    )
    return changed.first() is not None


async def runs_late(
    session: AsyncSession, *, account_id: int, kind: str, occurrence_at: datetime, now: datetime
) -> bool:
    """Whether `occurrence_at` is a scheduled occurrence of one of this
    account's `kind` automations that is being acted on late: claimed with
    the `late` note, or acted on more than the grace after its time. The
    owner is told (FR-FUP-08: "fires late (with a note)")."""
    row = (
        await session.execute(
            select(AutomationOccurrence.note_code)
            .join(Automation, Automation.id == AutomationOccurrence.automation_id)
            .where(AutomationOccurrence.account_id == account_id)
            .where(Automation.account_id == account_id)
            .where(Automation.kind == kind)
            .where(AutomationOccurrence.occurrence_at == occurrence_at)
            .where(AutomationOccurrence.status.in_(("claimed", "fired")))
            .limit(1)
        )
    ).first()
    if row is None:
        return False  # not a scheduled occurrence (e.g. `/digest`)
    return row.note_code == Note.LATE.value or now - occurrence_at > GRACE


# ------------------------------------------------------------ restores
async def skip_for_restore(session: AsyncSession, now: datetime) -> dict[int, int]:
    """Post-restore step 4 (ARCHITECTURE §15.6; S05.3b §3.7), in the
    post-restore transaction: nothing from a rewound backup fires by itself.
    A `claimed` occurrence (its job is gone, and it may have fired after the
    dump) and every missed occurrence of an active automation (at most
    `MAX_SKIPPED_ROWS` each) are recorded `skipped_restore`; `next_run_at`
    is recomputed from now. Returns the skipped count per account."""
    skipped: Counter[int] = Counter()
    interrupted = await session.execute(
        update(AutomationOccurrence)
        .where(AutomationOccurrence.status == "claimed")
        .values(status="skipped", note_code=Note.SKIPPED_RESTORE.value)
        .returning(AutomationOccurrence.account_id)
    )
    for (account_id,) in interrupted.all():
        skipped[int(account_id)] += 1
    rows = (
        (
            await session.execute(
                select(Automation)
                .where(Automation.status == "active")
                .where(Automation.kind.in_(SCHEDULED_KINDS))
                .where(Automation.next_run_at.is_not(None))
                .where(Automation.next_run_at < now)
                .order_by(Automation.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for automation in rows:
        loaded = _load(automation)
        if loaded is None:
            _pause_invalid(session, automation, now)
            continue
        _, spec = loaded
        occurrence: datetime | None = automation.next_run_at
        recorded = 0
        while occurrence is not None and occurrence < now and recorded < MAX_SKIPPED_ROWS:
            if await _record(session, automation, occurrence, "skipped", Note.SKIPPED_RESTORE, now):
                skipped[automation.account_id] += 1
            recorded += 1
            occurrence = next_occurrence(spec, after=occurrence)
        following = next_occurrence(spec, after=now)
        automation.next_run_at = _whole_second(following)
        automation.updated_at = now
        if following is None:
            _end(automation, "completed", now)
    await session.flush()
    return dict(skipped)
