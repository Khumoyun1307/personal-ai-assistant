"""Conversation summaries: storage, refresh timing and invalidation
(ARCHITECTURE §11.1, §12.2; S03.2).

Domain code only — no model calls (those are `agent/summarize.py`). What
lives here:

- **reading the source**: one conversation's retained messages after a
  summary's `covers_through_message_id` (rolling), or within a range
  (period), account-scoped;
- **storing** a rolling summary optimistically (only if nobody refreshed it
  meanwhile) and a period summary as a cache keyed by range and
  `source_digest`; both under the conversation row lock, never across a
  model call (AGENTS §13);
- **the refresh flag** `conversations.pending_ai_since` ("content no AI run
  has seen yet"): cleared only when no message changed since the refresh
  read its source;
- **refresh timing**: a trailing debounce with a maximum wait;
- **deferring** `memory.refresh_summary` after the ingesting transaction
  commits (a crash in between is caught by `system.reconcile_pending`);
- **invalidation**: a deleted message must not live on in a summary
  (PRD §20: no keeping contacts' deleted messages), so a deletion drops the
  conversation's period caches, and its rolling summary when it covered the
  message (rebuilt from what is retained);
- **deletion and retention** helpers for the privacy cascades.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.core.jobs import REFRESH_SUMMARY_TASK, JobScheduler, summary_queueing_lock
from assistant.db.uow import after_commit
from assistant.memory.models import KIND_PERIOD, KIND_ROLLING, ConversationSummary
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

#: Period summaries are catch-up caches, not memory: kept this long (§10.4).
PERIOD_SUMMARY_RETENTION = timedelta(days=30)
#: Refresh after this much quiet in the conversation (M03 D60).
DEFAULT_REFRESH_DEBOUNCE = timedelta(minutes=30)
#: ...but no later than this after the first unseen message (M03 D60).
DEFAULT_REFRESH_MAX_WAIT = timedelta(hours=6)


@dataclass(frozen=True, slots=True)
class SourceMessage:
    """One retained message as a summary sees it."""

    message_id: int
    direction: str
    sent_at: datetime
    content_type: str
    text: str | None
    media_kind: str | None
    edited: bool


@dataclass(frozen=True, slots=True)
class StoredSummary:
    summary_id: int
    conversation_id: int
    kind: str
    covers_through_message_id: int | None
    message_count: int
    summary: Mapping[str, Any]
    period_start: datetime | None
    period_end: datetime | None
    source_digest: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConversationRef:
    """What a job needs to know about a conversation it was given by id."""

    conversation_id: int
    account_id: int
    mode: str
    is_bot: bool
    pending_ai_since: datetime | None


def _stored(row: ConversationSummary) -> StoredSummary:
    return StoredSummary(
        summary_id=row.id,
        conversation_id=row.conversation_id,
        kind=row.kind,
        covers_through_message_id=row.covers_through_message_id,
        message_count=row.message_count,
        summary=dict(row.summary),
        period_start=row.period_start,
        period_end=row.period_end,
        source_digest=row.source_digest,
        updated_at=row.updated_at,
    )


def _source(row: Message) -> SourceMessage:
    return SourceMessage(
        message_id=row.id,
        direction=str(row.direction),
        sent_at=row.sent_at,  # type: ignore[arg-type]  # filtered NOT NULL
        content_type=row.content_type,
        text=row.text,
        media_kind=(row.media or {}).get("kind") if row.media else None,
        edited=row.edited_at is not None,
    )


def _retained(account_id: int, conversation_id: int) -> list[Any]:
    """Messages a summary may read: this conversation's, with content."""
    return [
        Message.account_id == account_id,
        Message.conversation_id == conversation_id,
        Message.deleted_at.is_(None),
        Message.purged_at.is_(None),
        Message.direction.is_not(None),
        Message.sent_at.is_not(None),
        (Message.text.is_not(None)) | (Message.media.is_not(None)),
    ]


# ------------------------------------------------------------------ reading
async def conversation_ref(session: AsyncSession, conversation_id: int) -> ConversationRef | None:
    """The one unscoped lookup: a job carries only the conversation id
    (AGENTS §13), and the account must come from somewhere before scoped
    queries can run. Returns ids and flags, never content."""
    row = (
        await session.execute(
            select(
                Conversation.account_id,
                Conversation.mode,
                Conversation.pending_ai_since,
                Contact.is_bot,
            )
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(Conversation.id == conversation_id)
        )
    ).first()
    if row is None:
        return None
    account_id, mode, pending, is_bot = row
    return ConversationRef(conversation_id, int(account_id), str(mode), bool(is_bot), pending)


async def rolling_summaries(
    session: AsyncSession, account_id: int, conversation_ids: Sequence[int]
) -> dict[int, StoredSummary]:
    if not conversation_ids:
        return {}
    rows = await session.execute(
        select(ConversationSummary)
        .where(ConversationSummary.account_id == account_id)
        .where(ConversationSummary.conversation_id.in_(list(conversation_ids)))
        .where(ConversationSummary.kind == KIND_ROLLING)
    )
    return {row.conversation_id: _stored(row) for row in rows.scalars().all()}


async def rolling_summary(
    session: AsyncSession, account_id: int, conversation_id: int
) -> StoredSummary | None:
    return (await rolling_summaries(session, account_id, [conversation_id])).get(conversation_id)


async def messages_after(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    *,
    after_id: int | None,
    limit: int,
) -> list[SourceMessage]:
    """Retained messages with an id above `after_id`, lowest id first, at most
    `limit` (ids order arrival, so an incremental refresh never skips one)."""
    statement = select(Message).where(*_retained(account_id, conversation_id))
    if after_id is not None:
        statement = statement.where(Message.id > after_id)
    rows = await session.execute(statement.order_by(Message.id).limit(limit))
    return [_source(row) for row in rows.scalars().all()]


async def activity_marker(
    session: AsyncSession, account_id: int, conversation_id: int
) -> datetime | None:
    """When any message of the conversation last changed (new, edited,
    deleted or expired): the refresh flag is cleared only if this did not
    move while the refresh ran."""
    return (
        await session.execute(
            select(func.max(Message.updated_at))
            .where(Message.account_id == account_id)
            .where(Message.conversation_id == conversation_id)
        )
    ).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class PeriodSource:
    #: Oldest first; the newest `limit` of the range when it holds more.
    messages: tuple[SourceMessage, ...]
    #: Every retained message in the range, including those left out.
    total: int
    digest: str | None
    covers_through_message_id: int | None


def source_digest(pairs: Sequence[tuple[int, datetime]]) -> str | None:
    """A hash over (message id, updated_at): any new, edited, deleted or
    expired message in the range changes it. No content goes in."""
    if not pairs:
        return None
    canonical = "|".join(f"{mid}:{moment.isoformat()}" for mid, moment in sorted(pairs))
    return hashlib.sha256(canonical.encode()).hexdigest()[:40]


async def period_source(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    *,
    start: datetime,
    end: datetime,
    limit: int,
) -> PeriodSource:
    """The retained messages of one conversation sent in `[start, end)`."""
    rows = list(
        (
            await session.execute(
                select(Message)
                .where(*_retained(account_id, conversation_id))
                .where(Message.sent_at >= start)
                .where(Message.sent_at < end)
                .order_by(Message.sent_at.desc(), Message.id.desc())
            )
        )
        .scalars()
        .all()
    )
    kept = [_source(row) for row in reversed(rows[:limit])]
    return PeriodSource(
        messages=tuple(kept),
        total=len(rows),
        digest=source_digest([(row.id, row.updated_at) for row in rows]),
        covers_through_message_id=max((row.id for row in rows), default=None),
    )


async def cached_period(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    *,
    start: datetime,
    end: datetime | None,
    digest: str,
) -> StoredSummary | None:
    """A period summary for exactly this range and these messages."""
    row = (
        await session.execute(
            select(ConversationSummary)
            .where(ConversationSummary.account_id == account_id)
            .where(ConversationSummary.conversation_id == conversation_id)
            .where(ConversationSummary.kind == KIND_PERIOD)
            .where(ConversationSummary.period_start == start)
            .where(ConversationSummary.period_end.is_not_distinct_from(end))
            .where(ConversationSummary.source_digest == digest)
        )
    ).scalar_one_or_none()
    return None if row is None else _stored(row)


# ------------------------------------------------------------------ writing
async def lock_conversation(
    session: AsyncSession, account_id: int, conversation_id: int
) -> Conversation | None:
    """The conversation row, locked (§9.4 lock order: conversation first)."""
    return (
        await session.execute(
            select(Conversation)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id == conversation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def store_rolling(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    based_on: int | None,
    summary: Mapping[str, Any],
    covers_through: int,
    added_messages: int,
    prompt_version: str,
    model: str,
    now: datetime,
) -> bool:
    """Write the new rolling summary if the stored one is still the one it
    was built on (`based_on` = its `covers_through_message_id`, None for
    none). False when another refresh got there first: nothing is written,
    and the caller's work is simply dropped."""
    if await lock_conversation(session, account_id, conversation_id) is None:
        return False
    current = (
        await session.execute(
            select(ConversationSummary)
            .where(ConversationSummary.account_id == account_id)
            .where(ConversationSummary.conversation_id == conversation_id)
            .where(ConversationSummary.kind == KIND_ROLLING)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if (None if current is None else current.covers_through_message_id) != based_on:
        return False
    if current is None:
        session.add(
            ConversationSummary(
                account_id=account_id,
                conversation_id=conversation_id,
                kind=KIND_ROLLING,
                covers_through_message_id=covers_through,
                message_count=added_messages,
                summary=dict(summary),
                prompt_version=prompt_version,
                model=model,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        current.summary = dict(summary)
        current.covers_through_message_id = covers_through
        current.message_count = current.message_count + added_messages
        current.prompt_version = prompt_version
        current.model = model
        current.updated_at = now
    await session.flush()
    return True


async def clear_refresh_flag(
    session: AsyncSession, *, account_id: int, conversation_id: int, marker: datetime | None
) -> bool:
    """Clear `pending_ai_since` — only if no message changed since `marker`
    was read; otherwise the flag stays and the next refresh picks it up."""
    conversation = await lock_conversation(session, account_id, conversation_id)
    if conversation is None or conversation.pending_ai_since is None:
        return False
    if await activity_marker(session, account_id, conversation_id) != marker:
        return False
    conversation.pending_ai_since = None
    return True


async def store_period(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    start: datetime,
    end: datetime | None,
    digest: str,
    summary: Mapping[str, Any],
    covers_through: int | None,
    message_count: int,
    prompt_version: str,
    model: str,
    now: datetime,
) -> None:
    """Cache one period summary, replacing the previous one for the range."""
    if await lock_conversation(session, account_id, conversation_id) is None:
        return
    await session.execute(
        delete(ConversationSummary)
        .where(ConversationSummary.account_id == account_id)
        .where(ConversationSummary.conversation_id == conversation_id)
        .where(ConversationSummary.kind == KIND_PERIOD)
        .where(ConversationSummary.period_start == start)
        .where(ConversationSummary.period_end.is_not_distinct_from(end))
    )
    session.add(
        ConversationSummary(
            account_id=account_id,
            conversation_id=conversation_id,
            kind=KIND_PERIOD,
            period_start=start,
            period_end=end,
            covers_through_message_id=covers_through,
            message_count=message_count,
            source_digest=digest,
            summary=dict(summary),
            prompt_version=prompt_version,
            model=model,
            created_at=now,
            updated_at=now,
        )
    )
    await session.flush()


# ----------------------------------------------------------- refresh timing
@dataclass(frozen=True, slots=True)
class RefreshTiming:
    """`due`: refresh now. `later`: re-defer to then. Neither: nothing new."""

    due: bool
    later: datetime | None = None


async def refresh_timing(
    session: AsyncSession,
    ref: ConversationRef,
    *,
    now: datetime,
    debounce: timedelta,
    max_wait: timedelta,
) -> RefreshTiming:
    """Trailing debounce (AGENTS §13): refresh once the conversation has been
    quiet for `debounce`, or once its oldest unseen content has waited
    `max_wait`, whichever comes first."""
    if ref.pending_ai_since is None:
        return RefreshTiming(due=False)
    marker = await activity_marker(session, ref.account_id, ref.conversation_id)
    quiet_at = (marker + debounce) if marker is not None else now
    deadline = ref.pending_ai_since + max_wait
    if quiet_at <= now or deadline <= now:
        return RefreshTiming(due=True)
    return RefreshTiming(due=False, later=min(quiet_at, deadline))


async def defer_refresh(
    scheduler: JobScheduler, conversation_id: int, *, at: datetime | None
) -> int | None:
    """Queue a refresh; None when one is already queued (queueing lock)."""
    return await scheduler.defer(
        REFRESH_SUMMARY_TASK,
        args={"conversation_id": conversation_id},
        queueing_lock=summary_queueing_lock(conversation_id),
        schedule_at=at,
    )


@dataclass
class SummaryRefreshTrigger:
    """Defers `memory.refresh_summary` once the transaction that stored new
    content has committed — never inside it: a queueing-lock conflict would
    abort the ingesting transaction. A crash between the commit and the
    defer leaves the flag set, and `system.reconcile_pending` re-creates
    the job."""

    scheduler: JobScheduler
    clock: Clock
    debounce: timedelta = DEFAULT_REFRESH_DEBOUNCE

    def after_new_content(self, session: AsyncSession, conversation_id: int) -> None:
        when = self.clock.now() + self.debounce
        scheduler = self.scheduler

        async def defer(_factory: async_sessionmaker[AsyncSession]) -> None:
            await defer_refresh(scheduler, conversation_id, at=when)

        after_commit(session, defer)


# -------------------------------------------------------------- invalidation
async def invalidate_after_deletion(
    session: AsyncSession,
    conversation: Conversation,
    purged_message_ids: Sequence[int],
    *,
    now: datetime,
) -> int:
    """A deleted message must not survive in a summary (M03 D63).

    Runs in the deletion's transaction, with the conversation row already
    locked. Drops every period cache of the conversation, and the rolling
    summary if it covered one of the messages; the rolling summary is then
    rebuilt from what is retained (the refresh flag is set). Content that
    already expired cannot be rebuilt: `summary_gap_before` records it, so
    answers say earlier messages expired without a summary. Returns the
    number of summaries removed.
    """
    if not purged_message_ids:
        return 0
    account_id, conversation_id = conversation.account_id, conversation.id
    removed = int(
        getattr(
            await session.execute(
                delete(ConversationSummary)
                .where(ConversationSummary.account_id == account_id)
                .where(ConversationSummary.conversation_id == conversation_id)
                .where(ConversationSummary.kind == KIND_PERIOD)
            ),
            "rowcount",
            0,
        )
        or 0
    )
    rolling = (
        await session.execute(
            select(ConversationSummary)
            .where(ConversationSummary.account_id == account_id)
            .where(ConversationSummary.conversation_id == conversation_id)
            .where(ConversationSummary.kind == KIND_ROLLING)
        )
    ).scalar_one_or_none()
    covered = (
        rolling is not None
        and rolling.covers_through_message_id is not None
        and min(purged_message_ids) <= rolling.covers_through_message_id
    )
    if covered:
        await session.delete(rolling)
        removed += 1
        expired = (
            await session.execute(
                select(func.max(Message.sent_at))
                .where(Message.account_id == account_id)
                .where(Message.conversation_id == conversation_id)
                .where(Message.purged_at.is_not(None))
            )
        ).scalar_one_or_none()
        if expired is not None and (
            conversation.summary_gap_before is None or conversation.summary_gap_before < expired
        ):
            conversation.summary_gap_before = expired
        if conversation.mode != "ignore" and conversation.pending_ai_since is None:
            conversation.pending_ai_since = now
    if removed:
        logger.info(
            "summaries_invalidated",
            conversation_id=conversation_id,
            removed=removed,
            rolling=covered,
        )
    return removed


# ------------------------------------------------------- deletion, retention
def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


async def delete_account_summaries(session: AsyncSession, account_id: int) -> int:
    return _rowcount(
        await session.execute(
            delete(ConversationSummary).where(ConversationSummary.account_id == account_id)
        )
    )


async def delete_conversation_summaries(
    session: AsyncSession, account_id: int, conversation_id: int
) -> int:
    return _rowcount(
        await session.execute(
            delete(ConversationSummary)
            .where(ConversationSummary.account_id == account_id)
            .where(ConversationSummary.conversation_id == conversation_id)
        )
    )


async def sweep_period_summaries(session: AsyncSession, now: datetime) -> int:
    """Period caches older than `PERIOD_SUMMARY_RETENTION` (§10.4)."""
    return _rowcount(
        await session.execute(
            delete(ConversationSummary)
            .where(ConversationSummary.kind == KIND_PERIOD)
            .where(ConversationSummary.updated_at < now - PERIOD_SUMMARY_RETENTION)
        )
    )
