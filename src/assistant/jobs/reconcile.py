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

from assistant.core.jobs import APPLY_CONFIRMATION_TASK, OWNER_REQUEST_TASK, REFRESH_SUMMARY_TASK

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


#: A claimed (`applying`) call should finish within seconds; after this it was
#: cut off (a cancelled job) and its outcome is unknown.
APPLYING_TIMEOUT = timedelta(minutes=10)

_INTERRUPTED_CONFIRMATIONS = text(
    """
    UPDATE app.tool_confirmations
    SET status = 'failed', args = NULL
    WHERE status = 'applying' AND decided_at < :older_than
    RETURNING id, account_id, tool_name
    """
)


async def fail_interrupted_confirmations(
    session: AsyncSession, now: datetime
) -> list[tuple[int, int, str]]:
    """Fail calls left `applying` (cut off mid-way); `(id, account_id, tool)`
    so the owner can be told. Conditional: a row that finished meanwhile is
    untouched."""
    result = await session.execute(
        _INTERRUPTED_CONFIRMATIONS, {"older_than": now - APPLYING_TIMEOUT}
    )
    return [(int(row[0]), int(row[1]), str(row[2])) for row in result.all()]


async def unqueued_owner_turns(session: AsyncSession, now: datetime) -> list[tuple[int, int]]:
    """`(turn_id, account_id)` pairs that need their job re-deferred."""
    result = await session.execute(
        _UNQUEUED_OWNER_TURNS,
        {"older_than": now - GRACE, "newer_than": now - WINDOW, "task": OWNER_REQUEST_TASK},
    )
    return [(int(row[0]), int(row[1])) for row in result.all()]


#: A conversation flagged for a summary refresh this long ago, with no queued
#: or running `memory.refresh_summary` job, lost its job (a crash between the
#: ingesting commit and the defer, or a run that ended without clearing the
#: flag). Only accounts with an active `ai_managed` consent: without it
#: nothing may be summarized (§14.3); the job re-checks the processor.
SUMMARY_GRACE = timedelta(seconds=30)
SUMMARY_BATCH = 500

_UNQUEUED_SUMMARY_REFRESHES = text(
    """
    SELECT c.id
    FROM app.conversations AS c
    JOIN app.contacts AS k ON k.id = c.contact_id
    WHERE c.pending_ai_since IS NOT NULL
      AND c.pending_ai_since < :older_than
      AND c.mode <> 'ignore'
      AND NOT k.is_bot
      AND EXISTS (
          SELECT 1 FROM app.consents AS s
          WHERE s.account_id = c.account_id
            AND s.kind = 'ai_managed'
            AND s.revoked_at IS NULL
      )
      AND NOT EXISTS (
          -- By queueing lock (`sum:{id}`, core/jobs.py): `todo` rows are
          -- found through procrastinate's unique queueing-lock index.
          SELECT 1 FROM procrastinate.procrastinate_jobs AS j
          WHERE j.queueing_lock = 'sum:' || c.id::text
            AND j.task_name = :task
            AND j.status IN ('todo', 'doing')
      )
    ORDER BY c.pending_ai_since, c.id
    LIMIT :limit
    """
)


async def unqueued_summary_refreshes(session: AsyncSession, now: datetime) -> list[int]:
    """Conversation ids whose summary refresh job must be re-created."""
    result = await session.execute(
        _UNQUEUED_SUMMARY_REFRESHES,
        {"older_than": now - SUMMARY_GRACE, "task": REFRESH_SUMMARY_TASK, "limit": SUMMARY_BATCH},
    )
    return [int(row[0]) for row in result.all()]
