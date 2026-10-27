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
from assistant.conversations.read import not_stored_while_ignored, saved_message
from assistant.conversations.window import window_closes_at, window_open
from assistant.memory import items, summaries


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
    #: Messages whose content is stored now, by direction (`read.saved_message`).
    counts_by_direction: dict[str, int]
    deleted: int
    expired: int
    last_contact_msg_at: datetime | None
    last_owner_msg_at: datetime | None
    window_open: bool
    window_closes_at: datetime | None
    unresolved_gaps: int
    #: The rolling summary's overview (model output; owner-facing only), S03.3.
    summary_overview: str | None = None
    #: The owner's notes about this contact and chat, newest first (J12).
    notes: tuple[str, ...] = ()
    notes_total: int = 0
    #: Messages that arrived while the chat was in Ignore: a row each, without
    #: content (`read.not_stored_while_ignored`, M03 fix round C3).
    not_stored: int = 0
    contact_id: int | None = None
    contact_priority: str = "normal"

    @property
    def total(self) -> int:
        return sum(self.counts_by_direction.values())


async def conversation_panel(
    session: AsyncSession,
    *,
    account_id: int,
    now: datetime,
    business_chat_id: int | None = None,
    conversation_id: int | None = None,
) -> ConversationPanel | None:
    """By the deep link's chat id, or by our conversation id (the /settings
    lists); always within the account."""
    statement = (
        select(Conversation, Contact)
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.account_id == account_id)
    )
    if business_chat_id is not None:
        statement = statement.where(Conversation.business_chat_id == business_chat_id)
    elif conversation_id is not None:
        statement = statement.where(Conversation.id == conversation_id)
    else:
        return None
    row = (await session.execute(statement)).first()
    if row is None:
        return None
    conversation, contact = row

    # Only rows with content count as saved: the metadata rows Ignore writes
    # made the old count go up when a message arrived while ignored (C3).
    direction_rows = await session.execute(
        select(Message.direction, func.count())
        .where(Message.conversation_id == conversation.id)
        .where(saved_message())
        .group_by(Message.direction)
    )
    counts = {str(direction): int(count) for direction, count in direction_rows.all()}
    not_stored = (
        await session.execute(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation.id)
            .where(not_stored_while_ignored())
        )
    ).scalar_one()
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
    rolling = await summaries.rolling_summary(session, account_id, conversation.id)
    overview = None if rolling is None else rolling.summary.get("overview")
    notes, notes_total = await items.notes_for_panel(session, account_id, conversation.id)

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
        summary_overview=overview if isinstance(overview, str) and overview else None,
        notes=tuple(note.content for note in notes),
        notes_total=notes_total,
        not_stored=int(not_stored),
        contact_id=contact.id,
        contact_priority=contact.priority,
    )
