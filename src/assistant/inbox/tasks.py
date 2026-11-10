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

import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, Row, String, func, literal_column, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.conversations.models import Message
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
_RESOLUTION_DETAILS = "task_resolution_evidence_v1"


@dataclass(frozen=True, slots=True)
class ResolutionEvidence:
    task_id: int
    evidence_message_ids: tuple[int, ...]


def resolution_ids(details: str | None) -> tuple[int, ...]:
    """Read references from the text envelope; legacy details remain readable.

    Content-free references survive message content retention as tombstone ids.
    Plain text and owner-governance snapshots do not represent resolutions.
    """
    if not details:
        return ()
    try:
        saved = json.loads(details)
    except ValueError:
        return ()
    if not isinstance(saved, dict) or saved.get("type") != _RESOLUTION_DETAILS:
        return ()
    values = saved.get("resolution_evidence_message_ids", [])
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if type(value) is int and value > 0)


async def close_with_evidence(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    resolution: ResolutionEvidence,
    shown_new_ids: Collection[int],
    suspicious: bool,
    clock: Clock,
) -> bool:
    """Re-check references under the caller's conversation lock, before closing.

    A source-backed task starts at its newest source's sent time/id; a task
    without sources starts at creation. Receipt time is not message chronology.
    No content is copied and retention is never extended.
    """
    evidence_ids = set(resolution.evidence_message_ids)
    if suspicious or not evidence_ids or not evidence_ids.issubset(shown_new_ids):
        return False
    task = await session.scalar(
        select(Task).where(
            Task.account_id == account_id,
            Task.conversation_id == conversation_id,
            Task.id == resolution.task_id,
            Task.status == "open",
        )
    )
    if task is None:
        return False
    sources = set(task.source_message_ids or ())
    rows = list(
        (
            await session.scalars(
                select(Message)
                .where(
                    Message.account_id == account_id,
                    Message.conversation_id == conversation_id,
                    Message.id.in_(sources | evidence_ids),
                )
                .order_by(Message.id)
                .with_for_update()
            )
        ).all()
    )
    by_id = {row.id: row for row in rows}
    if not (sources | evidence_ids).issubset(by_id):
        return False
    source_times = [by_id[source].sent_at for source in sources]
    if any(moment is None for moment in source_times):
        return False
    start = task.created_at
    if sources:
        start = max(moment for moment in source_times if moment is not None)
    for evidence_id in evidence_ids:
        message = by_id[evidence_id]
        if (
            message.deleted_at is not None
            or message.purged_at is not None
            or message.sent_at is None
            or message.sent_at <= start
            or evidence_id <= max(sources, default=0)
            or message.direction not in {"contact", "owner_manual"}
            or (message.text is None and message.media is None)
        ):
            return False
    closed = await close(
        session,
        account_id=account_id,
        task_id=task.id,
        reason=RESOLVED_BY_TRIAGE,
        clock=clock,
    )
    if closed:
        task.details = json.dumps(
            {
                "type": _RESOLUTION_DETAILS,
                "resolution_evidence_message_ids": sorted(evidence_ids),
                "original_details": task.details,
            }
        )
    return closed


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
    #: Postgres row revision, captured with the triage read, never sent to the model.
    revision: str | None = None
    resolution_evidence_message_ids: tuple[int, ...] = ()


def _view(row: Task, *, revision: str | None = None) -> TaskView:
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
        revision=revision,
        resolution_evidence_message_ids=resolution_ids(row.details),
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
        # xmin changes even when two writes use the same injected clock.
        select(Task, literal_column("xmin::text", type_=String))
        .where(Task.account_id == account_id)
        .where(Task.conversation_id == conversation_id)
        .where(Task.status == "open")
        .order_by(Task.due_at.asc().nulls_last(), Task.created_at, Task.id)
    )
    return [_view(row, revision=revision) for row, revision in rows]


async def open_by_conversation(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_ids: Collection[int],
    limit_per_conversation: int | None = None,
) -> dict[int, list[TaskView]]:
    """`open_for_conversation` for several chats in one query."""
    found: dict[int, list[TaskView]] = {conversation_id: [] for conversation_id in conversation_ids}
    if not found:
        return found
    stmt = (
        select(Task)
        .where(Task.account_id == account_id)
        .where(Task.conversation_id.in_(sorted(found)))
        .where(Task.status == "open")
        .order_by(Task.due_at.asc().nulls_last(), Task.created_at, Task.id)
    )
    if limit_per_conversation is not None:
        ranked = (
            select(
                Task.id,
                func.row_number()
                .over(
                    partition_by=Task.conversation_id,
                    order_by=(Task.due_at.asc().nulls_last(), Task.created_at, Task.id),
                )
                .label("position"),
            )
            .where(Task.account_id == account_id)
            .where(Task.conversation_id.in_(sorted(found)))
            .where(Task.status == "open")
            .subquery()
        )
        stmt = stmt.join(ranked, ranked.c.id == Task.id).where(
            ranked.c.position <= limit_per_conversation
        )
    rows = await session.execute(stmt)
    for row in rows.scalars():
        if row.conversation_id is not None:
            found[row.conversation_id].append(_view(row))
    return found


async def digest_commitments(
    session: AsyncSession, *, account_id: int, end: datetime, limit: int
) -> tuple[list[TaskView], int]:
    """Open promises: all overdue/undated and those due before the horizon.

    Oldest discovered first, so undated promises compete fairly with dated
    ones. Count overflow in SQL; only the capped titles leave the database.
    """
    eligible = (
        select(Task)
        .where(Task.account_id == account_id)
        .where(Task.status == "open")
        .where(Task.kind.in_(COMMITMENT_KINDS))
        .where(Task.due_at.is_(None) | (Task.due_at < end))
    )
    total = int(await session.scalar(select(func.count()).select_from(eligible.subquery())) or 0)
    rows = await session.execute(eligible.order_by(Task.created_at, Task.id).limit(limit))
    views = [_view(row) for row in rows.scalars()]
    return views, max(0, total - len(views))


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


async def expire_due(session: AsyncSession, *, clock: Clock) -> int:
    """Compatibility hook for the hourly job: age is never resolution (D-S9).

    Open tasks remain visible until evidence or an explicit owner decision
    closes them. `list_tasks` marks old tasks for review without mutating them.
    """
    return 0
