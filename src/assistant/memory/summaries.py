"""Conversation summaries: storage, refresh timing and invalidation
(ARCHITECTURE §11.1, §12.2; S03.2).

Domain code only — no model calls (those are `agent/summarize.py`). What
lives here:

- **reading the source**: one conversation's retained messages after a
  summary's `covers_through_message_id` (rolling), or within a range
  (period), account-scoped, each with its `content_hash`;
- **storing** a rolling summary optimistically (only if the stored one is
  still the one it was built on) and a period summary as a cache keyed by
  range and `source_digest`. Both take the §9.4 locks (account FOR SHARE,
  so a store waits for managed-data deletion's FOR NO KEY UPDATE; then the
  conversation FOR UPDATE) and **re-check the source in a fresh statement**:
  every message the model read must still be retained with the same
  content, and the chat must still be summarizable (not Ignore, not a bot
  chat). Otherwise nothing is stored — deleted or edited text never lands in
  a summary written while the model ran (§6 invariant 9, PRD §20, D63, D66).
  No lock is held across a model call (AGENTS §13);
- **the refresh flag** `conversations.pending_ai_since` ("content no AI run
  has seen yet"): cleared when no message changed since the refresh read its
  source, otherwise moved to the earliest such change, so the maximum wait
  counts from content the summary has not seen;
- **refresh timing**: a trailing debounce with a maximum wait;
- **deferring** `memory.refresh_summary` after the ingesting transaction
  commits (a crash in between is caught by `system.reconcile_pending`);
- **invalidation**: a deleted or edited message must not live on in a
  summary, so it drops the chat's period caches, and the rolling summary
  when it covered the message (rebuilt from what is retained);
- **deletion and retention** helpers for the privacy cascades.

Change markers ignore retention purges (`purged_at`): expiry is not news
for a summary, which exists to outlive it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, Consent
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
    #: What the store re-checks: the message still has this content.
    content_hash: str | None = None


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

    @property
    def version(self) -> tuple[int, int | None]:
        """What a rolling store compares: the row and how far it covers."""
        return (self.summary_id, self.covers_through_message_id)


@dataclass(frozen=True, slots=True)
class ConversationRef:
    """What a job needs to know about a conversation it was given by id."""

    conversation_id: int
    account_id: int
    mode: str
    is_bot: bool
    pending_ai_since: datetime | None


class StoreResult(StrEnum):
    """What a summary store did. Every value but STORED stores nothing; all
    but RACED mean the chat changed while the model ran, and say how."""

    STORED = "stored"
    RACED = "raced"
    """Another refresh stored first: its summary stands."""
    CHANGED = "changed"
    """A message the model read was deleted or edited (or the summary it
    built on was invalidated by one)."""
    EXPIRED = "expired"
    """A message the model read expired under the retention setting."""
    IGNORED = "ignored"
    """The chat was set to Ignore mode."""
    BOT_CHAT = "bot_chat"
    """The chat turned out to be a chat with a bot (purged)."""

    @property
    def discarded(self) -> bool:
        return self not in (StoreResult.STORED, StoreResult.RACED)


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
        content_hash=row.content_hash,
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


def preferred_name(display_name: str | None, username: str | None) -> str | None:
    """How the owner's views name a contact: the display name, else the
    @username, else None (contact-chosen: callers wrap it as untrusted)."""
    if display_name and display_name.strip():
        return display_name.strip()
    if username and username.strip().lstrip("@"):
        return "@" + username.strip().lstrip("@")
    return None


async def contact_display_name(
    session: AsyncSession, account_id: int, conversation_id: int
) -> str | None:
    """The name of THIS conversation's contact, for its summary run (S03.7):
    account-scoped, one conversation, nothing about any other chat (§6
    invariant 4)."""
    row = (
        await session.execute(
            select(Contact.display_name, Contact.username)
            .join(Conversation, Conversation.contact_id == Contact.id)
            .where(Conversation.account_id == account_id)
            .where(Contact.account_id == account_id)
            .where(Conversation.id == conversation_id)
        )
    ).first()
    return None if row is None else preferred_name(row[0], row[1])


async def message_directions(
    session: AsyncSession, account_id: int, conversation_id: int, ids: Collection[int]
) -> dict[int, str]:
    """Message id → direction (classification; content-free) for those of
    `ids` that are this conversation's messages — who wrote what a summary
    cites, for the attribution check (S03.7). Expired and deleted messages
    keep their direction, so their authorship is still known."""
    if not ids:
        return {}
    rows = await session.execute(
        select(Message.id, Message.direction)
        .where(Message.account_id == account_id)
        .where(Message.conversation_id == conversation_id)
        .where(Message.id.in_(sorted(ids)))
        .where(Message.direction.is_not(None))
    )
    return {int(mid): str(direction) for mid, direction in rows.all()}


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
    """When a message of the conversation last changed (new, edited,
    deleted) — retention purges excluded."""
    return (
        await session.execute(
            select(func.max(Message.updated_at))
            .where(Message.account_id == account_id)
            .where(Message.conversation_id == conversation_id)
            .where(Message.purged_at.is_(None))
        )
    ).scalar_one_or_none()


async def first_change_after(
    session: AsyncSession, account_id: int, conversation_id: int, marker: datetime | None
) -> datetime | None:
    """The earliest message change after `marker` (None: no change)."""
    statement = (
        select(func.min(Message.updated_at))
        .where(Message.account_id == account_id)
        .where(Message.conversation_id == conversation_id)
        .where(Message.purged_at.is_(None))
    )
    if marker is not None:
        statement = statement.where(Message.updated_at > marker)
    return (await session.execute(statement)).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class PeriodSource:
    #: Oldest first; the newest `limit` of the range when it holds more.
    messages: tuple[SourceMessage, ...]
    #: Every retained message in the range, including those left out.
    total: int
    digest: str | None
    covers_through_message_id: int | None


def source_digest(pairs: Sequence[tuple[int, str | None]]) -> str | None:
    """A hash over (message id, content hash): any new, edited, deleted or
    expired message in the range changes it."""
    if not pairs:
        return None
    canonical = "|".join(f"{mid}:{content or ''}" for mid, content in sorted(pairs))
    return hashlib.sha256(canonical.encode()).hexdigest()[:40]


async def _period_fingerprint(
    session: AsyncSession, account_id: int, conversation_id: int, start: datetime, end: datetime
) -> tuple[str | None, int, int | None]:
    """(digest, count, highest id) of the range, without loading any text."""
    rows = (
        await session.execute(
            select(Message.id, Message.content_hash)
            .where(*_retained(account_id, conversation_id))
            .where(Message.sent_at >= start)
            .where(Message.sent_at < end)
        )
    ).all()
    pairs = [(int(mid), content) for mid, content in rows]
    return source_digest(pairs), len(pairs), max((mid for mid, _ in pairs), default=None)


async def period_source(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    *,
    start: datetime,
    end: datetime,
    limit: int,
) -> PeriodSource:
    """The retained messages of one conversation sent in `[start, end)`:
    counted and fingerprinted in full, but only the newest `limit` loaded.

    A range that fits the limit is read in ONE statement and its digest
    taken from those very rows, so the digest and the texts cannot differ.
    A larger range is fingerprinted first and loaded second: a change in
    between can only make the store's re-check fail (a wasted call), never
    let changed text through."""
    statement = (
        select(Message)
        .where(*_retained(account_id, conversation_id))
        .where(Message.sent_at >= start)
        .where(Message.sent_at < end)
        .order_by(Message.sent_at.desc(), Message.id.desc())
    )
    rows = list((await session.execute(statement.limit(limit + 1))).scalars().all())
    if len(rows) <= limit:
        kept = [_source(row) for row in reversed(rows)]
        return PeriodSource(
            messages=tuple(kept),
            total=len(kept),
            digest=source_digest([(m.message_id, m.content_hash) for m in kept]),
            covers_through_message_id=max((m.message_id for m in kept), default=None),
        )
    digest, total, highest = await _period_fingerprint(
        session, account_id, conversation_id, start, end
    )
    newest = list((await session.execute(statement.limit(limit))).scalars().all())
    kept = [_source(row) for row in reversed(newest)]
    return PeriodSource(
        messages=tuple(kept), total=total, digest=digest, covers_through_message_id=highest
    )


async def cached_period(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    *,
    start: datetime,
    end: datetime | None,
    digest: str,
    prompt_version: str,
    model: str,
) -> StoredSummary | None:
    """A period summary for exactly this range and these messages, written by
    this prompt version and model (S03.7: a v4 summary, without attribution
    status, is never served as a v5 one; a changed model is a new summary)."""
    row = (
        await session.execute(
            select(ConversationSummary)
            .where(ConversationSummary.account_id == account_id)
            .where(ConversationSummary.conversation_id == conversation_id)
            .where(ConversationSummary.kind == KIND_PERIOD)
            .where(ConversationSummary.period_start == start)
            .where(ConversationSummary.period_end.is_not_distinct_from(end))
            .where(ConversationSummary.source_digest == digest)
            .where(ConversationSummary.prompt_version == prompt_version)
            .where(ConversationSummary.model == model)
        )
    ).scalar_one_or_none()
    return None if row is None else _stored(row)


# ------------------------------------------------------------------ writing
async def lock_conversation(
    session: AsyncSession, account_id: int, conversation_id: int
) -> Conversation | None:
    """The conversation row, locked FOR UPDATE."""
    return (
        await session.execute(
            select(Conversation)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id == conversation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _lock_for_store(
    session: AsyncSession, account_id: int, conversation_id: int
) -> StoreResult | None:
    """§9.4 lock order for a summary write: the account FOR SHARE (it waits
    for managed-data deletion's FOR NO KEY UPDATE, but neither for
    ingestion's foreign-key share nor for other stores), then the
    conversation FOR UPDATE — the lock every deletion, edit, bot-chat
    purge, retention purge and managed-data deletion of this chat takes
    first, so a store waits for them and then sees their result. None when
    the chat may still be summarized; otherwise why not."""
    account = (
        await session.execute(
            select(Account.id).where(Account.id == account_id).with_for_update(read=True)
        )
    ).scalar_one_or_none()
    if account is None:
        return StoreResult.CHANGED
    conversation = await lock_conversation(session, account_id, conversation_id)
    if conversation is None:
        return StoreResult.CHANGED
    is_bot = (
        await session.execute(select(Contact.is_bot).where(Contact.id == conversation.contact_id))
    ).scalar_one_or_none()
    if is_bot:
        return StoreResult.BOT_CHAT
    if conversation.mode == "ignore":
        return StoreResult.IGNORED
    return None


async def _changed_source(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    source: Mapping[int, str | None],
) -> StoreResult | None:
    """None when every message the model read is still retained with the
    same content (read in a fresh statement, after the locks); otherwise
    EXPIRED if one of them expired under retention, else CHANGED."""
    if not source:
        return None
    rows = await session.execute(
        select(Message.id, Message.content_hash)
        .where(*_retained(account_id, conversation_id))
        .where(Message.id.in_(list(source)))
    )
    if {int(mid): content for mid, content in rows.all()} == dict(source):
        return None
    expired = (
        await session.execute(
            select(func.count())
            .select_from(Message)
            .where(Message.account_id == account_id)
            .where(Message.id.in_(list(source)))
            .where(Message.purged_at.is_not(None))
        )
    ).scalar_one()
    return StoreResult.EXPIRED if expired else StoreResult.CHANGED


async def store_rolling(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    based_on: tuple[int, int | None] | None,
    source: Mapping[int, str | None],
    summary: Mapping[str, Any],
    covers_through: int,
    added_messages: int,
    prompt_version: str,
    model: str,
    now: datetime,
) -> StoreResult:
    """Write the new rolling summary if (a) the chat may still be summarized,
    (b) every message it was written from is unchanged, and (c) the stored
    summary is still the one it was built on (`based_on` = its `version`,
    None for none). Otherwise nothing is written."""
    refused = await _lock_for_store(session, account_id, conversation_id)
    refused = refused or await _changed_source(session, account_id, conversation_id, source)
    if refused is not None:
        return refused
    current = (
        await session.execute(
            select(ConversationSummary)
            .where(ConversationSummary.account_id == account_id)
            .where(ConversationSummary.conversation_id == conversation_id)
            .where(ConversationSummary.kind == KIND_ROLLING)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if current is None and based_on is not None:
        # The summary this was built on is gone: only a deletion or edit
        # of a message it covered (or a purge) removes it (D63, D66).
        return StoreResult.CHANGED
    if (None if current is None else (current.id, current.covers_through_message_id)) != based_on:
        return StoreResult.RACED
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
    return StoreResult.STORED


async def settle_refresh_flag(
    session: AsyncSession, *, account_id: int, conversation_id: int, marker: datetime | None
) -> None:
    """After a refresh saw everything up to `marker`: clear `pending_ai_since`
    if nothing changed since, else move it to the earliest change after the
    marker — the content the summary has not seen (the maximum wait counts
    from there, not from content already summarized)."""
    conversation = await lock_conversation(session, account_id, conversation_id)
    if conversation is None or conversation.pending_ai_since is None:
        return
    conversation.pending_ai_since = await first_change_after(
        session, account_id, conversation_id, marker
    )


async def store_period(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    start: datetime,
    source_end: datetime,
    end: datetime | None,
    digest: str,
    source: Mapping[int, str | None],
    summary: Mapping[str, Any],
    covers_through: int | None,
    message_count: int,
    prompt_version: str,
    model: str,
    now: datetime,
) -> StoreResult:
    """Cache one period summary, replacing the previous one for the range —
    only if the range's messages are exactly those it was written from
    (`digest` recomputed after the locks) and the chat may still be
    summarized. `source_end` is the range end that was read; `end` is the
    stored key (None = open-ended); `source` maps the messages the model
    read to their content hashes (to say why, when the digest moved)."""
    refused = await _lock_for_store(session, account_id, conversation_id)
    if refused is not None:
        return refused
    current, _, _ = await _period_fingerprint(
        session, account_id, conversation_id, start, source_end
    )
    if current != digest:
        why = await _changed_source(session, account_id, conversation_id, source)
        return why or StoreResult.CHANGED
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
    return StoreResult.STORED


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
async def invalidate_covering(
    session: AsyncSession,
    conversation: Conversation,
    message_ids: Sequence[int],
    *,
    now: datetime,
) -> int:
    """A deleted (D63) or edited (D66) message must not survive in a summary.

    Runs in the deletion's or edit's transaction, with the conversation row
    already locked. Drops every period cache of the conversation, and the
    rolling summary if it covered one of the messages; the rolling summary is
    then rebuilt from what is retained (the refresh flag is set). Content that
    already expired cannot be rebuilt: `summary_gap_before` records it, so
    answers say earlier messages expired without a summary. Returns the
    number of summaries removed. `message_ids` must be messages whose content
    changed in that transaction (a repeated deletion is not one).
    """
    if not message_ids:
        return 0
    account_id, conversation_id = conversation.account_id, conversation.id
    removed = _rowcount(
        await session.execute(
            delete(ConversationSummary)
            .where(ConversationSummary.account_id == account_id)
            .where(ConversationSummary.conversation_id == conversation_id)
            .where(ConversationSummary.kind == KIND_PERIOD)
        )
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
        and min(message_ids) <= rolling.covers_through_message_id
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


async def covered_through(session: AsyncSession, conversation_ids: set[int]) -> dict[int, int]:
    """conversation id → the highest message id its rolling summary covers."""
    if not conversation_ids:
        return {}
    rows = await session.execute(
        select(ConversationSummary.conversation_id, ConversationSummary.covers_through_message_id)
        .where(ConversationSummary.kind == KIND_ROLLING)
        .where(ConversationSummary.conversation_id.in_(sorted(conversation_ids)))
        .where(ConversationSummary.covers_through_message_id.is_not(None))
    )
    return {int(c): int(m) for c, m in rows.all() if m is not None}


async def rollup_candidates(
    session: AsyncSession,
    expiring_before: datetime,
    *,
    limit: int = 500,
    account_ids: Sequence[int] | None = None,
) -> list[tuple[int, datetime]]:
    """(conversation id, earliest change) of chats with retained content last
    changed before `expiring_before` that their rolling summary has not
    covered — the rollup before the retention ceiling (§10.4). Only chats
    that may be summarized: account with an active `ai_managed` consent, not
    Ignore, not a bot. The earliest change is that content's `updated_at`,
    what the refresh flag counts its maximum wait from. `account_ids` limits
    it to those accounts (their retention, S03.5)."""
    rolling = (
        select(ConversationSummary.covers_through_message_id)
        .where(ConversationSummary.conversation_id == Message.conversation_id)
        .where(ConversationSummary.kind == KIND_ROLLING)
        .scalar_subquery()
    )
    consent = (
        select(Consent.id)
        .where(Consent.account_id == Conversation.account_id)
        .where(Consent.kind == "ai_managed")
        .where(Consent.revoked_at.is_(None))
        .exists()
    )
    statement = select(Message.conversation_id, func.min(Message.updated_at))
    if account_ids is not None:
        statement = statement.where(Message.account_id.in_(list(account_ids)))
    rows = await session.execute(
        statement.join(Conversation, Conversation.id == Message.conversation_id)
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.mode != "ignore")
        .where(Contact.is_bot.is_(False))
        .where(consent)
        .where(Message.deleted_at.is_(None))
        .where(Message.purged_at.is_(None))
        .where((Message.text.is_not(None)) | (Message.media.is_not(None)))
        .where(func.coalesce(Message.edited_at, Message.sent_at) < expiring_before)
        .where(Message.id > func.coalesce(rolling, 0))
        .group_by(Message.conversation_id)
        # Soonest to expire first, so a chat whose refresh keeps failing
        # cannot starve the others past the batch limit (review item 8).
        .order_by(
            func.min(func.coalesce(Message.edited_at, Message.sent_at)), Message.conversation_id
        )
        .limit(limit)
    )
    return [(int(c), changed) for c, changed in rows.all()]


async def sweep_period_summaries(session: AsyncSession, now: datetime) -> int:
    """Period caches older than `PERIOD_SUMMARY_RETENTION` (§10.4)."""
    return _rowcount(
        await session.execute(
            delete(ConversationSummary)
            .where(ConversationSummary.kind == KIND_PERIOD)
            .where(ConversationSummary.updated_at < now - PERIOD_SUMMARY_RETENTION)
        )
    )
