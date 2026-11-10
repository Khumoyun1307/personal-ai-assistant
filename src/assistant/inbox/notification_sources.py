"""Freeze a verdict at enqueue; distinguish an event from a current request."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.conversations.models import Conversation, Message
from assistant.inbox.models import ConversationTriage, NotificationSource, Task

UNAVAILABLE = "The original explanation is no longer available (retention or deletion)."
HISTORICAL = "This earlier condition was superseded or resolved; check the chat for current state."


@dataclass(frozen=True, slots=True)
class EventExplanation:
    reasons: tuple[str, ...]
    current: bool
    note: str | None = None


def _state(verdict: ConversationTriage) -> dict[str, Any]:
    return {
        "needs_reply": verdict.needs_reply,
        "requests_decision": verdict.requests_decision,
        "urgency": verdict.urgency,
        "escalation_category": verdict.escalation_category,
    }


async def bind_source(
    session: AsyncSession,
    *,
    account_id: int,
    notification_id: int,
    conversation_id: int,
    expected_through: int | None = None,
) -> None:
    """Copy only the verdict already stored by this transaction, never new content."""
    await session.flush()
    verdict = await session.scalar(
        select(ConversationTriage).where(
            ConversationTriage.account_id == account_id,
            ConversationTriage.conversation_id == conversation_id,
        )
    )
    if verdict is not None and (
        expected_through is None or verdict.covers_through_message_id == expected_through
    ):
        session.add(
            NotificationSource(
                notification_id=notification_id,
                account_id=account_id,
                conversation_id=conversation_id,
                verdict_id=verdict.id,
                covers_through_message_id=verdict.covers_through_message_id,
                run_id=verdict.run_id,
                reasons=list(verdict.reasons),
                verdict_state=_state(verdict),
                # A delayed enqueue must not extend the verdict's content retention.
                created_at=verdict.updated_at,
            )
        )


async def explanation(
    session: AsyncSession,
    *,
    account_id: int,
    notification_id: int,
    conversation_id: int,
    refs: Mapping[str, Any],
) -> EventExplanation:
    source = await session.scalar(
        select(NotificationSource).where(
            NotificationSource.account_id == account_id,
            NotificationSource.notification_id == notification_id,
            NotificationSource.conversation_id == conversation_id,
        )
    )
    if source is None:
        return EventExplanation((), False, UNAVAILABLE)
    reply_boundary = source.created_at
    message_ids = refs.get("message_ids", [])
    if isinstance(message_ids, list) and message_ids:
        retained = (
            await session.execute(
                select(Message.id, Message.sent_at, Message.direction).where(
                    Message.account_id == account_id,
                    Message.conversation_id == conversation_id,
                    Message.id.in_(message_ids),
                    Message.deleted_at.is_(None),
                    Message.purged_at.is_(None),
                )
            )
        ).all()
        if {row.id for row in retained} != set(message_ids):
            return EventExplanation((), False, UNAVAILABLE)
        contact_times = [row.sent_at for row in retained if row.direction == "contact"]
        if contact_times:
            reply_boundary = max(contact_times)
    current = await session.scalar(
        select(ConversationTriage).where(
            ConversationTriage.account_id == account_id,
            ConversationTriage.conversation_id == conversation_id,
        )
    )
    if current is None:
        return EventExplanation((), False, UNAVAILABLE)
    chat = await session.scalar(
        select(Conversation).where(
            Conversation.account_id == account_id, Conversation.id == conversation_id
        )
    )
    task_ids = refs.get("task_ids", [])
    resolved_task = False
    if isinstance(task_ids, list) and task_ids:
        open_ids = set(
            await session.scalars(
                select(Task.id).where(
                    Task.account_id == account_id,
                    Task.conversation_id == conversation_id,
                    Task.id.in_(task_ids),
                    Task.status == "open",
                )
            )
        )
        resolved_task = open_ids != set(task_ids)
    active = (
        current.id == source.verdict_id
        and current.run_id == source.run_id
        and current.covers_through_message_id == source.covers_through_message_id
        and current.reasons == source.reasons
        and _state(current) == source.verdict_state
        and not resolved_task
        and chat is not None
        and chat.autonomy_paused_reason != "takeover"
        and (chat.last_owner_msg_at is None or chat.last_owner_msg_at < reply_boundary)
        and (chat.last_assistant_msg_at is None or chat.last_assistant_msg_at < reply_boundary)
    )
    return EventExplanation(tuple(source.reasons), active, None if active else HISTORICAL)
