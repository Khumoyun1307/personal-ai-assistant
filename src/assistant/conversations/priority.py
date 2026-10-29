"""Contact priority (FR-TRI-04; S05.2 §3.6).

`vip` raises a contact's alerts one step, `low` lowers them one step
(`inbox/notification_policy.py`); `normal` leaves them as triage set them.
One setter for the panel (S05.2P) and the `set_contact_priority` tool:
validated, scoped to the account, audited content-free, and reversible by
Undo only while the value is still the one a change set (`expect`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.conversations.models import Contact
from assistant.core.clock import Clock
from assistant.core.errors import NotInAccountError

EVENT_PRIORITY_CHANGED = "contact_priority_changed"
PRIORITIES = ("vip", "normal", "low")


class PriorityError(ValueError):
    """A priority that does not exist. Safe to show the owner."""


@dataclass(frozen=True, slots=True)
class PriorityChange:
    contact_id: int
    previous: str
    current: str
    #: The change's audit row; None when nothing changed.
    audit_id: int | None

    @property
    def changed(self) -> bool:
        return self.audit_id is not None


async def set_priority(
    session: AsyncSession,
    *,
    account_id: int,
    contact_id: int,
    priority: str,
    actor: str,
    clock: Clock,
    details: dict[str, Any] | None = None,
    expect: str | None = None,
) -> PriorityChange:
    """Set one contact's priority, in the caller's transaction. A foreign or
    unknown contact is `NotInAccountError`; setting the current value is a
    no-op; with `expect` (an Undo), only if the value is still that."""
    if priority not in PRIORITIES:
        raise PriorityError("priority must be vip, normal or low")
    contact = (
        await session.execute(
            select(Contact)
            .where(Contact.account_id == account_id)
            .where(Contact.id == contact_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if contact is None:
        raise NotInAccountError("contact")
    previous = contact.priority
    if previous == priority or (expect is not None and previous != expect):
        return PriorityChange(contact.id, previous, previous, None)
    contact.priority = priority
    entry = AuditLogEntry(
        account_id=account_id,
        at=clock.now(),
        actor=actor,
        event=EVENT_PRIORITY_CHANGED,
        subject_type="contact",
        subject_id=contact.id,
        details={"contact_id": contact.id, "old": previous, "new": priority, **(details or {})},
    )
    session.add(entry)
    await session.flush()
    return PriorityChange(contact.id, previous, priority, entry.id)
