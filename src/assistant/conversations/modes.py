"""Conversation modes (PRD §11.1, FR-OBS-08; S03.3).

One setter for every path — the conversation panel's buttons, the /settings
lists and the `set_conversation_mode` tool:

- **Ignore**: from the next message on, nothing is stored or processed for
  the chat (`record_message` keeps metadata only); what is already stored
  stays — messages until they expire, the summary, notes. The refresh flag is
  cleared, so no summary job picks the chat up.
- **Observe**: storage and processing resume. Retained content that the
  rolling summary has not seen is flagged for a refresh.

`assist` arrives with drafts (M04); Delegate is never a stored mode. A chat
with a bot stays in Ignore (FR-OBS-04). The change takes the conversation
row lock (§9.4: ingestion holds it too, so a message is stored entirely
before or entirely after the switch) and writes one content-free audit row,
which is what a one-tap Undo reverses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.core.errors import NotInAccountError
from assistant.memory import summaries

MODE_OBSERVE = "observe"
MODE_IGNORE = "ignore"
#: What the owner can choose now (`assist`: M04).
SETTABLE_MODES = (MODE_OBSERVE, MODE_IGNORE)

EVENT_MODE_CHANGED = "conversation_mode_changed"


class ModeError(ValueError):
    """A mode that cannot be set. The message is safe to show the owner."""


@dataclass(frozen=True, slots=True)
class ModeChange:
    conversation_id: int
    contact_id: int
    previous: str
    current: str
    #: The audit row recording it (the Undo reference); None if unchanged.
    audit_id: int | None

    @property
    def changed(self) -> bool:
        return self.previous != self.current


async def conversation_for_contact(session: AsyncSession, account_id: int, contact_id: int) -> int:
    """The conversation with this contact; NotInAccountError if it is not this
    account's contact or there is no chat with them."""
    found = (
        await session.execute(
            select(Conversation.id)
            .where(Conversation.account_id == account_id)
            .where(Conversation.contact_id == contact_id)
            .order_by(Conversation.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotInAccountError("no conversation with this contact")
    return int(found)


async def contact_of(session: AsyncSession, account_id: int, conversation_id: int) -> int:
    """The contact of one of the account's chats; NotInAccountError otherwise."""
    found = (
        await session.execute(
            select(Conversation.contact_id)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotInAccountError("conversation not found in this account")
    return int(found)


async def has_unsummarized_content(
    session: AsyncSession, account_id: int, conversation_id: int
) -> bool:
    """Retained content the rolling summary has not covered."""
    rolling = await summaries.rolling_summary(session, account_id, conversation_id)
    covers = None if rolling is None else rolling.covers_through_message_id
    statement = (
        select(func.count())
        .select_from(Message)
        .where(Message.account_id == account_id)
        .where(Message.conversation_id == conversation_id)
        .where(Message.deleted_at.is_(None))
        .where(Message.purged_at.is_(None))
        .where((Message.text.is_not(None)) | (Message.media.is_not(None)))
    )
    if covers is not None:
        statement = statement.where(Message.id > covers)
    return int((await session.execute(statement)).scalar_one()) > 0


async def set_mode(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    mode: str,
    actor: str,
    clock: Clock,
    details: dict[str, Any] | None = None,
    expect: str | None = None,
) -> ModeChange:
    """Set one conversation's mode, in the caller's transaction. With
    `expect` (an Undo), only if the chat is still in that mode.

    NotInAccountError: not this account's conversation. ModeError: a mode that
    cannot be chosen, or a bot chat leaving Ignore."""
    if mode not in SETTABLE_MODES:
        raise ModeError("chats can be set to observe or ignore")
    conversation = (
        await session.execute(
            select(Conversation)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id == conversation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if conversation is None:
        raise NotInAccountError("conversation not found in this account")
    is_bot = (
        await session.execute(select(Contact.is_bot).where(Contact.id == conversation.contact_id))
    ).scalar_one_or_none()
    if is_bot and mode != MODE_IGNORE:
        raise ModeError("chats with bots are always ignored")
    previous = conversation.mode
    if previous == mode or (expect is not None and previous != expect):
        return ModeChange(conversation.id, conversation.contact_id, previous, previous, None)
    now = clock.now()
    conversation.mode = mode
    conversation.updated_at = now
    if mode == MODE_IGNORE:
        # Nothing more is processed for the chat: no summary job picks it up.
        conversation.pending_ai_since = None
    elif conversation.pending_ai_since is None and await has_unsummarized_content(
        session, account_id, conversation.id
    ):
        conversation.pending_ai_since = now
    entry = AuditLogEntry(
        account_id=account_id,
        at=now,
        actor=actor,
        event=EVENT_MODE_CHANGED,
        subject_type="conversation",
        subject_id=conversation.id,
        details={"from": previous, "to": mode, **(details or {})},
    )
    session.add(entry)
    await session.flush()
    return ModeChange(conversation.id, conversation.contact_id, previous, mode, entry.id)


async def counts_by_mode(session: AsyncSession, account_id: int) -> dict[str, int]:
    """How many of the account's chats (bots excluded) are in each mode."""
    rows = await session.execute(
        select(Conversation.mode, func.count())
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.account_id == account_id)
        .where(Contact.is_bot.is_(False))
        .group_by(Conversation.mode)
    )
    return {str(mode): int(count) for mode, count in rows.all()}


@dataclass(frozen=True, slots=True)
class ChatEntry:
    conversation_id: int
    business_chat_id: int
    #: Owner-facing only (control chat): the contact's chosen name.
    display_name: str | None
    username: str | None


async def chats_in_mode(
    session: AsyncSession, account_id: int, mode: str, *, offset: int, limit: int
) -> list[ChatEntry]:
    """One page of the account's chats (bots excluded) in `mode`, most
    recently active first."""
    activity = func.greatest(
        func.coalesce(Conversation.last_contact_msg_at, Conversation.created_at),
        func.coalesce(Conversation.last_owner_msg_at, Conversation.created_at),
    )
    rows = await session.execute(
        select(
            Conversation.id, Conversation.business_chat_id, Contact.display_name, Contact.username
        )
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.account_id == account_id)
        .where(Conversation.mode == mode)
        .where(Contact.is_bot.is_(False))
        .order_by(activity.desc(), Conversation.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return [ChatEntry(int(r[0]), int(r[1]), r[2], r[3]) for r in rows.all()]
