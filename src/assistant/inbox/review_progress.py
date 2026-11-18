"""Contiguous review coverage: the cursor and its settlement (C7, I2).

A reader's cursor is a statement that every retained message up to it was
examined. It may only move over messages the reader actually looked at (or
deterministically decided had nothing to say), never to the newest sampled ID
past a backlog it left out. All writes happen in the caller's transaction,
under the conversation lock (ARCHITECTURE §9.4), never across a model call.

Structural only: ids, offsets, counters and fixed enums, no message text.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.conversations.models import Contact, Conversation, Message
from assistant.inbox.models import ConversationTriage, ReviewProgress
from assistant.memory.summaries import StoreResult
from assistant.memory.summaries import _changed_source as changed_source
from assistant.memory.summaries import _retained as retained

READER_TRIAGE = "triage"
SCOPE_ROLLING = "rolling"
#: Attempts at one batch before the reader reports degraded (C7).
MAX_ATTEMPTS = 3
#: Intervals kept per reader before they collapse into one wider range.
MAX_PENDING_RANGES = 64
#: Single messages skipped in a row before the review degrades instead.
MAX_SKIPS = 3

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
    "unproven",
    "unexaminable",
]
#: Gaps are data the bot lost or could not examine, not work left to do: a later
#: successful review never clears them (removing message rows cannot erase a
#: missed-review warning). `unproven` = coverage no cursor of ours established
#: (before a first review's window, or a legacy verdict's claim); `unexaminable`
#: = a single message that kept failing the reader and was skipped (C7 extension).
GAP_REASONS: frozenset[str] = frozenset(
    {"deleted", "retention", "partial_text", "unproven", "unexaminable"}
)


class ReviewPosition(BaseModel):
    """`message_id` is the newest message completely examined. `next_char` is the
    first unexamined offset inside it; a whole-message reader keeps 0 and I6
    owns partial positions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: Id
    next_char: Annotated[int, Field(strict=True, ge=0)] = 0
    content_revision: Revision


class ReviewRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    first_message_id: Id
    last_message_id: Id
    reason: RangeReason


class Settlement(StrEnum):
    APPLIED = "applied"
    #: Another settlement covered as far or further: it stands.
    RACED = "raced"
    #: The progress row changed (or appeared) since the batch was read.
    STALE = "stale"


class SourceState(StrEnum):
    CURRENT = "current"
    CHANGED = "changed"
    EXPIRED = "expired"


def position_of(row: ReviewProgress | None) -> ReviewPosition | None:
    if row is None or row.position is None:
        return None
    try:
        return ReviewPosition.model_validate(row.position)
    except ValidationError:
        return None  # corrupt state is unproven coverage, never a cursor


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


async def get(
    session: AsyncSession, *, account_id: int, conversation_id: int, reader: str = READER_TRIAGE
) -> ReviewProgress | None:
    return (
        await session.execute(
            select(ReviewProgress)
            .where(ReviewProgress.account_id == account_id)
            .where(ReviewProgress.conversation_id == conversation_id)
            .where(ReviewProgress.reader == reader)
            .where(ReviewProgress.monitor_id.is_(None))
            .where(ReviewProgress.scope_key == SCOPE_ROLLING)
        )
    ).scalar_one_or_none()


async def lock(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    reader: str = READER_TRIAGE,
    now: datetime,
    create: bool,
) -> ReviewProgress | None:
    """The row FOR UPDATE; created empty when `create`. The caller holds the
    conversation lock, which is what makes creation race-free."""
    row = (
        await session.execute(
            select(ReviewProgress)
            .where(ReviewProgress.account_id == account_id)
            .where(ReviewProgress.conversation_id == conversation_id)
            .where(ReviewProgress.reader == reader)
            .where(ReviewProgress.monitor_id.is_(None))
            .where(ReviewProgress.scope_key == SCOPE_ROLLING)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None and create:
        row = ReviewProgress(
            account_id=account_id,
            conversation_id=conversation_id,
            reader=reader,
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
        await session.flush()
    return row


async def source_state(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    shown: dict[int, str | None],
    contiguous_ids: Collection[int],
    after: int,
    through: int,
    snapshot_revision: int,
    current_revision: int,
) -> SourceState:
    """Is what the model read still what is there? Call under the conversation lock.

    Every shown message must still be retained with the same hash. When the
    conversation's content revision moved during the call, the batch must also
    still be exactly the retained messages in `(after, through]`: a message that
    gained content inside the range (an edit of a placeholder) was never read,
    and certifying the range would cover it unexamined. New arrivals after
    `through` change the revision but only add messages beyond the batch."""
    changed = await changed_source(session, account_id, conversation_id, shown)
    if changed is not None:
        return SourceState.EXPIRED if changed is StoreResult.EXPIRED else SourceState.CHANGED
    if current_revision == snapshot_revision:
        return SourceState.CURRENT
    ids = (
        await session.execute(
            select(Message.id)
            .where(*retained(account_id, conversation_id))
            .where(Message.id > after)
            .where(Message.id <= through)
        )
    ).scalars()
    if set(ids) != set(contiguous_ids):
        return SourceState.CHANGED
    return SourceState.CURRENT


async def lost_ranges(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    after: int,
    through: int,
) -> list[ReviewRange]:
    """Messages inside `(after, through]` that were gone before any review
    reached them: expired by retention or deleted. The cursor passes over them,
    and the loss stays on record instead of reading as examined."""
    rows = (
        await session.execute(
            select(Message.id, Message.purged_at.is_not(None))
            .where(Message.account_id == account_id)
            .where(Message.conversation_id == conversation_id)
            .where(Message.id > after)
            .where(Message.id <= through)
            .where((Message.purged_at.is_not(None)) | (Message.deleted_at.is_not(None)))
            .order_by(Message.id)
        )
    ).all()
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


async def unproven_range(
    session: AsyncSession, *, account_id: int, conversation_id: int, before_or_at: int
) -> ReviewRange | None:
    """Messages at or before `before_or_at` that no cursor of ours ever covered:
    those before a first review's window, or what a legacy verdict claimed
    without progress metadata (C7: NULL legacy metadata is unproven coverage).
    Recorded, not re-read in this unit, so the status is never `current` on
    their account."""
    oldest = (
        await session.execute(
            select(func.min(Message.id))
            .where(Message.account_id == account_id)
            .where(Message.conversation_id == conversation_id)
            .where(Message.id <= before_or_at)
        )
    ).scalar_one_or_none()
    if oldest is None:
        return None
    return ReviewRange(first_message_id=oldest, last_message_id=before_or_at, reason="unproven")


async def advance(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    expected_version: int | None,
    after: int,
    through: int,
    snapshot_revision: int,
    current_revision: int,
    lost: Sequence[ReviewRange],
    backlog: bool,
    batch_cap: int,
    now: datetime,
) -> tuple[Settlement, ReviewProgress | None]:
    """Move the cursor to `through` if nothing else moved it since the batch was
    read (compare-and-swap on `version`). The caller has verified the source."""
    row = await lock(
        session, account_id=account_id, conversation_id=conversation_id, now=now, create=False
    )
    if (row is None) != (expected_version is None) or (
        row is not None and row.version != expected_version
    ):
        current = position_of(row)
        raced = current is not None and current.message_id >= through
        return (Settlement.RACED if raced else Settlement.STALE), None
    if row is None:
        row = await lock(
            session, account_id=account_id, conversation_id=conversation_id, now=now, create=True
        )
        assert row is not None
    previous = position_of(row)
    if previous is not None and previous.message_id >= through:
        return Settlement.RACED, None
    baseline: list[ReviewRange] = []
    if previous is None:
        found = await unproven_range(
            session, account_id=account_id, conversation_id=conversation_id, before_or_at=after
        )
        baseline = [] if found is None else [found]
    kept = [r for r in ranges_of(row) if r.reason in GAP_REASONS or r.last_message_id > through]
    pending = merge_ranges([*kept, *lost, *baseline])
    row.position = ReviewPosition(
        message_id=through, content_revision=snapshot_revision
    ).model_dump(mode="json")
    row.source_revision = snapshot_revision
    row.pending_ranges = [r.model_dump(mode="json") for r in pending]
    row.attempts = 0
    row.failed_first_message_id = row.failed_last_message_id = None
    row.skip_streak = 0
    if row.batch_limit is not None:  # grow back toward the default as batches succeed
        grown = row.batch_limit * 2
        row.batch_limit = None if grown >= batch_cap else grown
    row.retry_at = None
    row.last_error = None
    row.version += 1
    row.status = _status(pending, backlog=backlog, stale=current_revision != snapshot_revision)
    row.updated_at = now
    await session.flush()
    return Settlement.APPLIED, row


def _status(pending: Sequence[ReviewRange], *, backlog: bool, stale: bool) -> str:
    if pending:
        return "partial"
    return "pending" if backlog or stale else "current"


class FailureOutcome(StrEnum):
    #: Transient (outage, rate limit): a short bounded retry time, never degraded.
    DEFERRED = "deferred"
    #: An attempt at the same batch was counted; the next run tries again.
    COUNTED = "counted"
    #: The batch kept failing: the next one is half the size.
    HALVED = "halved"
    #: One message kept failing alone: recorded as an unexaminable gap, cursor
    #: moved past it so the rest of the chat is reviewed.
    SKIPPED = "skipped"
    #: Several messages in a row failed alone (a context problem, not a message
    #: problem): degraded until `retry_at`, nothing more is skipped.
    DEGRADED = "degraded"
    #: Settled or changed meanwhile: the failure no longer describes the state.
    STALE = "stale"


async def record_failure(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    expected_version: int | None,
    after: int,
    first_message_id: int,
    last_message_id: int,
    size: int,
    calls: int,
    repeating: bool,
    reason: Literal["provider_failed", "invalid_output"],
    last_error: str,
    retry_after: timedelta,
    transient_retry: timedelta,
    snapshot_revision: int,
    now: datetime,
) -> tuple[FailureOutcome, ReviewProgress | None]:
    """A run at this batch failed. The cursor moves only by the isolation rule.

    Transient errors (`repeating` False) set a short `retry_at` and nothing
    else. Repeating errors (refusal, invalid output, truncation) count the paid
    `calls` against THIS batch (a different batch starts again at zero). After
    `MAX_ATTEMPTS` the batch is halved; a single message that still fails is
    recorded as an `unexaminable` gap and the cursor passes it; after
    `MAX_SKIPS` such skips in a row the review is degraded instead (a bad
    context would otherwise turn the whole backlog into gaps). `last_error` is
    a fixed value, never provider text. The caller holds the conversation lock
    and has verified the source is unchanged."""
    row = await lock(
        session, account_id=account_id, conversation_id=conversation_id, now=now, create=False
    )
    if not repeating:
        if row is None:
            row = await lock(
                session,
                account_id=account_id,
                conversation_id=conversation_id,
                now=now,
                create=True,
            )
        assert row is not None
        row.retry_at = now + transient_retry
        row.last_error = last_error
        row.updated_at = now
        await session.flush()
        return FailureOutcome.DEFERRED, row
    if (row is None) != (expected_version is None) or (
        row is not None and row.version != expected_version
    ):
        return FailureOutcome.STALE, None
    if row is None:
        row = await lock(
            session, account_id=account_id, conversation_id=conversation_id, now=now, create=True
        )
        assert row is not None
    row.last_error = last_error
    row.updated_at = now
    same = (row.failed_first_message_id, row.failed_last_message_id) == (
        first_message_id,
        last_message_id,
    )
    row.attempts = (row.attempts if same else 0) + max(calls, 1)
    row.failed_first_message_id, row.failed_last_message_id = first_message_id, last_message_id
    if row.attempts < MAX_ATTEMPTS:
        await session.flush()
        return FailureOutcome.COUNTED, row
    if size > 1:
        row.batch_limit = max(1, size // 2)
        row.attempts = 0
        row.failed_first_message_id = row.failed_last_message_id = None
        await session.flush()
        return FailureOutcome.HALVED, row
    failed = ReviewRange(
        first_message_id=first_message_id, last_message_id=last_message_id, reason=reason
    )
    if row.skip_streak >= MAX_SKIPS:
        row.status = "degraded"
        row.retry_at = now + retry_after
        row.pending_ranges = [
            r.model_dump(mode="json") for r in merge_ranges([*ranges_of(row), failed])
        ]
        await session.flush()
        return FailureOutcome.DEGRADED, row
    previous = position_of(row)
    baseline: list[ReviewRange] = []
    if previous is None:
        found = await unproven_range(
            session, account_id=account_id, conversation_id=conversation_id, before_or_at=after
        )
        baseline = [] if found is None else [found]
    skipped = ReviewRange(
        first_message_id=first_message_id,
        last_message_id=last_message_id,
        reason="unexaminable",
    )
    row.pending_ranges = [
        r.model_dump(mode="json") for r in merge_ranges([*ranges_of(row), skipped, *baseline])
    ]
    row.position = ReviewPosition(
        message_id=last_message_id, content_revision=snapshot_revision
    ).model_dump(mode="json")
    row.source_revision = snapshot_revision
    row.skip_streak += 1
    row.attempts = 0
    row.failed_first_message_id = row.failed_last_message_id = None
    row.retry_at = None
    row.version += 1
    row.status = "partial"
    await session.flush()
    return FailureOutcome.SKIPPED, row


async def delete_progress(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_ids: Collection[int] | None = None,
) -> int:
    """Remove progress rows, first clearing BOTH `review_progress_id` and
    `review_version` on retained verdicts (ON DELETE SET NULL would clear only
    the id; the database trigger is the backstop for other paths). Returns the
    number of rows removed."""
    scope = [ReviewProgress.account_id == account_id]
    if conversation_ids is not None:
        scope.append(ReviewProgress.conversation_id.in_(sorted(conversation_ids)))
    ids = list((await session.execute(select(ReviewProgress.id).where(*scope))).scalars())
    if not ids:
        return 0
    await session.execute(
        update(ConversationTriage)
        .where(ConversationTriage.account_id == account_id)
        .where(ConversationTriage.review_progress_id.in_(ids))
        .values(review_progress_id=None, review_version=None)
    )
    removed = 0
    for chunk in range(0, len(ids), 500):
        result = await session.execute(
            delete(ReviewProgress).where(
                ReviewProgress.account_id == account_id,
                ReviewProgress.id.in_(ids[chunk : chunk + 500]),
            )
        )
        removed += int(getattr(result, "rowcount", 0) or 0)
    return removed


async def sweep_orphans(session: AsyncSession, *, account_id: int) -> int:
    """Hourly sweep: cursor metadata of chats no reader reads any more (Ignore
    mode, bot chats) is dropped; the verdict stays, with both linkage fields
    cleared atomically. Orphaned means unreviewed scope, not lost coverage."""
    ids = list(
        (
            await session.execute(
                select(Conversation.id)
                .join(Contact, Contact.id == Conversation.contact_id)
                .join(ReviewProgress, ReviewProgress.conversation_id == Conversation.id)
                .where(Conversation.account_id == account_id)
                .where((Conversation.mode == "ignore") | (Contact.is_bot.is_(True)))
            )
        ).scalars()
    )
    if not ids:
        return 0
    return await delete_progress(session, account_id=account_id, conversation_ids=ids)
