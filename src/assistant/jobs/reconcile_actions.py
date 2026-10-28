"""M04 reconcilers (F0-interfaces §6.3; ARCHITECTURE §12.4).

Domain rows are the source of truth and jobs are triggers (AGENTS §13). An
`approved` action that is due (no `retry_at` ahead) and has had no change
for a few seconds, with no `actions.execute` job queued or running for it,
lost its job — a crash between the approval's commit and its defer
(ARCHITECTURE §9.5), or a re-defer lost after a WAIT or a 429. Re-deferring
is safe by construction: Tx A's conditional claim lets only one job send.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.core.jobs import EXECUTE_TASK
from assistant.jobs.reconcile import DeferRequest

#: Do not race the defer that normally follows an approval's commit.
EXECUTE_GRACE = timedelta(seconds=10)
EXECUTE_BATCH = 500

_UNQUEUED_EXECUTIONS = text(
    """
    SELECT a.id
    FROM app.actions AS a
    WHERE a.state = 'approved'
      AND (a.retry_at IS NULL OR a.retry_at <= :now)
      AND a.updated_at < :older_than
      AND NOT EXISTS (
          SELECT 1 FROM procrastinate.procrastinate_jobs AS j
          WHERE j.task_name = :task
            AND j.args ->> 'action_id' = a.id::text
            AND j.status IN ('todo', 'doing')
      )
    ORDER BY a.id
    LIMIT :limit
    """
)


async def unqueued_executions(session: AsyncSession, now: datetime) -> list[DeferRequest]:
    """`actions.execute` defers for approved actions whose job was lost."""
    rows = await session.execute(
        _UNQUEUED_EXECUTIONS,
        {
            "now": now,
            "older_than": now - EXECUTE_GRACE,
            "task": EXECUTE_TASK,
            "limit": EXECUTE_BATCH,
        },
    )
    return [DeferRequest(EXECUTE_TASK, {"action_id": int(row[0])}) for row in rows.all()]
