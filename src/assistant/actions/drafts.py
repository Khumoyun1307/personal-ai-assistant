"""The proactive draft trigger: the switch (P2-O6) and `request_draft` (S04.2 §3.6).

`assist_trigger` is the single switch between "no proactive drafts", drafts
after ingestion (M04) and drafts after triage (M05). `request_draft` asks for
one draft of a conversation: the `replies.draft` job (S04.2) makes it.

Two ways to ask, because the two callers need different guarantees:

- **Proactive** (`ASSIST_INGESTION`, `TRIAGE_NEEDS_REPLY`): the conversation's
  `pending_draft_at` flag is the source of truth, set in the caller's
  transaction; the job is deferred after the commit with the `draft:{id}`
  queueing lock and the debounce. A lost defer is recovered by the drafts
  reconciler from the flag, and a queueing-lock conflict (a job is already
  waiting) can never abort the ingesting transaction (F0-interfaces §6.1).
- **Re-draft** (`REDRAFT`, the owner's tap on a card, S04.3): no flag. The job
  is deferred inside the press's transaction, so it commits with the press
  and cannot be lost; the defer sits in a SAVEPOINT, so a second tap while
  one is queued (`redraft:{id}` conflict) rolls back only the savepoint and
  the press still commits (decisions R17).

Job arguments are ids and enums only (AGENTS §13).
"""

from __future__ import annotations

from collections.abc import Set
from datetime import timedelta
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.conversations.models import Conversation
from assistant.core.clock import Clock
from assistant.core.errors import NotInAccountError
from assistant.core.jobs import DRAFT_TASK, DraftReason, JobScheduler, draft_queueing_lock
from assistant.db.uow import after_commit

#: The reasons a `replies.draft` job may carry; the others name the drafter's
#: own triggers (`propose_reply`, the E2 target) and are never job arguments.
JOB_REASONS = frozenset(
    {DraftReason.ASSIST_INGESTION, DraftReason.TRIAGE_NEEDS_REPLY, DraftReason.REDRAFT}
)
#: Proactive reasons use the flag and the debounce; a re-draft uses neither.
FLAGGED_REASONS = frozenset({DraftReason.ASSIST_INGESTION, DraftReason.TRIAGE_NEEDS_REPLY})


def assist_trigger(flags: Set[str]) -> Literal["off", "ingestion", "triage"]:
    if "assist.drafts" not in flags:
        return "off"
    if {"triage.run", "triage.assist_trigger"} <= flags:
        return "triage"
    return "ingestion"


def redraft_queueing_lock(conversation_id: int) -> str:
    """At most one queued owner re-draft per conversation (S04.3 `rd`)."""
    return f"redraft:{conversation_id}"


async def request_draft(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    reason: DraftReason,
    scheduler: JobScheduler,
    clock: Clock,
    debounce: timedelta,
) -> None:
    """Ask for one draft of the account's conversation (see the module doc).

    Raises `NotInAccountError` for a conversation that is not the account's,
    and `ValueError` for a reason that is not a job reason."""
    reason = DraftReason(reason)
    if reason not in JOB_REASONS:
        raise ValueError("not a replies.draft reason")
    now = clock.now()
    if reason in FLAGGED_REASONS:
        # The UPDATE takes the row lock itself when the caller has not (the
        # ingestion listener already holds it, §9.4). `coalesce`: the first
        # message of a burst sets the time; later ones keep it.
        flagged = await session.scalar(
            update(Conversation)
            .where(Conversation.account_id == account_id, Conversation.id == conversation_id)
            .values(pending_draft_at=func.coalesce(Conversation.pending_draft_at, now))
            .returning(Conversation.id)
            .execution_options(synchronize_session=False)
        )
        if flagged is None:
            raise NotInAccountError("conversation not found in this account")
        when = now + debounce

        async def defer(_factory: async_sessionmaker[AsyncSession]) -> None:
            await scheduler.defer(
                DRAFT_TASK,
                args={"conversation_id": conversation_id, "reason": reason.value},
                queueing_lock=draft_queueing_lock(conversation_id),
                schedule_at=when,
            )

        after_commit(session, defer)
        return
    found = await session.scalar(
        select(Conversation.id).where(
            Conversation.account_id == account_id, Conversation.id == conversation_id
        )
    )
    if found is None:
        raise NotInAccountError("conversation not found in this account")
    savepoint = await session.begin_nested()
    try:
        connection = await session.connection()
        raw = await connection.get_raw_connection()
        job_id = await scheduler.defer(
            DRAFT_TASK,
            args={"conversation_id": conversation_id, "reason": reason.value},
            queueing_lock=redraft_queueing_lock(conversation_id),
            connection=raw.driver_connection,
        )
    except BaseException:
        await savepoint.rollback()
        raise
    if job_id is None:
        # One is already queued; Postgres aborted the savepoint on the
        # unique violation, so only the savepoint is undone.
        await savepoint.rollback()
    else:
        await savepoint.commit()
