"""Monitor reconciler (S05.4 §3.6; F0-interfaces §6.3): a semantic-monitor
evaluation whose job was lost — a crash between the ingestion commit and the
after-commit defer — is recovered from `conversations.pending_monitor_at`,
the source of truth (AGENTS §13)."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.core.jobs import MONITOR_TASK, monitor_queueing_lock
from assistant.jobs.reconcile import DeferRequest, Reconciler

#: A flagged conversation younger than this still has its own defer coming.
MONITOR_GRACE = timedelta(seconds=30)
MONITOR_BATCH = 500

#: Flagged chats that may still be evaluated (Observe/Assist, not a bot
#: chat, `ai_managed` on — without it the job would only skip, every minute)
#: and have no live job, found by its queueing lock (`mon:{id}`).
_UNQUEUED_EVALUATIONS = text(
    """
    SELECT c.id
    FROM app.conversations AS c
    JOIN app.contacts AS k ON k.id = c.contact_id AND k.account_id = c.account_id
    WHERE c.pending_monitor_at IS NOT NULL
      AND c.pending_monitor_at < :older_than
      AND c.mode IN ('observe', 'assist')
      AND NOT k.is_bot
      AND EXISTS (
          SELECT 1 FROM app.consents AS s
          WHERE s.account_id = c.account_id
            AND s.kind = 'ai_managed'
            AND s.revoked_at IS NULL
      )
      AND NOT EXISTS (
          SELECT 1 FROM procrastinate.procrastinate_jobs AS j
          WHERE j.queueing_lock = 'mon:' || c.id::text
            AND j.task_name = :task
            AND j.status IN ('todo', 'doing')
      )
    ORDER BY c.pending_monitor_at, c.id
    LIMIT :limit
    """
)


def monitor_reconciler() -> Reconciler:
    """Wired only while flag `monitors.run` is on (S05.4 §3.6)."""

    async def reconcile(session: AsyncSession, now: datetime) -> list[DeferRequest]:
        result = await session.execute(
            _UNQUEUED_EVALUATIONS,
            {"older_than": now - MONITOR_GRACE, "task": MONITOR_TASK, "limit": MONITOR_BATCH},
        )
        return [
            DeferRequest(
                MONITOR_TASK,
                {"conversation_id": int(conversation_id)},
                queueing_lock=monitor_queueing_lock(int(conversation_id)),
                schedule_at=now,
            )
            for (conversation_id,) in result.all()
        ]

    return reconcile
