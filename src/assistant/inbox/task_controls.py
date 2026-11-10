"""Minimal owner task governance (stabilization I11, D-S9).

Undo content stays in the task's existing retained/deletable `details`
column. Audits contain only ids, codes and the call's idempotency key.
One snapshot per task prevents Undo from overwriting a later correction.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, AuditLogEntry
from assistant.conversations.models import Conversation
from assistant.core.clock import Clock
from assistant.core.errors import NotInAccountError
from assistant.inbox.models import Task

EVENT_TASK_UPDATED = "owner_task_updated"
Operation = Literal["complete", "cancel", "correct"]
_SNAPSHOT = "task_governance_v1"


async def get(session: AsyncSession, *, account_id: int, task_id: int, lock: bool = False) -> Task:
    stmt = select(Task).where(Task.account_id == account_id, Task.id == task_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = await session.scalar(stmt)
    if row is None:
        raise NotInAccountError("task not in account")
    return row


async def _lock_task(session: AsyncSession, account_id: int, task_id: int) -> Task:
    # NO KEY UPDATE serializes owner calls without blocking triage's FK
    # checks while it finishes under the conversation lock.
    await session.scalar(
        select(Account.id).where(Account.id == account_id).with_for_update(key_share=True)
    )
    row = await get(session, account_id=account_id, task_id=task_id)
    if row.conversation_id is not None:
        await session.scalar(
            select(Conversation.id)
            .where(Conversation.account_id == account_id, Conversation.id == row.conversation_id)
            .with_for_update()
        )
    # Refresh after the conversation lock: triage may have committed while
    # this transaction was waiting. Never overwrite its newer evidence.
    return await get(session, account_id=account_id, task_id=task_id, lock=True)


def _state(row: Task) -> dict[str, str | None]:
    return {
        "title": row.title,
        "due_at": None if row.due_at is None else row.due_at.isoformat(),
        "status": row.status,
        "closed_at": None if row.closed_at is None else row.closed_at.isoformat(),
        "closed_reason": row.closed_reason,
        "updated_at": row.updated_at.isoformat(),
        "source_message_ids": json.dumps(row.source_message_ids or []),
    }


def _snapshot(details: str | None) -> dict[str, Any] | None:
    if details is None:
        return None
    try:
        value = json.loads(details)
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("type") != _SNAPSHOT:
        return None
    return value


async def change(
    session: AsyncSession,
    *,
    account_id: int,
    task_id: int,
    operation: Operation,
    title: str | None,
    due_at: datetime | None,
    change_due: bool,
    run_id: int,
    call_id: str,
    clock: Clock,
) -> tuple[str, int | None]:
    """Serialize owner changes and retries under the account, then task lock.

    Complete/cancel require an open task; a correction never reopens one.
    The caller translates local dates before entering this function.
    """
    row = await _lock_task(session, account_id, task_id)
    if call_id:
        previous = await session.scalar(
            select(AuditLogEntry.id)
            .where(AuditLogEntry.account_id == account_id)
            .where(AuditLogEntry.event == EVENT_TASK_UPDATED)
            .where(AuditLogEntry.details["run_id"].astext == str(run_id))
            .where(AuditLogEntry.details["call_id"].astext == call_id)
            .limit(1)
        )
        if previous is not None:
            return "already_applied", None
    if row.status != "open":
        return "not_open", None
    before = _state(row)
    old_snapshot = _snapshot(row.details)
    original_details = row.details if old_snapshot is None else old_snapshot["original_details"]
    if operation == "correct":
        if title is not None:
            row.title = title
        if change_due:
            row.due_at = due_at
    else:
        row.status = "done" if operation == "complete" else "cancelled"
        row.closed_reason = "owner_done" if operation == "complete" else "owner_cancelled"
        row.closed_at = clock.now()
    if _state(row) == before:
        return "unchanged", None
    row.updated_at = clock.now()
    entry = AuditLogEntry(
        account_id=account_id,
        at=clock.now(),
        actor="owner",
        event=EVENT_TASK_UPDATED,
        subject_type="task",
        subject_id=task_id,
        details={"operation": operation, "run_id": run_id, "call_id": call_id},
    )
    session.add(entry)
    await session.flush()
    row.details = json.dumps(
        {
            "type": _SNAPSHOT,
            "audit_id": entry.id,
            "before": before,
            "after": _state(row),
            "original_details": original_details,
        }
    )
    await session.flush()
    return "changed", entry.id


async def undo(
    session: AsyncSession, *, account_id: int, entry: AuditLogEntry, clock: Clock
) -> bool:
    if entry.subject_id is None:
        return False
    try:
        row = await _lock_task(session, account_id, entry.subject_id)
    except NotInAccountError:
        return False
    saved = _snapshot(row.details)
    if saved is None or saved.get("audit_id") != entry.id or saved.get("after") != _state(row):
        return False
    before = saved["before"]
    row.title = before["title"]
    row.due_at = None if before["due_at"] is None else datetime.fromisoformat(before["due_at"])
    row.status = before["status"]
    row.closed_reason = before["closed_reason"]
    row.closed_at = (
        None if before["closed_at"] is None else datetime.fromisoformat(before["closed_at"])
    )
    row.details = saved["original_details"]
    row.updated_at = clock.now()
    return True
