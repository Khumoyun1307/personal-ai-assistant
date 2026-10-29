"""M05 reconcilers (F0-interfaces §6.3): recover deferred work whose job was
lost — a crash between the domain commit and the defer — from the domain
rows, which are the source of truth (AGENTS §13)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.core.jobs import (
    NOTIFY_TASK,
    TRIAGE_TASK,
    notification_queueing_lock,
    triage_queueing_lock,
)
from assistant.jobs.reconcile import DeferRequest, Reconciler

#: A queued notification younger than this still has its own defer coming.
NOTIFICATION_GRACE = timedelta(seconds=30)
NOTIFICATION_BATCH = 500

_UNQUEUED_NOTIFICATIONS = text(
    """
    SELECT n.id
    FROM app.notifications AS n
    WHERE n.status = 'queued'
      AND n.updated_at < :older_than
      AND (n.deliver_after IS NULL OR n.deliver_after <= :now)
      AND (:standard OR n.alert_class = 'system')
      AND NOT EXISTS (
          -- By queueing lock (`notify:{id}`, core/jobs.py).
          SELECT 1 FROM procrastinate.procrastinate_jobs AS j
          WHERE j.queueing_lock = 'notify:' || n.id::text
            AND j.task_name = :task
            AND j.status IN ('todo', 'doing')
      )
    ORDER BY n.created_at, n.id
    LIMIT :limit
    """
)


def notification_reconciler(*, enabled: bool) -> Reconciler:
    """Queued notifications that are due and have no live job. While flag
    `notifications.deliver` is off (`enabled=False`), standard rows are
    meant to wait, so only system rows are recovered — no churn."""

    async def reconcile(session: AsyncSession, now: datetime) -> list[DeferRequest]:
        result = await session.execute(
            _UNQUEUED_NOTIFICATIONS,
            {
                "older_than": now - NOTIFICATION_GRACE,
                "now": now,
                "standard": enabled,
                "task": NOTIFY_TASK,
                "limit": NOTIFICATION_BATCH,
            },
        )
        return [
            DeferRequest(
                NOTIFY_TASK,
                {"notification_id": int(notification_id)},
                queueing_lock=notification_queueing_lock(int(notification_id)),
            )
            for (notification_id,) in result.all()
        ]

    return reconcile


#: A flagged conversation younger than this still has its own defer coming.
TRIAGE_GRACE = timedelta(seconds=30)
TRIAGE_BATCH = 500

_UNQUEUED_TRIAGE = text(
    """
    SELECT c.id
    FROM app.conversations AS c
    JOIN app.contacts AS k ON k.id = c.contact_id
    WHERE c.pending_triage_at IS NOT NULL
      AND c.pending_triage_at < :older_than
      AND c.mode IN ('observe', 'assist')
      AND NOT k.is_bot
      AND EXISTS (
          SELECT 1 FROM app.consents AS s
          WHERE s.account_id = c.account_id
            AND s.kind = 'ai_managed'
            AND s.revoked_at IS NULL
      )
      AND NOT EXISTS (
          -- By queueing lock (`triage:{id}`, core/jobs.py).
          SELECT 1 FROM procrastinate.procrastinate_jobs AS j
          WHERE j.queueing_lock = 'triage:' || c.id::text
            AND j.task_name = :task
            AND j.status IN ('todo', 'doing')
      )
    ORDER BY c.pending_triage_at, c.id
    LIMIT :limit
    """
)


def triage_reconciler() -> Reconciler:
    """Flagged Observe/Assist chats (not bots, with `ai_managed` active)
    whose `inbox.triage` job was lost (S05.1 §3.8). Wired only while flag
    `triage.run` is on."""

    async def reconcile(session: AsyncSession, now: datetime) -> list[DeferRequest]:
        result = await session.execute(
            _UNQUEUED_TRIAGE,
            {"older_than": now - TRIAGE_GRACE, "task": TRIAGE_TASK, "limit": TRIAGE_BATCH},
        )
        return [
            DeferRequest(
                TRIAGE_TASK,
                {"conversation_id": int(conversation_id)},
                queueing_lock=triage_queueing_lock(int(conversation_id)),
                schedule_at=now,
            )
            for (conversation_id,) in result.all()
        ]

    return reconcile
