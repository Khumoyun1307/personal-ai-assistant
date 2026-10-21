"""Deletion of stored managed-conversation data (FR-ONB-06, J1 step 5; §10.1).

Offered when the owner declines or revokes the `ai_managed` consent. It
cascades over every content-bearing column that managed chats reach
(`privacy/content_columns.py`): message rows, contact names, the raw
business payloads, and control turns that quoted managed content. Ids and
tombstones stay where audit needs them; one content-free audit row records
the counts.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, AuditLogEntry, ControlTurn
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.inbox.models import InboundUpdate

#: Raw updates that carry managed content, or the owner's name from the
#: connection payload (`business_connection.user`).
MANAGED_PAYLOAD_KINDS = (
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "business_connection",
)


@dataclass(frozen=True, slots=True)
class DeletionReport:
    messages: int
    contacts_cleared: int
    payloads_cleared: int
    control_turns_cleared: int

    @property
    def total(self) -> int:
        return (
            self.messages
            + self.contacts_cleared
            + self.payloads_cleared
            + self.control_turns_cleared
        )


async def delete_managed_data(
    session: AsyncSession, *, account_id: int, clock: Clock
) -> DeletionReport:
    """Delete everything stored from the owner's managed chats, in one
    transaction (the caller's). Conversations and coverage gaps stay: they
    hold no content and keep the coverage record honest."""
    account = await session.get(Account, account_id, with_for_update=True)
    if account is None:
        raise ValueError("unknown account")

    def _count(result: object) -> int:
        return int(getattr(result, "rowcount", 0) or 0)

    messages = _count(
        await session.execute(delete(Message).where(Message.account_id == account_id))
    )
    contacts = _count(
        await session.execute(
            update(Contact)
            .where(Contact.account_id == account_id)
            .where((Contact.display_name.is_not(None)) | (Contact.username.is_not(None)))
            .values(display_name=None, username=None, updated_at=clock.now())
        )
    )
    payloads = _count(
        await session.execute(
            update(InboundUpdate)
            .where(InboundUpdate.account_id == account_id)
            .where(InboundUpdate.kind.in_(MANAGED_PAYLOAD_KINDS))
            .where(InboundUpdate.payload.is_not(None))
            .values(payload=None)
        )
    )
    turns = _count(
        await session.execute(
            update(ControlTurn)
            .where(ControlTurn.account_id == account_id)
            .where(ControlTurn.contains_managed.is_(True))
            .where(ControlTurn.text.is_not(None))
            .values(text=None)
        )
    )
    await session.execute(
        update(Conversation)
        .where(Conversation.account_id == account_id)
        .values(pending_ai_since=None, updated_at=clock.now())
    )
    remaining = (
        await session.execute(
            select(func.count()).select_from(Message).where(Message.account_id == account_id)
        )
    ).scalar_one()
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=clock.now(),
            actor="owner",
            event="managed_data_deleted",
            details={
                "messages": messages,
                "contacts_cleared": contacts,
                "payloads_cleared": payloads,
                "control_turns_cleared": turns,
                "messages_remaining": int(remaining),
            },
        )
    )
    await session.flush()
    return DeletionReport(messages, contacts, payloads, turns)
