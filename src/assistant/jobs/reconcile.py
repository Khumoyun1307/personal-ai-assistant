"""Find work whose job may have been lost (ARCHITECTURE §12.2 `reconcile_pending`).

Domain rows are the source of truth and jobs are triggers (AGENTS §13). An
owner control turn is "lost" when it is older than a grace period, recent
enough to still matter, and has no `agent.owner_request` job that is queued,
running, finished or failed — i.e. none at all, or only aborted/cancelled ones
(a shutdown that cut a run short).

Lives in the jobs adapter because it reads procrastinate's own table.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.core.jobs import APPLY_CONFIRMATION_TASK, OWNER_REQUEST_TASK

#: Do not race a job that is being deferred right now.
GRACE = timedelta(seconds=30)
#: Older turns are stale; answering them now would confuse more than help.
WINDOW = timedelta(hours=1)

_UNQUEUED_OWNER_TURNS = text(
    """
    SELECT t.id, t.account_id
    FROM app.control_turns AS t
    WHERE t.role = 'owner'
      AND t.created_at < :older_than
      AND t.created_at >= :newer_than
      AND NOT EXISTS (
          SELECT 1 FROM procrastinate.procrastinate_jobs AS j
          WHERE j.task_name = :task
            AND j.args ->> 'turn_id' = t.id::text
            AND j.status IN ('todo', 'doing', 'succeeded', 'failed')
      )
    ORDER BY t.id
    """
)


#: A confirmed tool call should be applied within seconds; after this, a
#: missing job is presumed lost. Re-deferring is safe: the applier claims
#: `confirmed → applied` once, so a duplicate job does nothing.
CONFIRMATION_GRACE = timedelta(minutes=2)
#: Beyond this the retention sweep fails the row instead (the owner's
#: intent is a day old).
CONFIRMATION_WINDOW = timedelta(hours=24)

_UNAPPLIED_CONFIRMATIONS = text(
    """
    SELECT c.id, c.account_id
    FROM app.tool_confirmations AS c
    WHERE c.status = 'confirmed'
      AND c.decided_at < :older_than
      AND c.decided_at >= :newer_than
      AND NOT EXISTS (
          SELECT 1 FROM procrastinate.procrastinate_jobs AS j
          WHERE j.task_name = :task
            AND j.args ->> 'confirmation_id' = c.id::text
            AND j.status IN ('todo', 'doing')
      )
    ORDER BY c.id
    """
)


async def unapplied_confirmations(session: AsyncSession, now: datetime) -> list[tuple[int, int]]:
    """`(confirmation_id, account_id)` of confirmed calls whose job was lost."""
    result = await session.execute(
        _UNAPPLIED_CONFIRMATIONS,
        {
            "older_than": now - CONFIRMATION_GRACE,
            "newer_than": now - CONFIRMATION_WINDOW,
            "task": APPLY_CONFIRMATION_TASK,
        },
    )
    return [(int(row[0]), int(row[1])) for row in result.all()]


async def unqueued_owner_turns(session: AsyncSession, now: datetime) -> list[tuple[int, int]]:
    """`(turn_id, account_id)` pairs that need their job re-deferred."""
    result = await session.execute(
        _UNQUEUED_OWNER_TURNS,
        {"older_than": now - GRACE, "newer_than": now - WINDOW, "task": OWNER_REQUEST_TASK},
    )
    return [(int(row[0]), int(row[1])) for row in result.all()]
