"""M04 reconcilers (F0-interfaces §6.3): domain rows are the truth, jobs are triggers.

`UnqueuedDrafts` (S04.2 §3.9): a conversation in Assist whose
`pending_draft_at` is older than the grace period, with no queued or running
`replies.draft` job holding its `draft:{id}` queueing lock, lost its job (a
crash between the ingesting commit and the after-commit defer, or a run that
ended without clearing the flag). Only while the Assist trigger is on, never
for bot chats, and only for accounts whose consent lets a draft run (the job
re-checks): without consent the flag simply waits. A flag found on a chat
that left Assist, or while the trigger is off, is cleared here, in the
reconciling transaction, so it is never re-deferred every cycle.

Lives in the jobs adapter because it reads procrastinate's own table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions.config import ActionsConfig
from assistant.agent.drafting import draft_consent_ok
from assistant.core.jobs import DRAFT_TASK, DraftReason, draft_queueing_lock
from assistant.jobs.reconcile import DeferRequest

#: Do not race a defer that is happening right now.
DRAFT_GRACE = timedelta(seconds=30)
DRAFT_BATCH = 500

_CLEAR_STRAY_FLAGS = text(
    """
    UPDATE app.conversations AS c
    SET pending_draft_at = NULL
    FROM app.contacts AS k
    WHERE k.id = c.contact_id
      AND c.pending_draft_at IS NOT NULL
      AND c.pending_draft_at < :older_than
      AND (c.mode <> 'assist' OR k.is_bot OR :trigger_off)
    RETURNING c.id
    """
)

_UNQUEUED_DRAFTS = text(
    """
    SELECT c.id, c.account_id
    FROM app.conversations AS c
    JOIN app.contacts AS k ON k.id = c.contact_id
    WHERE c.pending_draft_at IS NOT NULL
      AND c.pending_draft_at < :older_than
      AND c.mode = 'assist'
      AND NOT k.is_bot
      AND NOT EXISTS (
          SELECT 1 FROM procrastinate.procrastinate_jobs AS j
          WHERE j.queueing_lock = 'draft:' || c.id::text
            AND j.task_name = :task
            AND j.status IN ('todo', 'doing')
      )
    ORDER BY c.pending_draft_at, c.id
    LIMIT :limit
    """
)


@dataclass(frozen=True, slots=True)
class UnqueuedDrafts:
    """A `Reconciler` (`JobHandlers.reconcilers`), built once in `_wire_m04`."""

    config: ActionsConfig
    #: The model the `draft_reply` route uses: consent must name its processor.
    model: str

    async def __call__(self, session: AsyncSession, now: datetime) -> list[DeferRequest]:
        older_than = now - DRAFT_GRACE
        trigger = self.config.assist_draft_trigger
        await session.execute(
            _CLEAR_STRAY_FLAGS, {"older_than": older_than, "trigger_off": trigger == "off"}
        )
        if trigger == "off":
            return []
        reason = DraftReason.TRIAGE_NEEDS_REPLY
        if trigger == "ingestion":
            reason = DraftReason.ASSIST_INGESTION
        rows = (
            await session.execute(
                _UNQUEUED_DRAFTS,
                {"older_than": older_than, "task": DRAFT_TASK, "limit": DRAFT_BATCH},
            )
        ).all()
        consent: dict[int, bool] = {}
        requests: list[DeferRequest] = []
        for conversation_id, account_id in rows:
            if account_id not in consent:
                consent[account_id] = await draft_consent_ok(session, account_id, self.model)
            if not consent[account_id]:
                continue
            requests.append(
                DeferRequest(
                    DRAFT_TASK,
                    {"conversation_id": int(conversation_id), "reason": reason.value},
                    queueing_lock=draft_queueing_lock(int(conversation_id)),
                )
            )
        return requests
