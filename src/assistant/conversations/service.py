"""Storing managed conversations: the seven-step transaction (ARCHITECTURE §6.5).

Every entry point runs inside the caller's transaction (the inbox row's), so
a message and the update it came from commit together or not at all. Writes to
one conversation are serialised by locking its row (AGENTS §13, §9.4 lock
order: conversation before messages), never across a network call.

The rules that matter most, each pinned by an integration test:

- state comes from **new** messages only, using Telegram's `date` — an edit,
  however late, never moves the reply window (TF §12 LV-08/LV-15);
- a no-op edit (same content hash) touches nothing, not even `edited_at`;
- a deletion purges content and leaves a tombstone, so a requeued or
  redelivered update cannot bring the content back, and drops the summaries
  that covered it (S03.2);
- content is stored whether or not `ai_managed` consent exists; the consent
  gate is at the LLM boundary (§14.3). `pending_ai_since` only remembers that
  something new is waiting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, AuditLogEntry
from assistant.accounts.preferences import FALLBACK_DEFAULT_MODE, default_mode
from assistant.accounts.service import active_consent
from assistant.conversations.events import (
    Direction,
    ObservedDeletion,
    ObservedMessage,
)
from assistant.conversations.models import (
    Contact,
    Conversation,
    CoverageGap,
    Message,
)
from assistant.core.clock import Clock
from assistant.inbox.models import InboundUpdate
from assistant.memory.search import detect_script, guess_lang, normalize
from assistant.memory.summaries import invalidate_covering
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

PAUSE_TAKEOVER = "takeover"
PAUSE_INGEST_ERROR = "ingest_error"


class Effect(StrEnum):
    INSERTED = "inserted"
    """A new message, stored."""

    UPDATED = "updated"
    """An edit that changed the content."""

    UNCHANGED = "unchanged"
    """A no-op edit, an edit of a deleted message: nothing written."""

    DUPLICATE = "duplicate"
    """A redelivery of a message already stored: nothing written."""

    RESURFACED = "resurfaced"
    """A new message whose id was already a tombstone (a requeue after a
    deletion): content stays purged, the message still counts for state."""

    PRE_COVERAGE_EDIT = "pre_coverage_edit"
    """An edit of a message we never saw: stored as an edit, no state change."""


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    conversation_id: int
    message_id: int | None
    effect: Effect
    metadata_only: bool


@dataclass(frozen=True, slots=True)
class DeletionOutcome:
    conversation_id: int | None
    purged_message_ids: tuple[int, ...] = ()
    tombstoned_telegram_ids: tuple[int, ...] = ()
    #: The conversation is in Ignore mode: the update's payload is not kept.
    metadata_only: bool = False
    #: Messages that still held content until this deletion (a subset of
    #: `purged_message_ids`, which also lists rows already deleted).
    content_lost_ids: tuple[int, ...] = ()


# ------------------------------------------------------------------ helpers
def _content_columns(event: ObservedMessage, *, metadata_only: bool) -> dict[str, Any]:
    """The content-bearing columns for a message, or their absence."""
    if metadata_only:
        return {
            "text": None,
            "media": None,
            "search_key": None,
            "script": None,
            "lang_guess": None,
            "content_hash": None,
        }
    text = event.content.text
    return {
        "text": text,
        "media": event.content.media.as_json() if event.content.media else None,
        "search_key": normalize(text) if text else None,
        "script": detect_script(text) if text else None,
        "lang_guess": guess_lang(text) if text else None,
        "content_hash": event.content.content_hash,
    }


async def _upsert_contact(
    session: AsyncSession,
    *,
    account_id: int,
    telegram_user_id: int,
    display_name: str | None,
    username: str | None,
    is_bot: bool,
    clock: Clock,
    write_names: bool,
) -> int:
    values: dict[str, Any] = {
        "account_id": account_id,
        "telegram_user_id": telegram_user_id,
        "is_bot": is_bot,
    }
    if write_names:
        values["display_name"] = display_name
        values["username"] = username
    statement = insert(Contact).values(**values)
    on_conflict: dict[str, Any] = {
        "is_bot": Contact.is_bot | statement.excluded.is_bot,
        "updated_at": clock.now(),
    }
    if write_names:
        # Names are refreshed from messages (§10.3); a message without a name
        # does not erase one we have.
        on_conflict["display_name"] = func.coalesce(
            statement.excluded.display_name, Contact.display_name
        )
        on_conflict["username"] = func.coalesce(statement.excluded.username, Contact.username)
    upsert = statement.on_conflict_do_update(
        constraint="uq_contacts_account_id_telegram_user_id", set_=on_conflict
    ).returning(Contact.id)
    return int((await session.execute(upsert)).scalar_one())


async def new_chat_mode(session: AsyncSession, account_id: int) -> str:
    """The mode a conversation created now starts in: the owner's default
    (PRD §11.1; /settings, S03.3), Observe unless they chose otherwise."""
    account = await session.get(Account, account_id)
    return FALLBACK_DEFAULT_MODE if account is None else default_mode(account)


async def _lock_conversation(
    session: AsyncSession,
    *,
    account_id: int,
    business_chat_id: int,
    contact_id: int,
    mode: str | None = None,
) -> Conversation:
    """Upsert, then lock: every write to a conversation goes through here.
    `mode` applies only if the conversation is created now."""
    values: dict[str, Any] = {
        "account_id": account_id,
        "business_chat_id": business_chat_id,
        "contact_id": contact_id,
    }
    values["mode"] = mode or await new_chat_mode(session, account_id)
    await session.execute(
        insert(Conversation)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_conversations_account_id_business_chat_id")
    )
    result = await session.execute(
        select(Conversation)
        .where(Conversation.account_id == account_id)
        .where(Conversation.business_chat_id == business_chat_id)
        .with_for_update()
    )
    return result.scalar_one()


async def _find_conversation(
    session: AsyncSession, *, account_id: int, business_chat_id: int
) -> Conversation | None:
    result = await session.execute(
        select(Conversation)
        .where(Conversation.account_id == account_id)
        .where(Conversation.business_chat_id == business_chat_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


def _later(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None or candidate > current else current


def _earlier(current: datetime | None, candidate: datetime) -> datetime:
    return candidate if current is None or candidate < current else current


async def _audit(
    session: AsyncSession, *, account_id: int, event: str, conversation_id: int, at: datetime
) -> None:
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=at,
            actor="system",
            event=event,
            subject_type="conversation",
            subject_id=conversation_id,
        )
    )


# ------------------------------------------------------------- record_message
async def record_message(
    session: AsyncSession,
    *,
    account_id: int,
    event: ObservedMessage,
    source_update_id: int | None,
    clock: Clock,
) -> IngestOutcome:
    """Steps 1-5 of §6.5 for one observed message."""
    now = clock.now()
    # Step 1: contact and conversation, then the lock.
    conversation_probe = await _find_conversation(
        session, account_id=account_id, business_chat_id=event.business_chat_id
    )
    # A new chat starts in the owner's default mode; in Ignore not even the
    # contact's name is stored (S03.3).
    mode = (
        conversation_probe.mode
        if conversation_probe is not None
        else await new_chat_mode(session, account_id)
    )
    metadata_only = mode == "ignore"
    contact_id = await _upsert_contact(
        session,
        account_id=account_id,
        telegram_user_id=event.counterpart.telegram_user_id,
        display_name=event.counterpart.display_name,
        username=event.counterpart.username,
        is_bot=event.counterpart.is_bot,
        clock=clock,
        write_names=not metadata_only,
    )
    conversation = await _lock_conversation(
        session,
        account_id=account_id,
        business_chat_id=event.business_chat_id,
        contact_id=contact_id,
        mode=mode,
    )
    metadata_only = conversation.mode == "ignore"

    # Step 2: the message.
    if event.is_edit:
        message_id, effect = await _apply_edit(
            session,
            conversation,
            event,
            now=now,
            metadata_only=metadata_only,
            source_update_id=source_update_id,
        )
    else:
        message_id, effect = await _apply_new(
            session,
            conversation,
            event,
            now=now,
            metadata_only=metadata_only,
            source_update_id=source_update_id,
        )

    # Step 3: state, from new messages only, using Telegram's date.
    if effect in (Effect.INSERTED, Effect.RESURFACED):
        _advance_state(conversation, event)
        # Step 4: takeover.
        takeover = event.direction is Direction.OWNER_MANUAL
        if takeover and conversation.autonomy_paused_reason is None:
            conversation.autonomy_paused_reason = PAUSE_TAKEOVER
            await _audit(
                session,
                account_id=account_id,
                event="takeover_detected",
                conversation_id=conversation.id,
                at=now,
            )

    # Step 5: something new for the AI to look at, once consent allows.
    content_changed = effect in (Effect.INSERTED, Effect.UPDATED) and not metadata_only
    if effect is Effect.UPDATED and not metadata_only and message_id is not None:
        # The pre-edit text must not live on in a summary either (M03 D66).
        await invalidate_covering(session, conversation, [message_id], now=now)
    if content_changed and conversation.pending_ai_since is None:
        conversation.pending_ai_since = now
    conversation.updated_at = now

    logger.info(
        "business_message_recorded",
        conversation_id=conversation.id,
        direction=event.direction.value,
        effect=effect.value,
        metadata_only=metadata_only,
    )
    return IngestOutcome(conversation.id, message_id, effect, metadata_only)


def _advance_state(conversation: Conversation, event: ObservedMessage) -> None:
    sent_at = event.sent_at
    match event.direction:
        case Direction.CONTACT:
            conversation.last_contact_msg_at = _later(conversation.last_contact_msg_at, sent_at)
        case Direction.OWNER_MANUAL:
            conversation.last_owner_msg_at = _later(conversation.last_owner_msg_at, sent_at)
        case Direction.ASSISTANT_SENT:
            conversation.last_assistant_msg_at = _later(conversation.last_assistant_msg_at, sent_at)
        case Direction.OWNER_AUTOMATED:
            pass
    conversation.coverage_started_at = _earlier(conversation.coverage_started_at, sent_at)
    # A delivered message proves the chat is neither paused nor excluded (§6.6).
    conversation.availability = "active"


async def _apply_new(
    session: AsyncSession,
    conversation: Conversation,
    event: ObservedMessage,
    *,
    now: datetime,
    metadata_only: bool,
    source_update_id: int | None,
) -> tuple[int | None, Effect]:
    statement = (
        insert(Message)
        .values(
            account_id=conversation.account_id,
            conversation_id=conversation.id,
            telegram_message_id=event.telegram_message_id,
            direction=event.direction.value,
            observed_via="new",
            sent_at=event.sent_at,
            edited_at=event.edited_at,
            reply_to_telegram_message_id=event.reply_to_telegram_message_id,
            content_type=event.content.content_type,
            source_update_id=source_update_id,
            created_at=now,
            updated_at=now,
            **_content_columns(event, metadata_only=metadata_only),
        )
        .on_conflict_do_nothing(constraint="uq_messages_conversation_id_telegram_message_id")
        .returning(Message.id)
    )
    inserted = (await session.execute(statement)).scalar_one_or_none()
    if inserted is not None:
        return int(inserted), Effect.INSERTED
    existing = await _message_for_update(session, conversation.id, event.telegram_message_id)
    if existing is None:  # pragma: no cover - only under a concurrent delete
        return None, Effect.DUPLICATE
    if existing.deleted_at is not None:
        return existing.id, Effect.RESURFACED
    return existing.id, Effect.DUPLICATE


async def _apply_edit(
    session: AsyncSession,
    conversation: Conversation,
    event: ObservedMessage,
    *,
    now: datetime,
    metadata_only: bool,
    source_update_id: int | None,
) -> tuple[int | None, Effect]:
    existing = await _message_for_update(session, conversation.id, event.telegram_message_id)
    if existing is None:
        # An edit of a message from before coverage (TF §3.2): keep it as an
        # edit, without touching the window or the owner timestamps.
        message = Message(
            account_id=conversation.account_id,
            conversation_id=conversation.id,
            telegram_message_id=event.telegram_message_id,
            direction=event.direction.value,
            observed_via="edit",
            sent_at=event.sent_at,
            edited_at=event.edited_at or now,
            reply_to_telegram_message_id=event.reply_to_telegram_message_id,
            content_type=event.content.content_type,
            source_update_id=source_update_id,
            created_at=now,
            updated_at=now,
            **_content_columns(event, metadata_only=metadata_only),
        )
        session.add(message)
        await session.flush()
        return message.id, Effect.PRE_COVERAGE_EDIT
    if existing.deleted_at is not None or existing.purged_at is not None:
        # Deleted or expired: an edit must not bring content back.
        return existing.id, Effect.UNCHANGED
    if metadata_only:
        return existing.id, Effect.UNCHANGED
    if existing.content_hash == event.content.content_hash:
        return existing.id, Effect.UNCHANGED
    for column, value in _content_columns(event, metadata_only=False).items():
        setattr(existing, column, value)
    existing.content_type = event.content.content_type
    existing.edited_at = event.edited_at or now
    existing.updated_at = now
    return existing.id, Effect.UPDATED


async def _message_for_update(
    session: AsyncSession, conversation_id: int, telegram_message_id: int
) -> Message | None:
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .where(Message.telegram_message_id == telegram_message_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


# ------------------------------------------------------------ record_deletion
async def record_deletion(
    session: AsyncSession,
    *,
    account_id: int,
    event: ObservedDeletion,
    clock: Clock,
    tombstones: bool = True,
) -> DeletionOutcome:
    """Step 6 of §6.5: purge content, keep tombstones, clear linked payloads.

    The payload never says who deleted, or whether it was for both sides
    (TF §12 LV-07), so nothing here attributes the deletion. `tombstones=False`
    purges what exists without writing rows for ids never stored — for the
    dead-letter fallback when storage is off (§6 invariant 7).
    """
    conversation = await _find_conversation(
        session, account_id=account_id, business_chat_id=event.business_chat_id
    )
    if conversation is None or not event.telegram_message_ids:
        # Nothing stored for this chat: nothing to purge, nothing to remember.
        return DeletionOutcome(None)
    now = clock.now()
    # Telegram may list an id more than once; two tombstones for one id
    # would violate the unique key and dead-letter the deletion.
    ids = list(dict.fromkeys(event.telegram_message_ids))
    # Rows that still hold content: only these lose something now. A
    # redelivered deletion or a never-stored id must not invalidate a summary
    # (and force a paid rebuild) — the conversation row lock orders this read
    # against any concurrent edit of the chat.
    content_lost = tuple(
        int(message_id)
        for message_id in (
            await session.execute(
                select(Message.id)
                .where(Message.conversation_id == conversation.id)
                .where(Message.telegram_message_id.in_(ids))
                .where((Message.text.is_not(None)) | (Message.media.is_not(None)))
            )
        ).scalars()
    )
    purged = await session.execute(
        update(Message)
        .where(Message.conversation_id == conversation.id)
        .where(Message.telegram_message_id.in_(ids))
        .values(
            text=None,
            media=None,
            search_key=None,
            script=None,
            lang_guess=None,
            content_hash=None,
            deleted_at=func.coalesce(Message.deleted_at, now),
            updated_at=now,
        )
        .returning(Message.id, Message.telegram_message_id)
    )
    rows = purged.all()
    purged_ids = tuple(int(row[0]) for row in rows)
    seen = {row[1] for row in rows if row[1] is not None}
    missing = tuple(i for i in ids if i not in seen) if tombstones else ()
    for telegram_id in missing:
        # A message we never stored: the tombstone stops a late requeue of
        # its original update from storing content that is gone for the owner.
        session.add(
            Message(
                account_id=account_id,
                conversation_id=conversation.id,
                telegram_message_id=telegram_id,
                observed_via="deletion",
                content_type="unknown",
                deleted_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    if purged_ids:
        await session.execute(
            update(InboundUpdate)
            .where(InboundUpdate.account_id == account_id)
            .where(InboundUpdate.message_id.in_(purged_ids))
            .values(payload=None)
        )
    # A deleted message must not live on in a summary (S03.2, M03 D63).
    await invalidate_covering(session, conversation, content_lost, now=now)
    conversation.updated_at = now
    logger.info(
        "business_messages_deleted",
        conversation_id=conversation.id,
        purged=len(purged_ids),
        tombstoned=len(missing),
    )
    return DeletionOutcome(
        conversation.id,
        purged_ids,
        missing,
        metadata_only=conversation.mode == "ignore",
        content_lost_ids=content_lost,
    )


# -------------------------------------------------------- ingest failures
async def record_ingest_failure(
    session: AsyncSession,
    *,
    account_id: int,
    business_chat_id: int,
    telegram_user_id: int,
    inbound_update_id: int,
    at: datetime,
    clock: Clock,
) -> int:
    """A business update for this chat was dead-lettered (§6.2, §6.6): the
    conversation is paused for autonomy and a definite gap is recorded,
    idempotently per update row. Returns the conversation id."""
    contact_id = await _upsert_contact(
        session,
        account_id=account_id,
        telegram_user_id=telegram_user_id,
        display_name=None,
        username=None,
        is_bot=False,
        clock=clock,
        write_names=False,
    )
    conversation = await _lock_conversation(
        session, account_id=account_id, business_chat_id=business_chat_id, contact_id=contact_id
    )
    if conversation.autonomy_paused_reason is None:
        conversation.autonomy_paused_reason = PAUSE_INGEST_ERROR
    existing = await session.execute(
        select(CoverageGap.id).where(CoverageGap.inbound_update_id == inbound_update_id)
    )
    if existing.scalar_one_or_none() is None:
        session.add(
            CoverageGap(
                account_id=account_id,
                conversation_id=conversation.id,
                kind="dead_letter",
                certainty="definite",
                at_risk_from=at,
                at_risk_to=at,
                inbound_update_id=inbound_update_id,
                created_at=clock.now(),
            )
        )
        await _audit(
            session,
            account_id=account_id,
            event="ingest_error",
            conversation_id=conversation.id,
            at=clock.now(),
        )
    conversation.updated_at = clock.now()
    return conversation.id


async def resolve_ingest_failure(
    session: AsyncSession, *, account_id: int, inbound_update_id: int, clock: Clock
) -> None:
    """The update went through after a requeue: close its gap, and lift the
    ingest pause once no unresolved dead letter remains for the chat."""
    gap = (
        await session.execute(
            select(CoverageGap)
            .where(CoverageGap.account_id == account_id)
            .where(CoverageGap.inbound_update_id == inbound_update_id)
            .where(CoverageGap.resolved_at.is_(None))
        )
    ).scalar_one_or_none()
    if gap is None:
        return
    gap.resolved_at = clock.now()
    if gap.conversation_id is None:
        return
    remaining = (
        await session.execute(
            select(func.count())
            .select_from(CoverageGap)
            .where(CoverageGap.conversation_id == gap.conversation_id)
            .where(CoverageGap.kind == "dead_letter")
            .where(CoverageGap.resolved_at.is_(None))
            .where(CoverageGap.id != gap.id)
        )
    ).scalar_one()
    if remaining == 0:
        conversation = await session.get(Conversation, gap.conversation_id, with_for_update=True)
        if conversation is not None and conversation.autonomy_paused_reason == PAUSE_INGEST_ERROR:
            conversation.autonomy_paused_reason = None
            conversation.updated_at = clock.now()


# ------------------------------------------------------------ bot chats
async def is_bot_chat(session: AsyncSession, *, account_id: int, business_chat_id: int) -> bool:
    """A chat already recognised as one with a third-party bot (FR-OBS-04):
    nothing from it is stored, whoever writes and whatever its mode."""
    result = await session.execute(
        select(Contact.is_bot)
        .join(Conversation, Conversation.contact_id == Contact.id)
        .where(Conversation.account_id == account_id)
        .where(Conversation.business_chat_id == business_chat_id)
    )
    return result.scalar_one_or_none() is True


async def mark_bot_chat(
    session: AsyncSession, *, account_id: int, business_chat_id: int, clock: Clock
) -> int | None:
    """A bot answered in this chat (§6.5 `ignored_bot_chat`).

    A private `Chat` carries no `is_bot`, so the owner's own messages to a
    bot are stored until the bot's first answer reveals it (M02 D37). Then
    the contact is marked a bot, the conversation goes to Ignore, and what was
    stored from it is purged. A chat we never stored stays unknown: no row is
    created for it (RG-02). Returns the conversation id, or None."""
    conversation = await _find_conversation(
        session, account_id=account_id, business_chat_id=business_chat_id
    )
    if conversation is None:
        return None
    contact = await session.get(Contact, conversation.contact_id, with_for_update=True)
    if contact is not None and contact.is_bot and conversation.mode == "ignore":
        return conversation.id  # already recognised
    # Imported here: privacy depends on this package's models, not the reverse.
    from assistant.privacy.deletion import purge_conversation

    report = await purge_conversation(
        session,
        account_id=account_id,
        conversation_id=conversation.id,
        business_chat_id=business_chat_id,
        clock=clock,
    )
    if contact is not None:
        contact.is_bot = True
        contact.updated_at = clock.now()
    conversation.mode = "ignore"
    conversation.pending_ai_since = None
    conversation.updated_at = clock.now()
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=clock.now(),
            actor="system",
            event="bot_chat",
            subject_type="conversation",
            subject_id=conversation.id,
            details={"messages": report.messages, "payloads_cleared": report.payloads_cleared},
        )
    )
    logger.info(
        "bot_chat_recognised",
        conversation_id=conversation.id,
        purged=report.messages,
        payloads_cleared=report.payloads_cleared,
    )
    return conversation.id


# ------------------------------------------------------------ consent hook
async def conversations_awaiting_ai(session: AsyncSession, account_id: int) -> list[int]:
    """Conversations with unprocessed content — empty unless `ai_managed`
    consent is active (§14.3). The first consumer is the M03 summary
    scheduler; in M02 it exists so the consent gate is testable end to end."""
    if not (await active_consent(session, account_id, kind="ai_managed")).granted:
        return []
    result = await session.execute(
        select(Conversation.id)
        .where(Conversation.account_id == account_id)
        .where(Conversation.pending_ai_since.is_not(None))
        .where(Conversation.mode != "ignore")
        .order_by(Conversation.pending_ai_since)
    )
    return [int(row) for row in result.scalars().all()]
