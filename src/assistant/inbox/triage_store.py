"""Triage verdicts: what a triage run reads, and what its verdict stores
(ARCHITECTURE §6.5 steps 4-5, §10.3; S05.1, P2-T12).

Domain code only — the model call is `agent/triage.py`. A triage run reads
ONE conversation (§6 invariant 4): the **burst** — that conversation's
retained messages newer than its current verdict's
`covers_through_message_id` (the newest `BURST_LIMIT`; the context says when
older ones were left out) — and up to `EARLIER_LIMIT` messages before it as
context. A chat without a verdict yet has no "already triaged" line: its
burst is the messages of the `FIRST_BURST_WINDOW` before its newest message,
so switching triage on never turns a chat's whole history into new alerts
and tasks.

Deleted and expired messages are never read: the filter is the summaries'
(`memory/summaries.py`), the one definition of what an AI run may read.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.conversations.models import Message
from assistant.inbox.tasks import TaskView
from assistant.memory.summaries import SourceMessage
from assistant.memory.summaries import _retained as retained
from assistant.memory.summaries import _source as source_message

#: New messages one triage call reads (the newest, if more arrived).
BURST_LIMIT = 60
#: Earlier messages shown before the burst, for context.
EARLIER_LIMIT = 20
#: Without a verdict, the burst reaches back this far from the newest message.
FIRST_BURST_WINDOW = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class TriageSource:
    """What one triage call reads, oldest first."""

    earlier: tuple[SourceMessage, ...]
    burst: tuple[SourceMessage, ...]
    #: Burst messages left out because more than `BURST_LIMIT` arrived.
    omitted: int = 0

    @property
    def through(self) -> int | None:
        """The newest message shown: what a stored verdict covers."""
        return self.burst[-1].message_id if self.burst else None

    def hashes(self) -> dict[int, str | None]:
        """Every shown message's content hash, for the store re-check."""
        return {m.message_id: m.content_hash for m in (*self.earlier, *self.burst)}

    def directions(self) -> dict[int, str]:
        return {m.message_id: m.direction for m in (*self.earlier, *self.burst)}


@dataclass(frozen=True, slots=True)
class TriageState:
    """What triage knows of a conversation before a run: how far its verdict
    covers, the contact's priority (FR-TRI-04) and its open agent tasks."""

    covers_through_message_id: int | None
    contact_priority: str
    open_tasks: tuple[TaskView, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskCandidate:
    """A task a verdict creates, or updates when `existing_task_id` is set."""

    kind: str
    title: str
    due_at: datetime | None
    source_message_id: int
    existing_task_id: int | None


@dataclass(frozen=True, slots=True)
class TriageVerdict:
    """A validated verdict, as the effects transaction stores it: ids, enums,
    times, and the short reasons and titles (content columns)."""

    account_id: int
    conversation_id: int
    run_id: int
    prompt_version: str
    model: str
    #: Every message the model read → its content hash (the store re-check).
    source: Mapping[int, str | None]
    covers_through_message_id: int
    burst_message_ids: tuple[int, ...]
    needs_reply: bool
    contains_question: bool
    requests_decision: bool
    urgency: str
    deadline_at: datetime | None
    escalation_category: str
    reasons: tuple[str, ...]
    language: str | None
    script: str | None
    tasks: tuple[TaskCandidate, ...]
    resolved_task_ids: tuple[int, ...]


class ApplyResult(StrEnum):
    """What storing a verdict did. Every value but STORED stores nothing."""

    STORED = "stored"
    RACED = "raced"
    """A newer verdict covers as far or further: it stands."""
    CHANGED = "changed"
    """A message the model read was deleted or edited meanwhile."""
    EXPIRED = "expired"
    """A message the model read expired under the retention setting."""
    IGNORED = "ignored"
    """The chat was set to Ignore mode."""
    BOT_CHAT = "bot_chat"
    """The chat turned out to be a chat with a bot."""
    NO_CONSENT = "no_consent"
    """`ai_managed` was revoked while the model ran."""

    @property
    def discarded(self) -> bool:
        return self not in (ApplyResult.STORED, ApplyResult.RACED)


async def burst_source(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    *,
    covers_through: int | None,
) -> TriageSource:
    """The burst after `covers_through` and the messages before it (§3.2).
    Account-scoped; one conversation; retained messages only."""
    newer = select(Message).where(*retained(account_id, conversation_id))
    if covers_through is not None:
        newer = newer.where(Message.id > covers_through)
    else:
        newest = (
            await session.execute(
                select(func.max(Message.sent_at)).where(*retained(account_id, conversation_id))
            )
        ).scalar_one_or_none()
        if newest is None:
            return TriageSource(earlier=(), burst=())
        newer = newer.where(Message.sent_at >= newest - FIRST_BURST_WINDOW)
    rows = list(
        (await session.execute(newer.order_by(Message.id.desc()).limit(BURST_LIMIT + 1)))
        .scalars()
        .all()
    )
    if not rows:
        return TriageSource(earlier=(), burst=())
    omitted = 0
    if len(rows) > BURST_LIMIT:
        rows = rows[:BURST_LIMIT]
        omitted = (
            await session.execute(
                select(func.count()).select_from(newer.where(Message.id < rows[-1].id).subquery())
            )
        ).scalar_one()
    burst = tuple(source_message(row) for row in reversed(rows))
    before = await session.execute(
        select(Message)
        .where(*retained(account_id, conversation_id))
        .where(Message.id < burst[0].message_id)
        .order_by(Message.id.desc())
        .limit(EARLIER_LIMIT)
    )
    earlier = tuple(source_message(row) for row in reversed(before.scalars().all()))
    return TriageSource(earlier=earlier, burst=burst, omitted=int(omitted))
