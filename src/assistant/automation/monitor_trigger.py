"""Monitors at ingestion: the deterministic fast path (S05.4 §3.4; F0
listeners, P2-T04).

`MonitorIngestionListener` runs inside the ingesting transaction, after the
actions and triage listeners. For a new contact message in an Observe or
Assist chat it checks every applicable monitor confirmed before the
message was sent:

- a deterministic condition (`contact_replied`, `keywords`,
  `media_received`) is matched here, with no model call, and a match is
  recorded in this transaction (`automation.monitors.record_match`: the
  one-shot completion, the `MONITOR_MATCH` alert, audit rows);
- a semantic condition (`semantic`, `decision_requested`, `urgent`) sets
  `conversations.pending_monitor_at` (if unset) and defers
  `monitors.evaluate` after the commit, one debounce out — one model call
  per burst for every semantic monitor of the chat (`agent/monitor_eval.py`).

No model call, no Telegram call, no commit; idempotent — a re-processed
update is a duplicate, not a new message, and a one-shot monitor completes
at most once. The flag is the source of truth for the evaluation (AGENTS
§13): a lost defer is recovered by the reconciler
(`jobs/reconcile_monitors.py`).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.automation import monitors
from assistant.conversations.events import Direction
from assistant.conversations.listeners import DeletionChange, MessageChange
from assistant.conversations.models import Conversation, Message
from assistant.core.clock import Clock
from assistant.core.jobs import MONITOR_TASK, JobScheduler, monitor_queueing_lock
from assistant.db.uow import after_commit


async def defer_evaluation(
    scheduler: JobScheduler, conversation_id: int, *, at: datetime | None
) -> int | None:
    """Queue `monitors.evaluate`; None when one is already queued (queueing
    lock `mon:{id}`). Never a `lock`: it may be scheduled (AGENTS §13)."""
    return await scheduler.defer(
        MONITOR_TASK,
        args={"conversation_id": conversation_id},
        queueing_lock=monitor_queueing_lock(conversation_id),
        schedule_at=at,
    )


class MonitorIngestionListener:
    def __init__(self, *, scheduler: JobScheduler, clock: Clock, debounce: timedelta) -> None:
        self._scheduler = scheduler
        self._clock = clock
        self._debounce = debounce

    async def on_message(self, session: AsyncSession, change: MessageChange) -> None:
        if (
            not change.new_message
            or change.direction is not Direction.CONTACT
            or change.metadata_only
            or change.mode not in monitors.WATCHED_MODES
            or change.message_id is None
        ):
            return
        now = self._clock.now()
        views = [
            view
            for view in await monitors.applicable(
                session,
                account_id=change.account_id,
                conversation_id=change.conversation_id,
                now=now,
            )
            if view.covers(change.sent_at)
        ]
        if not views:
            return
        facts = await monitors.message_facts(
            session,
            account_id=change.account_id,
            conversation_id=change.conversation_id,
            message_id=change.message_id,
        )
        if facts is None or facts.direction != Direction.CONTACT.value:
            return
        for view in views:
            if not view.semantic and monitors.matches(view.rule, facts):
                await monitors.record_match(
                    session,
                    view,
                    conversation_id=change.conversation_id,
                    contact_id=facts.contact_id,
                    message_ids=(facts.message_id,),
                    clock=self._clock,
                    scheduler=self._scheduler,
                    via="ingestion",
                )
        if any(view.semantic for view in views):
            await self._flag(session, change, facts.created_at, now)

    async def _flag(
        self, session: AsyncSession, change: MessageChange, arrived: datetime, now: datetime
    ) -> None:
        """The evaluation flag: when the first message not yet evaluated
        arrived (its receipt time, so the burst query finds it), set once.
        The conversation row is already locked by ingestion (F0 §5)."""
        await session.execute(
            update(Conversation)
            .where(Conversation.id == change.conversation_id)
            .where(Conversation.account_id == change.account_id)
            .where(Conversation.pending_monitor_at.is_(None))
            .values(pending_monitor_at=arrived)
        )
        scheduler, conversation_id, at = (
            self._scheduler,
            change.conversation_id,
            now + self._debounce,
        )

        async def defer(_factory: async_sessionmaker[AsyncSession]) -> None:
            await defer_evaluation(scheduler, conversation_id, at=at)

        after_commit(session, defer)

    async def on_deletion(self, session: AsyncSession, change: DeletionChange) -> None:
        """A deletion triggers nothing: a deleted message is never evaluated,
        and its alert, if any, names no text."""


async def quiet_until(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    now: datetime,
    debounce: timedelta,
) -> datetime | None:
    """The trailing debounce (S05.4 §3.6, as S05.1 §3.8): when the chat has
    been quiet for `debounce` since its newest contact message arrived; None
    when it already has. Receipt time, never the Telegram date."""
    newest = await session.scalar(
        select(func.max(Message.created_at))
        .where(Message.account_id == account_id)
        .where(Message.conversation_id == conversation_id)
        .where(Message.direction == Direction.CONTACT.value)
    )
    if newest is None or newest + debounce <= now:
        return None
    return newest + debounce
