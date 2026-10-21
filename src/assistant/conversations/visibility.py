"""The per-conversation panel behind `/start bizChat<id>` (S02.3, TF §12 LV-10).

The deep-link parameter is the contact's chat id, in the owner's numbering.
The panel is resolved by `(account_id, business_chat_id)` — never by chat id
alone — and an id that is unknown, malformed or another owner's gives the
same `None` (AGENTS §6.7: fail closed, reveal nothing).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.conversations.models import Contact, Conversation, CoverageGap, Message
from assistant.conversations.window import window_closes_at, window_open


@dataclass(frozen=True, slots=True)
class ConversationPanel:
    conversation_id: int
    business_chat_id: int
    #: Owner-facing, so the contact's name is allowed here (it is shown only
    #: in the owner's own control chat).
    display_name: str | None
    username: str | None
    is_bot: bool
    first_seen_at: datetime
    coverage_started_at: datetime | None
    mode: str
    availability: str
    autonomy_paused_reason: str | None
    counts_by_direction: dict[str, int]
    deleted: int
    expired: int
    last_contact_msg_at: datetime | None
    last_owner_msg_at: datetime | None
    window_open: bool
    window_closes_at: datetime | None
    unresolved_gaps: int

    @property
    def total(self) -> int:
        return sum(self.counts_by_direction.values())


async def conversation_panel(
    session: AsyncSession, *, account_id: int, business_chat_id: int, now: datetime
) -> ConversationPanel | None:
    row = (
        await session.execute(
            select(Conversation, Contact)
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(Conversation.account_id == account_id)
            .where(Conversation.business_chat_id == business_chat_id)
        )
    ).first()
    if row is None:
        return None
    conversation, contact = row

    direction_rows = await session.execute(
        select(Message.direction, func.count())
        .where(Message.conversation_id == conversation.id)
        .where(Message.direction.is_not(None))
        .where(Message.deleted_at.is_(None))
        .group_by(Message.direction)
    )
    counts = {str(direction): int(count) for direction, count in direction_rows.all()}
    deleted = (
        await session.execute(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation.id)
            .where(Message.deleted_at.is_not(None))
        )
    ).scalar_one()
    expired = (
        await session.execute(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation.id)
            .where(Message.purged_at.is_not(None))
        )
    ).scalar_one()
    gaps = (
        await session.execute(
            select(func.count())
            .select_from(CoverageGap)
            .where(CoverageGap.conversation_id == conversation.id)
            .where(CoverageGap.resolved_at.is_(None))
            .where(CoverageGap.certainty.in_(["possible", "definite"]))
        )
    ).scalar_one()

    return ConversationPanel(
        conversation_id=conversation.id,
        business_chat_id=conversation.business_chat_id,
        display_name=contact.display_name,
        username=contact.username,
        is_bot=contact.is_bot,
        first_seen_at=contact.first_seen_at,
        coverage_started_at=conversation.coverage_started_at,
        mode=conversation.mode,
        availability=conversation.availability,
        autonomy_paused_reason=conversation.autonomy_paused_reason,
        counts_by_direction=counts,
        deleted=int(deleted),
        expired=int(expired),
        last_contact_msg_at=conversation.last_contact_msg_at,
        last_owner_msg_at=conversation.last_owner_msg_at,
        window_open=window_open(conversation, now),
        window_closes_at=window_closes_at(conversation),
        unresolved_gaps=int(gaps),
    )
