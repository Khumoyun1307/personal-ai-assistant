"""M05 reconcilers (F0-interfaces §6.3): recover deferred work whose job was
lost — a crash between the domain commit and the defer — from the domain
rows, which are the source of truth (AGENTS §13)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.core.jobs import NOTIFY_TASK, notification_queueing_lock
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
