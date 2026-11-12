"""Structural owner-intent counters; callers hold account/conversation locks."""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry, ControlTurn
from assistant.conversations.models import Conversation
from assistant.core.errors import NotInAccountError


async def advance_intent(session: AsyncSession, *, account_id: int, conversation_id: int) -> int:
    revision = await session.scalar(
        update(Conversation)
        .where(Conversation.account_id == account_id, Conversation.id == conversation_id)
        .values(
            owner_intent_revision=Conversation.owner_intent_revision + 1,
            pending_draft_at=None,
        )
        .returning(Conversation.owner_intent_revision)
    )
    if revision is None:
        raise NotInAccountError("conversation not found")
    return int(revision)


async def select_legacy_intent(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    turn_id: int,
    now: datetime,
) -> int:
    """Select a legacy explicit turn once, including retries after a slow call.

    The account/conversation lock serializes this scoped audit lookup and bump.
    C1 requests use their persisted intent_revision instead.
    """
    owner_turn = await session.scalar(
        select(ControlTurn.id).where(
            ControlTurn.account_id == account_id,
            ControlTurn.id == turn_id,
            ControlTurn.role == "owner",
        )
    )
    if owner_turn is None:
        raise NotInAccountError("owner turn not found")
    selected = await session.scalar(
        select(AuditLogEntry.details["intent_revision"].as_integer()).where(
            AuditLogEntry.account_id == account_id,
            AuditLogEntry.subject_type == "control_turn",
            AuditLogEntry.subject_id == turn_id,
            AuditLogEntry.event == "owner_intent_selected",
            AuditLogEntry.details["conversation_id"].astext == str(conversation_id),
        )
    )
    if selected is not None:
        return int(selected)
    revision = await advance_intent(session, account_id=account_id, conversation_id=conversation_id)
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=now,
            actor="owner",
            event="owner_intent_selected",
            subject_type="control_turn",
            subject_id=turn_id,
            details={"conversation_id": conversation_id, "intent_revision": revision},
        )
    )
    await session.flush()
    return revision
