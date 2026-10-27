"""P2 rows erased by the existing privacy deletion transactions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from sqlalchemy import ColumnElement, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions.lifecycle import cancel_pending, clear_texts
from assistant.actions.models import Action
from assistant.actions.states import PENDING, StateReason
from assistant.automation.models import Automation
from assistant.conversations.models import Conversation
from assistant.inbox.models import ConversationTriage, Notification, Task


async def purge_p2(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_ids: Sequence[int] | None,
    contact_ids: Sequence[int],
    scope: Literal["scoped", "everything", "managed"],
    now: datetime,
) -> dict[str, int]:
    """Cancel before clearing text, preserving claimed delivery states.

    The caller already holds account/conversation locks in the normal
    deletion order. Every statement keeps the explicit tenant predicate.
    Notifications/automations can name chats only in JSON refs, so deleting
    just their direct conversation column would leave derived data behind.
    """
    # Count the union once: a pending row whose text was already purged and
    # a settled row with retained text are distinct affected actions.
    affected = select(func.count(Action.id)).where(
        Action.account_id == account_id,
        or_(Action.state.in_(PENDING), Action.text.is_not(None)),
    )
    if conversation_ids is not None:
        affected = affected.where(Action.conversation_id.in_(conversation_ids))
    actions = int((await session.execute(affected)).scalar_one())
    await cancel_pending(
        session,
        account_id=account_id,
        conversation_ids=conversation_ids,
        reason=StateReason.DATA_DELETED,
        now=now,
    )
    await clear_texts(session, account_id=account_id, conversation_ids=conversation_ids)
    task_filter = Task.account_id == account_id
    triage_filter = ConversationTriage.account_id == account_id
    chat_filter = Conversation.account_id == account_id
    if conversation_ids is not None:
        task_filter &= Task.conversation_id.in_(conversation_ids)
        triage_filter &= ConversationTriage.conversation_id.in_(conversation_ids)
        chat_filter &= Conversation.id.in_(conversation_ids)
    if scope == "managed":
        task_filter &= Task.created_by == "agent"
    tasks = len((await session.execute(delete(Task).where(task_filter).returning(Task.id))).all())
    await session.execute(delete(ConversationTriage).where(triage_filter))
    notification_filter = Notification.account_id == account_id
    automation_filter = Automation.account_id == account_id
    if scope == "scoped":
        refs_notification: list[ColumnElement[bool]] = [
            Notification.conversation_id.in_(conversation_ids or ())
        ]
        refs_automation: list[ColumnElement[bool]] = []
        for conversation_id in conversation_ids or ():
            refs_notification.append(
                Notification.refs.contains({"conversation_ids": [conversation_id]})
            )
            refs_automation.append(
                Automation.refs.contains({"conversation_ids": [conversation_id]})
            )
        for contact_id in contact_ids:
            refs_notification.extend(
                (
                    Notification.contact_id == contact_id,
                    Notification.refs.contains({"contact_ids": [contact_id]}),
                )
            )
            refs_automation.append(Automation.refs.contains({"contact_ids": [contact_id]}))
        notification_filter &= or_(*refs_notification)
        automation_filter &= or_(False, *refs_automation)
    notifications = len(
        (
            await session.execute(
                delete(Notification).where(notification_filter).returning(Notification.id)
            )
        ).all()
    )
    automations = 0
    if scope != "managed":
        if scope == "everything":
            automation_filter &= Automation.kind != "digest"
        automations = len(
            (
                await session.execute(
                    delete(Automation).where(automation_filter).returning(Automation.id)
                )
            ).all()
        )
    await session.execute(
        update(Conversation)
        .where(chat_filter)
        .values(
            pending_draft_at=None,
            pending_triage_at=None,
            pending_monitor_at=None,
        )
    )
    return {
        "actions": actions,
        "tasks": tasks,
        "notifications": notifications,
        "automations": automations,
    }
