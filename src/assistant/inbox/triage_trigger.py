"""When a conversation needs triage (ARCHITECTURE §6.5 steps 4-5; S05.1 §3.6, §3.8).

`TriageIngestionListener` runs inside the ingesting transaction (F0
listeners, P2-T04): a new contact message in an Observe or Assist chat — or
the owner's own manual message in a chat with an open task, which may answer
it — sets `conversations.pending_triage_at` (if unset) and defers
`inbox.triage` after the commit, one debounce out. No model call, no Telegram
call, idempotent: a re-processed update only finds the flag already set.

The flag is the source of truth (AGENTS §13): a lost defer is recovered by
the reconciler (`jobs/reconcile_inbox.py`); the job settles the flag
(`inbox/triage_store.py`).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.conversations.events import Direction
from assistant.conversations.listeners import DeletionChange, MessageChange
from assistant.conversations.models import Conversation, Message
from assistant.core.clock import Clock
from assistant.core.jobs import TRIAGE_TASK, JobScheduler, triage_queueing_lock
from assistant.db.uow import after_commit
from assistant.inbox.models import Task

#: Chat modes triage reads (Ignore never; a bot chat is never in them).
TRIAGED_MODES = frozenset({"observe", "assist"})


async def defer_triage(
    scheduler: JobScheduler, conversation_id: int, *, at: datetime | None
) -> int | None:
    """Queue `inbox.triage`; None when one is already queued (queueing lock)."""
    return await scheduler.defer(
        TRIAGE_TASK,
        args={"conversation_id": conversation_id},
        queueing_lock=triage_queueing_lock(conversation_id),
        schedule_at=at,
    )


class TriageIngestionListener:
    def __init__(self, *, scheduler: JobScheduler, clock: Clock, debounce: timedelta) -> None:
        self._scheduler = scheduler
        self._clock = clock
        self._debounce = debounce

    async def on_message(self, session: AsyncSession, change: MessageChange) -> None:
        if not change.new_message or change.metadata_only or change.mode not in TRIAGED_MODES:
            return
        if change.direction is Direction.OWNER_MANUAL:
            if not await _has_open_task(session, change.account_id, change.conversation_id):
                return
        elif change.direction is not Direction.CONTACT:
            return
        now = self._clock.now()
        # The conversation row is already locked by ingestion (F0 §5).
        await session.execute(
            update(Conversation)
            .where(Conversation.id == change.conversation_id)
            .where(Conversation.account_id == change.account_id)
            .where(Conversation.pending_triage_at.is_(None))
            .values(pending_triage_at=now)
        )
        scheduler, conversation_id, at = (
            self._scheduler,
            change.conversation_id,
            now + self._debounce,
        )

        async def defer(_factory: async_sessionmaker[AsyncSession]) -> None:
            await defer_triage(scheduler, conversation_id, at=at)

        after_commit(session, defer)

    async def on_deletion(self, session: AsyncSession, change: DeletionChange) -> None:
        """A deletion triggers nothing: deleted messages are never shown, and
        the next burst's triage sees what remains."""


async def _has_open_task(session: AsyncSession, account_id: int, conversation_id: int) -> bool:
    return (
        await session.scalar(
            select(Task.id)
            .where(Task.account_id == account_id)
            .where(Task.conversation_id == conversation_id)
            .where(Task.status == "open")
            .limit(1)
        )
    ) is not None


async def quiet_until(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    now: datetime,
    debounce: timedelta,
) -> datetime | None:
    """The trailing debounce (S05.1 §3.8): when the chat has been quiet for
    `debounce` since its newest contact message arrived; None when it
    already has. Arrival (receipt) time, never the Telegram date."""
    newest = await session.scalar(
        select(func.max(Message.created_at))
        .where(Message.account_id == account_id)
        .where(Message.conversation_id == conversation_id)
        .where(Message.direction == Direction.CONTACT.value)
    )
    if newest is None or newest + debounce <= now:
        return None
    return newest + debounce
