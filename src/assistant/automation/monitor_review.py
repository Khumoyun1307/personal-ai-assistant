"""Semantic-monitor review coverage (C7, I4): each monitor's own cursor.

A semantic monitor's cursor is a statement that every retained contact
message it can count, up to the cursor, was examined by the model (or was
gone before any review reached it, which is recorded as a gap). It moves only
over messages a call actually read, oldest first, and never to the newest
sampled id past a backlog the call left out. One row per monitor and chat in
`review_progress` (`reader = 'monitor'`); a failure of a batch is counted on
the row, retried a bounded number of times with backoff, and then left
`degraded` with the unreviewed range kept, which the owner's monitor list
shows (R3-09). Nothing here is a model verdict: the row only says how far the
review got and why it stopped.

All writes happen in the caller's transaction, under the account `FOR SHARE`
and conversation `FOR UPDATE` locks (ARCHITECTURE §9.4), never across a model
call. Structural only: ids, counters, fixed enums, no message text.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.automation.models import Automation
from assistant.conversations.models import Contact, Conversation, Message
from assistant.inbox.models import ConversationTriage, ReviewProgress

READER_MONITOR = "monitor"
SCOPE_ROLLING = "rolling"
#: Attempts at one batch before it is left degraded (C7).
MAX_ATTEMPTS = 3
#: The wait before the next attempt, by the number of failed attempts so far.
RETRY_DELAYS = (timedelta(seconds=60), timedelta(seconds=300))
#: A budget hold: the monthly budget does not move within the hour.
BUDGET_RETRY = timedelta(hours=1)
#: Intervals kept per monitor before they collapse into one wider range.
MAX_PENDING_RANGES = 64
#: A monitor's review metadata outlives its end by this long (C7 retention).
ENDED_RETENTION = timedelta(days=365)

Id = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
Revision = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
RangeReason = Literal[
    "backlog",
    "partial_text",
    "source_changed",
    "deleted",
    "retention",
    "provider_failed",
    "invalid_output",
    "budget",
    "no_consent",
]
#: The reasons a batch is unreviewed because the call did not settle: work to
#: do again, replaced by the next failure and cleared by a success.
FAILURE_REASONS: frozenset[str] = frozenset({"provider_failed", "invalid_output", "source_changed"})
#: Gaps are data the bot lost, not work left to do: a later successful review
#: never clears them (removing message rows cannot erase a missed-review warning).
GAP_REASONS: frozenset[str] = frozenset({"deleted", "retention", "partial_text"})

FailureReason = Literal["provider_failed", "invalid_output", "source_changed"]
FailureError = Literal[
    "refusal", "truncated", "invalid_output", "provider_failed", "source_changed"
]


class ReviewPosition(BaseModel):
    """`message_id` is the newest message completely examined; `next_char` is
    the first unexamined offset inside it (0 for a whole-message reader; I6
    owns partial positions)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: Id
    next_char: Annotated[int, Field(strict=True, ge=0)] = 0
    content_revision: Revision


class ReviewRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    first_message_id: Id
    last_message_id: Id
    reason: RangeReason


def position_of(row: ReviewProgress | None) -> int:
    """The newest message id the monitor has examined; 0 when none.

    Corrupt state is unproven coverage, never a cursor: it reads as no
    position and the monitor re-reads from its confirmation."""
    if row is None or row.position is None:
        return 0
    try:
        return ReviewPosition.model_validate(row.position).message_id
    except ValidationError:
        return 0


def ranges_of(row: ReviewProgress) -> list[ReviewRange]:
    found: list[ReviewRange] = []
    for item in row.pending_ranges or ():
        try:
            found.append(ReviewRange.model_validate(item))
        except ValidationError:
            continue
    return found


def merge_ranges(ranges: Sequence[ReviewRange]) -> list[ReviewRange]:
    """Union overlapping or adjacent ranges of one reason; over the bound, widen.

    Widening only ever grows what counts as unreviewed, so no work is lost."""
    merged: list[ReviewRange] = []
    for reason in sorted({r.reason for r in ranges}):
        run: ReviewRange | None = None
        for item in sorted(
            (r for r in ranges if r.reason == reason), key=lambda r: r.first_message_id
        ):
            if run is not None and item.first_message_id <= run.last_message_id + 1:
                run = ReviewRange(
                    first_message_id=run.first_message_id,
                    last_message_id=max(run.last_message_id, item.last_message_id),
                    reason=reason,
                )
                continue
            if run is not None:
                merged.append(run)
            run = item
        if run is not None:
            merged.append(run)
    if len(merged) <= MAX_PENDING_RANGES:
        return sorted(merged, key=lambda r: (r.first_message_id, r.reason))
    by_reason: dict[str, ReviewRange] = {}
    for item in merged:
        wide = by_reason.get(item.reason)
        by_reason[item.reason] = (
            item
            if wide is None
            else ReviewRange(
                first_message_id=min(wide.first_message_id, item.first_message_id),
                last_message_id=max(wide.last_message_id, item.last_message_id),
                reason=item.reason,
            )
        )
    return sorted(by_reason.values(), key=lambda r: (r.first_message_id, r.reason))


# ------------------------------------------------------------------ rows
async def lock_rows(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    monitor_ids: Collection[int],
) -> dict[int, ReviewProgress]:
    """The monitors' rows for this chat FOR UPDATE, by monitor id."""
    if not monitor_ids:
        return {}
    rows = (
        await session.execute(
            select(ReviewProgress)
            .where(ReviewProgress.account_id == account_id)
            .where(ReviewProgress.conversation_id == conversation_id)
            .where(ReviewProgress.reader == READER_MONITOR)
            .where(ReviewProgress.scope_key == SCOPE_ROLLING)
            .where(ReviewProgress.monitor_id.in_(sorted(monitor_ids)))
            .order_by(ReviewProgress.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars()
    return {int(row.monitor_id): row for row in rows if row.monitor_id is not None}


async def ensure_rows(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    monitor_ids: Collection[int],
    now: datetime,
) -> dict[int, ReviewProgress]:
    """`lock_rows`, creating the missing ones empty. The caller holds the
    conversation lock, which is what makes creation race-free."""
    rows = await lock_rows(
        session, account_id=account_id, conversation_id=conversation_id, monitor_ids=monitor_ids
    )
    created = False
    for monitor_id in sorted(set(monitor_ids) - set(rows)):
        row = ReviewProgress(
            account_id=account_id,
            conversation_id=conversation_id,
            reader=READER_MONITOR,
            monitor_id=monitor_id,
            scope_key=SCOPE_ROLLING,
            version=1,
            source_revision=0,
            pending_ranges=[],
            status="pending",
            attempts=0,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        rows[monitor_id] = row
        created = True
    if created:
        await session.flush()
    return rows


# ------------------------------------------------------------------ eligibility
@dataclass(frozen=True, slots=True)
class Eligibility:
    """Whether a monitor with unreviewed messages may be read now."""

    ready: bool
    #: Waiting for the backoff: when the next attempt is allowed.
    retry_at: datetime | None = None
    #: Attempts exhausted on this very batch: it waits for an explicit retry.
    degraded: bool = False


def eligibility(row: ReviewProgress | None, *, first_pending: int, now: datetime) -> Eligibility:
    """A monitor is read when it has no row yet, or its backoff has passed;
    a batch that exhausted its attempts is not read again until an explicit
    retry — or until its first unreviewed message is no longer the one that
    failed (deleted, expired), which is new work."""
    if row is None:
        return Eligibility(ready=True)
    if row.retry_at is not None and row.retry_at > now:
        return Eligibility(ready=False, retry_at=row.retry_at)
    if row.status == "degraded":
        failed = {r.first_message_id for r in ranges_of(row) if r.reason in FAILURE_REASONS}
        if first_pending in failed:
            return Eligibility(ready=False, degraded=True)
    return Eligibility(ready=True)


# ------------------------------------------------------------------ settlement
def _status(pending: Sequence[ReviewRange], *, backlog: bool) -> str:
    if any(r.reason in FAILURE_REASONS for r in pending):
        return "pending"
    if pending:
        return "partial"
    return "pending" if backlog else "current"


async def advance(
    session: AsyncSession,
    row: ReviewProgress,
    *,
    through: int,
    snapshot_revision: int,
    lost: Sequence[ReviewRange],
    backlog: bool,
    now: datetime,
) -> None:
    """Move the cursor to `through` (the caller verified the row's version and
    the source). Failure ranges up to `through` are settled; gaps stay."""
    kept = [r for r in ranges_of(row) if r.reason in GAP_REASONS or r.last_message_id > through]
    pending = merge_ranges([*kept, *lost])
    row.position = ReviewPosition(
        message_id=through, content_revision=snapshot_revision
    ).model_dump(mode="json")
    row.source_revision = snapshot_revision
    row.pending_ranges = [r.model_dump(mode="json") for r in pending]
    row.attempts = 0
    row.retry_at = None
    row.last_error = None
    row.version += 1
    row.status = _status(pending, backlog=backlog)
    row.updated_at = now
    await session.flush()


async def record_failure(
    session: AsyncSession,
    row: ReviewProgress,
    *,
    first_message_id: int,
    last_message_id: int,
    reason: FailureReason,
    last_error: FailureError,
    now: datetime,
) -> bool:
    """One more failed attempt at the batch. No review movement. The batch is
    kept as unreviewed work; after `MAX_ATTEMPTS` the monitor is `degraded`
    and not read again until retried; before that the next attempt waits for a
    short backoff (a bounded trigger, never a paid hot loop), a stale source
    excepted (the answer is simply recomputed). `last_error` is a fixed value,
    never provider text. Returns True when this exhausted the attempts."""
    ranges = ranges_of(row)
    failed_before = {r.first_message_id for r in ranges if r.reason in FAILURE_REASONS}
    if first_message_id not in failed_before:
        row.attempts = 0  # a different batch: its own count
    row.attempts += 1
    others = [r for r in ranges if r.reason not in FAILURE_REASONS]
    pending = merge_ranges(
        [
            *others,
            ReviewRange(
                first_message_id=first_message_id, last_message_id=last_message_id, reason=reason
            ),
        ]
    )
    row.pending_ranges = [r.model_dump(mode="json") for r in pending]
    row.last_error = last_error
    exhausted = row.attempts >= MAX_ATTEMPTS
    if exhausted:
        row.status = "degraded"
        row.retry_at = None
    else:
        row.status = "pending"
        row.retry_at = None if reason == "source_changed" else now + RETRY_DELAYS[row.attempts - 1]
    row.version += 1
    row.updated_at = now
    await session.flush()
    return exhausted


async def hold(
    session: AsyncSession,
    row: ReviewProgress,
    *,
    reason: Literal["budget", "no_consent"],
    retry_at: datetime | None,
    now: datetime,
) -> None:
    """The review cannot run now (no consent, over budget): the unreviewed
    messages stay unreviewed and the row says why. Not an attempt."""
    row.last_error = reason
    row.status = "blocked" if reason == "no_consent" else "pending"
    row.retry_at = retry_at
    row.version += 1
    row.updated_at = now
    await session.flush()


async def request_retry(
    session: AsyncSession,
    *,
    account_id: int,
    now: datetime,
    monitor_id: int | None = None,
    conversation_id: int | None = None,
) -> list[int]:
    """The explicit recovery of exhausted work: degraded (or backing-off) rows
    get a fresh set of attempts, their failed ranges are dropped (the next run
    re-reads from the cursor) and their chats are flagged for evaluation.
    Returns the chats to queue. Gaps are kept."""
    scope = [
        ReviewProgress.account_id == account_id,
        ReviewProgress.reader == READER_MONITOR,
        ReviewProgress.status.in_(("degraded", "pending", "blocked")),
    ]
    if monitor_id is not None:
        scope.append(ReviewProgress.monitor_id == monitor_id)
    if conversation_id is not None:
        scope.append(ReviewProgress.conversation_id == conversation_id)
    rows = (await session.execute(select(ReviewProgress).where(*scope).with_for_update())).scalars()
    chats: set[int] = set()
    for row in rows.all():
        kept = [r for r in ranges_of(row) if r.reason not in FAILURE_REASONS]
        row.pending_ranges = [r.model_dump(mode="json") for r in kept]
        row.attempts = 0
        row.retry_at = None
        row.last_error = None
        row.status = "partial" if kept else "pending"
        row.version += 1
        row.updated_at = now
        chats.add(row.conversation_id)
    if chats:
        await session.execute(
            update(Conversation)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id.in_(sorted(chats)))
            .where(Conversation.pending_monitor_at.is_(None))
            .values(pending_monitor_at=now)
        )
    await session.flush()
    return sorted(chats)


# ------------------------------------------------------------------ source lookups
def contact_messages(account_id: int, conversation_id: int) -> list[Any]:
    """The retained contact messages a monitor can read: this chat's, with
    content, not deleted or expired."""
    return [
        Message.account_id == account_id,
        Message.conversation_id == conversation_id,
        Message.direction == "contact",
        Message.deleted_at.is_(None),
        Message.purged_at.is_(None),
        Message.sent_at.is_not(None),
        (Message.text.is_not(None)) | (Message.media.is_not(None)),
    ]


def counted_from(confirmed_at: datetime | None) -> datetime | None:
    """A message counts from the monitor's confirmation on, at Telegram's
    one-second precision (`MonitorView.covers`)."""
    return None if confirmed_at is None else confirmed_at.replace(microsecond=0)


async def first_pending(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    after: int,
    confirmed_at: datetime | None,
) -> int | None:
    """The oldest retained contact message after the cursor that the monitor
    counts; None when there is none (or it was never confirmed)."""
    floor = counted_from(confirmed_at)
    if floor is None:
        return None
    found = await session.scalar(
        select(func.min(Message.id))
        .where(*contact_messages(account_id, conversation_id))
        .where(Message.id > after)
        .where(Message.sent_at >= floor)
    )
    return None if found is None else int(found)


async def lost_ranges(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    after: int,
    through: int | None,
    confirmed_at: datetime | None,
) -> list[ReviewRange]:
    """Contact messages after the cursor (up to `through`, when given) that
    were gone before any review reached them: expired by retention or deleted.
    The cursor passes over them and the loss stays on record instead of
    reading as examined."""
    floor = counted_from(confirmed_at)
    if floor is None:
        return []
    query = (
        select(Message.id, Message.purged_at.is_not(None))
        .where(Message.account_id == account_id)
        .where(Message.conversation_id == conversation_id)
        .where(Message.direction == "contact")
        .where(Message.id > after)
        .where((Message.purged_at.is_not(None)) | (Message.deleted_at.is_not(None)))
        .where(or_(Message.sent_at.is_(None), Message.sent_at >= floor))
        .order_by(Message.id)
    )
    if through is not None:
        query = query.where(Message.id <= through)
    rows = (await session.execute(query)).all()
    return merge_ranges(
        [
            ReviewRange(
                first_message_id=int(mid),
                last_message_id=int(mid),
                reason="retention" if purged else "deleted",
            )
            for mid, purged in rows
        ]
    )


# ------------------------------------------------------------------ owner-visible state
@dataclass(frozen=True, slots=True)
class MonitorReview:
    """How a monitor's review stands, for the owner's monitor list: fixed words
    and enums only. `current` — everything counted was reviewed; `delayed` — a
    review attempt failed and will be repeated; `degraded` — the attempts ran
    out and some messages are unreviewed until retried; `gap` — messages were
    deleted or expired before they were reviewed; `blocked` — no AI consent."""

    state: Literal["current", "delayed", "degraded", "gap", "blocked"]
    reasons: tuple[str, ...] = ()


async def states(
    session: AsyncSession, *, account_id: int, monitor_ids: Collection[int], now: datetime
) -> dict[int, MonitorReview]:
    """The review state of each listed monitor, worst across its chats."""
    if not monitor_ids:
        return {}
    rows = (
        await session.execute(
            select(ReviewProgress)
            .where(ReviewProgress.account_id == account_id)
            .where(ReviewProgress.reader == READER_MONITOR)
            .where(ReviewProgress.monitor_id.in_(sorted(monitor_ids)))
        )
    ).scalars()
    order = ("current", "gap", "delayed", "blocked", "degraded")
    found: dict[int, MonitorReview] = {int(i): MonitorReview("current") for i in monitor_ids}
    for row in rows.all():
        assert row.monitor_id is not None
        reasons: set[str] = {r.reason for r in ranges_of(row)}
        if row.last_error is not None and row.status != "current":
            reasons.add(row.last_error)
        state: Literal["current", "delayed", "degraded", "gap", "blocked"]
        if row.status == "degraded":
            state = "degraded"
        elif row.status == "blocked":
            state = "blocked"
        elif (row.retry_at is not None and row.retry_at > now) or reasons & FAILURE_REASONS:
            state = "delayed"
        elif reasons & GAP_REASONS:
            state = "gap"
        else:
            state = "current"
        known = found[int(row.monitor_id)]
        if order.index(state) >= order.index(known.state):
            found[int(row.monitor_id)] = MonitorReview(
                state, tuple(sorted({*known.reasons, *reasons}))
            )
    return found


# ------------------------------------------------------------------ deletion and sweep
async def delete_rows(session: AsyncSession, *, account_id: int, ids: Sequence[int]) -> int:
    """Remove progress rows, first clearing BOTH `review_progress_id` and
    `review_version` on retained verdicts (ON DELETE SET NULL would clear only
    the id; the database trigger is the backstop for other paths). Returns the
    number of rows removed."""
    if not ids:
        return 0
    removed = 0
    for start in range(0, len(ids), 500):
        chunk = list(ids[start : start + 500])
        await session.execute(
            update(ConversationTriage)
            .where(ConversationTriage.account_id == account_id)
            .where(ConversationTriage.review_progress_id.in_(chunk))
            .values(review_progress_id=None, review_version=None)
        )
        result = await session.execute(
            delete(ReviewProgress).where(
                ReviewProgress.account_id == account_id, ReviewProgress.id.in_(chunk)
            )
        )
        removed += int(getattr(result, "rowcount", 0) or 0)
    return removed


async def delete_monitor_progress(
    session: AsyncSession, *, account_id: int, conversation_ids: Collection[int] | None = None
) -> int:
    """Managed or scoped data deletion: the monitors' cursors name deleted
    messages, so they are removed (a monitor starts afresh on what arrives)."""
    scope = [ReviewProgress.account_id == account_id, ReviewProgress.reader == READER_MONITOR]
    if conversation_ids is not None:
        scope.append(ReviewProgress.conversation_id.in_(sorted(conversation_ids)))
    ids = list((await session.execute(select(ReviewProgress.id).where(*scope))).scalars())
    return await delete_rows(session, account_id=account_id, ids=ids)


async def sweep(session: AsyncSession, *, now: datetime) -> int:
    """Hourly (with the monitor expiry): the review metadata of monitors that
    ended more than a year ago and of chats no monitor reads any more (Ignore
    mode, bot chats) is dropped; a verdict stays, with both linkage fields
    cleared atomically. Orphaned means out of scope, not lost coverage."""
    cutoff = now - ENDED_RETENTION
    rows = (
        await session.execute(
            select(ReviewProgress.account_id, ReviewProgress.id)
            .join(Automation, Automation.id == ReviewProgress.monitor_id)
            .join(Conversation, Conversation.id == ReviewProgress.conversation_id)
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(ReviewProgress.reader == READER_MONITOR)
            .where(
                or_(
                    (Automation.status.not_in(("active", "paused")))
                    & (func.coalesce(Automation.ended_at, Automation.updated_at) < cutoff),
                    Conversation.mode == "ignore",
                    Contact.is_bot.is_(True),
                )
            )
            .order_by(ReviewProgress.account_id, ReviewProgress.id)
        )
    ).all()
    by_account: dict[int, list[int]] = {}
    for account_id, progress_id in rows:
        by_account.setdefault(int(account_id), []).append(int(progress_id))
    removed = 0
    for account_id, ids in by_account.items():
        removed += await delete_rows(session, account_id=account_id, ids=ids)
    return removed
