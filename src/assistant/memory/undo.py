"""One-tap Undo for echoed changes (ARCHITECTURE §8.1 "echoed = executed,
then shown with an Undo button"; FR-MEM-01; S03.3).

A change made by a `configure` tool — a saved or forgotten memory item, a
conversation mode, a preference — writes one content-free audit row. The
echo card's Undo button carries a single-use token on a `pending_inputs`
row (kind `undo`) whose `ref_id` is that audit row. `undo` reverses exactly
that change, once, and only while the state it created is still in place:
a later change is never overwritten, and the owner is told so (§6
invariant 11). Every undo writes its own audit row (`change_undone`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts import preferences
from assistant.accounts.models import Account, AuditLogEntry
from assistant.conversations import modes
from assistant.core.clock import Clock
from assistant.memory import items

UNDO_CARD = "undo"
OP_UNDO = "undo"
EVENT_CHANGE_UNDONE = "change_undone"

#: Events an Undo card may reverse.
UNDOABLE_EVENTS = frozenset(
    {
        items.EVENT_MEMORY_SAVED,
        items.EVENT_MEMORY_FORGOTTEN,
        modes.EVENT_MODE_CHANGED,
        preferences.EVENT_PREFERENCES_CHANGED,
    }
)


class UndoStatus(StrEnum):
    UNDONE = "undone"
    #: Changed again since: nothing was reversed.
    SUPERSEDED = "superseded"
    #: What the change was about no longer exists (a note already deleted —
    #: by the sweep, a deletion or a purge): nothing to reverse.
    GONE = "gone"
    #: Some settings went back; others were changed again since and stay.
    PARTIAL = "partial"
    #: Unknown, another account's, or not a change Undo can reverse.
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class UndoOutcome:
    status: UndoStatus
    event: str | None = None
    #: Preference fields set back, and those left as they are (PARTIAL).
    restored: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()


async def undo(
    session: AsyncSession, *, account_id: int, change_id: int, clock: Clock
) -> UndoOutcome:
    """Reverse one recorded change, in the caller's transaction."""
    entry = (
        await session.execute(
            select(AuditLogEntry)
            .where(AuditLogEntry.account_id == account_id)
            .where(AuditLogEntry.id == change_id)
        )
    ).scalar_one_or_none()
    if entry is None or entry.event not in UNDOABLE_EVENTS:
        return UndoOutcome(UndoStatus.NOT_FOUND)
    details = entry.details or {}
    now = clock.now()
    memory_id = details.get("memory_id")
    if entry.event in (items.EVENT_MEMORY_SAVED, items.EVENT_MEMORY_FORGOTTEN) and (
        not isinstance(memory_id, int) or await items.get(session, account_id, memory_id) is None
    ):
        return UndoOutcome(UndoStatus.GONE, entry.event)
    done = False
    restored: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    if entry.event == items.EVENT_MEMORY_SAVED:
        memory_id = details.get("memory_id")
        if isinstance(memory_id, int):
            done = await items.set_status(
                session, account_id=account_id, memory_id=memory_id, active=False, now=now
            )
            replaced = details.get("replaced_id")
            if done and isinstance(replaced, int):
                await items.set_status(
                    session, account_id=account_id, memory_id=replaced, active=True, now=now
                )
    elif entry.event == items.EVENT_MEMORY_FORGOTTEN:
        memory_id = details.get("memory_id")
        if isinstance(memory_id, int):
            done = await items.set_status(
                session, account_id=account_id, memory_id=memory_id, active=True, now=now
            )
    elif entry.event == modes.EVENT_MODE_CHANGED:
        done = await _undo_mode(session, account_id, entry, clock)
    elif entry.event == preferences.EVENT_PREFERENCES_CHANGED:
        account = await session.get(Account, account_id)
        if account is not None:
            # Check-then-revert under the account lock (review item 6).
            reverted = await preferences.revert(session, account, details)
            done, restored, kept = bool(reverted.restored), reverted.restored, reverted.kept
    if not done:
        return UndoOutcome(UndoStatus.SUPERSEDED, entry.event)
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=now,
            actor="owner",
            event=EVENT_CHANGE_UNDONE,
            subject_type="audit_log",
            subject_id=entry.id,
            details={"event": entry.event},
        )
    )
    await session.flush()
    if kept:
        return UndoOutcome(UndoStatus.PARTIAL, entry.event, restored, kept)
    return UndoOutcome(UndoStatus.UNDONE, entry.event, restored)


async def _undo_mode(
    session: AsyncSession, account_id: int, entry: AuditLogEntry, clock: Clock
) -> bool:
    details = entry.details or {}
    before, after = details.get("from"), details.get("to")
    if entry.subject_id is None or before not in modes.SETTABLE_MODES:
        return False
    try:
        change = await modes.set_mode(
            session,
            account_id=account_id,
            conversation_id=entry.subject_id,
            mode=str(before),
            actor="owner",
            clock=clock,
            details={"undo_of": entry.id},
            expect=str(after),
        )
    except LookupError, modes.ModeError:
        return False
    return change.changed
