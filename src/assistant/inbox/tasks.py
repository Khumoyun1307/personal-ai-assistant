"""Open loops found by triage: commitments, questions and decisions
(ARCHITECTURE §10.3 `tasks`; S05.1, P2-T19).

Tasks are created only by triage in P2 (`created_by = 'agent'`); the owner's
own tasks and reminders are M07. A task's `title` is model-written from
contact text: a content column, shown wrapped as untrusted wherever it
reaches a model.

One promise or question is one task: the agent dedupe key is the
conversation, the kind and the first message that stated it, so triaging
the same promise again updates the task rather than adding a second one.
Every statement is scoped by account; audit rows carry ids and codes only.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Boolean, Row, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.core.clock import Clock
from assistant.inbox.models import Task

#: Task kinds triage creates (the `tasks.kind` CHECK also allows `follow_up`
#: and `reminder`, which are M07's).
OWNER_COMMITMENT = "owner_commitment"
CONTACT_COMMITMENT = "contact_commitment"
OPEN_QUESTION = "open_question"
DECISION = "decision"
COMMITMENT_KINDS = (OWNER_COMMITMENT, CONTACT_COMMITMENT)

#: `closed_reason` codes and the status each closes to.
RESOLVED_BY_TRIAGE = "resolved_by_triage"
EXPIRED = "expired"
_CLOSED_STATUS = {
    RESOLVED_BY_TRIAGE: "done",
    "owner_done": "done",
    "owner_cancelled": "cancelled",
    EXPIRED: "expired",
}

EVENT_TASK_CREATED = "task_created"
EVENT_TASK_CLOSED = "task_closed"

#: Expiry (S05.1 §3.8, TL decision): a question or decision 14 days after its
#: newest source message; a commitment 7 days after it was due, or 30 days
#: after it was found when it has no due date. Silent: no alert, no digest line.
QUESTION_EXPIRY = timedelta(days=14)
COMMITMENT_EXPIRY_AFTER_DUE = timedelta(days=7)
COMMITMENT_EXPIRY_UNDATED = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class TaskView:
    """One task as the triage context, the attention list and the digest see
    it: ids, enums and times, plus its title."""

    task_id: int
    conversation_id: int | None
    kind: str
    title: str
    due_at: datetime | None
    status: str
    source_message_ids: tuple[int, ...]
    created_at: datetime
    closed_at: datetime | None = None
    closed_reason: str | None = None


def _view(row: Task) -> TaskView:
    return TaskView(
        task_id=row.id,
        conversation_id=row.conversation_id,
        kind=row.kind,
        title=row.title,
        due_at=row.due_at,
        status=row.status,
        source_message_ids=tuple(int(i) for i in row.source_message_ids or ()),
        created_at=row.created_at,
        closed_at=row.closed_at,
        closed_reason=row.closed_reason,
    )


def _audit(
    session: AsyncSession,
    *,
    account_id: int,
    event: str,
    task_id: int,
    details: dict[str, Any],
    now: datetime,
) -> None:
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=now,
            actor="agent",
            event=event,
            subject_type="task",
            subject_id=task_id,
            details=details,
        )
    )


async def upsert_from_candidate(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    kind: str,
    title: str,
    due_at: datetime | None,
    source_message_ids: Sequence[int],
    run_id: int | None,
    existing_task_id: int | None,
    clock: Clock,
) -> tuple[int, bool]:
    """Create the task, or update the one it repeats: `existing_task_id` (an
    open task of this conversation the verdict named), else the agent task
    with the same `(conversation, kind, first source message)`. Returns the
    task id and whether a new row was created. A closed task is never
    reopened or retitled."""
    now = clock.now()
    sources = sorted({int(i) for i in source_message_ids})
    if existing_task_id is not None:
        updated = (
            await session.execute(
                update(Task)
                .where(Task.account_id == account_id)
                .where(Task.conversation_id == conversation_id)
                .where(Task.id == existing_task_id)
                .where(Task.status == "open")
                .values(title=title, due_at=due_at, updated_at=now)
                .returning(Task.id, Task.source_message_ids)
            )
        ).first()
        if updated is not None:
            merged = sorted({*(int(i) for i in updated[1] or ()), *sources})
            await session.execute(
                update(Task).where(Task.id == updated[0]).values(source_message_ids=merged)
            )
            return int(updated[0]), False
    new_row = insert(Task).values(
        account_id=account_id,
        conversation_id=conversation_id,
        kind=kind,
        title=title,
        due_at=due_at,
        source_message_ids=sources,
        first_source_message_id=sources[0] if sources else None,
        created_by="agent",
        run_id=run_id,
        created_at=now,
        updated_at=now,
    )
    # `xmax = 0` is true only for a freshly inserted row (Postgres).
    row: Row[int, bool] | None = (
        await session.execute(
            new_row.on_conflict_do_update(
                index_elements=["conversation_id", "kind", "first_source_message_id"],
                index_where=text("created_by = 'agent'"),
                set_={
                    "title": new_row.excluded.title,
                    "due_at": new_row.excluded.due_at,
                    "updated_at": now,
                },
                where=Task.status == "open",
            ).returning(Task.id, literal_column("(xmax = 0)", Boolean))
        )
    ).first()
    if row is None:
        # The same promise was already closed (done or expired): left alone.
        closed = await session.scalar(
            select(Task.id)
            .where(Task.account_id == account_id)
            .where(Task.conversation_id == conversation_id)
            .where(Task.kind == kind)
            .where(Task.first_source_message_id == (sources[0] if sources else None))
            .where(Task.created_by == "agent")
        )
        return int(closed or 0), False
    task_id, inserted = int(row[0]), bool(row[1])
    if inserted:
        _audit(
            session,
            account_id=account_id,
            event=EVENT_TASK_CREATED,
            task_id=task_id,
            details={"kind": kind, "conversation_id": conversation_id, "run_id": run_id},
            now=now,
        )
    return task_id, inserted


async def close(
    session: AsyncSession, *, account_id: int, task_id: int, reason: str, clock: Clock
) -> bool:
    """Close one open task of this account. False when it was not open (or not
    the account's): closing twice changes nothing."""
    status = _CLOSED_STATUS.get(reason)
    if status is None:
        raise ValueError(f"unknown closed_reason {reason!r}")
    now = clock.now()
    closed = (
        await session.execute(
            update(Task)
            .where(Task.account_id == account_id)
            .where(Task.id == task_id)
            .where(Task.status == "open")
            .values(status=status, closed_reason=reason, closed_at=now, updated_at=now)
            .returning(Task.id, Task.kind)
        )
    ).first()
    if closed is None:
        return False
    _audit(
        session,
        account_id=account_id,
        event=EVENT_TASK_CLOSED,
        task_id=task_id,
        details={"kind": closed[1], "reason": reason},
        now=now,
    )
    return True


async def open_for_conversation(
    session: AsyncSession, *, account_id: int, conversation_id: int
) -> list[TaskView]:
    """This conversation's open tasks, due ones first."""
    rows = await session.execute(
        select(Task)
        .where(Task.account_id == account_id)
        .where(Task.conversation_id == conversation_id)
        .where(Task.status == "open")
        .order_by(Task.due_at.asc().nulls_last(), Task.created_at, Task.id)
    )
    return [_view(row) for row in rows.scalars()]


async def open_by_conversation(
    session: AsyncSession, *, account_id: int, conversation_ids: Collection[int]
) -> dict[int, list[TaskView]]:
    """`open_for_conversation` for several chats in one query."""
    found: dict[int, list[TaskView]] = {conversation_id: [] for conversation_id in conversation_ids}
    if not found:
        return found
    rows = await session.execute(
        select(Task)
        .where(Task.account_id == account_id)
        .where(Task.conversation_id.in_(sorted(found)))
        .where(Task.status == "open")
        .order_by(Task.due_at.asc().nulls_last(), Task.created_at, Task.id)
    )
    for row in rows.scalars():
        if row.conversation_id is not None:
            found[row.conversation_id].append(_view(row))
    return found


async def due_between(
    session: AsyncSession,
    *,
    account_id: int,
    start: datetime,
    end: datetime,
    kinds: Collection[str],
) -> list[TaskView]:
    """Open tasks of these kinds due in `[start, end)`, soonest first."""
    rows = await session.execute(
        select(Task)
        .where(Task.account_id == account_id)
        .where(Task.status == "open")
        .where(Task.kind.in_(sorted(kinds)))
        .where(Task.due_at >= start)
        .where(Task.due_at < end)
        .order_by(Task.due_at, Task.id)
    )
    return [_view(row) for row in rows.scalars()]


async def closed_between(
    session: AsyncSession, *, account_id: int, start: datetime, end: datetime, reason: str
) -> list[TaskView]:
    """Tasks closed for `reason` in `[start, end)`, in closing order."""
    rows = await session.execute(
        select(Task)
        .where(Task.account_id == account_id)
        .where(Task.closed_reason == reason)
        .where(Task.closed_at >= start)
        .where(Task.closed_at < end)
        .order_by(Task.closed_at, Task.id)
    )
    return [_view(row) for row in rows.scalars()]


_EXPIRABLE = text(
    """
    SELECT t.id
    FROM app.tasks AS t
    WHERE t.account_id = :account_id
      AND t.status = 'open'
      AND t.created_by = 'agent'
      AND (
        (t.kind IN ('open_question', 'decision') AND coalesce((
            SELECT max(m.sent_at)
            FROM app.messages AS m
            WHERE m.account_id = t.account_id
              AND m.id IN (
                  SELECT CAST(value AS bigint)
                  FROM jsonb_array_elements_text(t.source_message_ids)
              )
        ), t.created_at) < :question_before)
        OR (t.kind IN ('owner_commitment', 'contact_commitment') AND (
            (t.due_at IS NOT NULL AND t.due_at < :due_before)
            OR (t.due_at IS NULL AND t.created_at < :undated_before)
        ))
      )
    ORDER BY t.id
    """
)


async def expire_due(session: AsyncSession, *, clock: Clock) -> int:
    """`system.expire_tasks`: expire the open agent tasks past their time, for
    every account, one account at a time (every statement scoped). Silent:
    content-free audit rows, no alert."""
    now = clock.now()
    accounts = (
        await session.execute(
            select(Task.account_id).where(Task.status == "open").group_by(Task.account_id)
        )
    ).scalars()
    expired = 0
    for account_id in list(accounts):
        ids = (
            await session.execute(
                _EXPIRABLE,
                {
                    "account_id": account_id,
                    "question_before": now - QUESTION_EXPIRY,
                    "due_before": now - COMMITMENT_EXPIRY_AFTER_DUE,
                    "undated_before": now - COMMITMENT_EXPIRY_UNDATED,
                },
            )
        ).scalars()
        for task_id in list(ids):
            expired += await close(
                session, account_id=account_id, task_id=int(task_id), reason=EXPIRED, clock=clock
            )
    return expired
