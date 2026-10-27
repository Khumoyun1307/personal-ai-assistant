"""Owner-facing alert texts, rendered at delivery from ids (S05.2 §3.3, P2-T12).

`notifications` rows hold no content: a renderer reads the rows its `refs`
name — the contact's name, the chat's latest triage verdict, a task — inside
the delivery transaction, and returns None when one of them is gone (the
chat's data was deleted), so the alert is suppressed rather than sent about
something that no longer exists.

Each alert says what happened, why (the verdict's reasons, as triage wrote
them) and what the owner can do next (PRD §14). Never message text: only
the names, reasons and task titles the product already shows elsewhere,
HTML-escaped. English in P2 (P2-O11).
"""

from __future__ import annotations

import html
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account
from assistant.conversations.models import Contact, Conversation
from assistant.inbox.models import ConversationTriage, Notification, Task
from assistant.inbox.notification_types import NotificationKind
from assistant.memory.summaries import preferred_name

#: A contact without a usable name.
UNNAMED = "A contact"


@dataclass(frozen=True, slots=True)
class RenderedAlert:
    text: str
    #: The conversation the alert is about (its "Open chat" button), if any.
    conversation_id: int | None = None


Renderer = Callable[[AsyncSession, Notification, Account], Awaitable[RenderedAlert | None]]

_HEADLINES: Mapping[NotificationKind, str] = {
    NotificationKind.TRIAGE_URGENT: "🚨 <b>{name}: urgent.</b>",
    NotificationKind.TRIAGE_DECISION: "🔔 <b>{name} needs a decision from you.</b>",
    NotificationKind.TRIAGE_NEEDS_REPLY: "💬 <b>{name} is waiting for a reply.</b>",
    NotificationKind.TRIAGE_SUSPICIOUS: (
        "⚠️ <b>{name}'s messages look like an attempt to instruct the assistant</b> — "
        "I did not act on them."
    ),
}
_NEXT_STEPS: Mapping[NotificationKind, str] = {
    NotificationKind.TRIAGE_URGENT: "Urgent alerts reach you right away. Open the chat to reply.",
    NotificationKind.TRIAGE_DECISION: "Open the chat to answer.",
    NotificationKind.TRIAGE_NEEDS_REPLY: "Open the chat to reply.",
    NotificationKind.TRIAGE_SUSPICIOUS: (
        "Nothing was changed. You may want to look at the chat yourself."
    ),
}


def _ids(refs: Mapping[str, Any], key: str) -> list[int]:
    value = refs.get(key)
    if not isinstance(value, list):
        return []
    return [i for i in value if isinstance(i, int) and not isinstance(i, bool)]


def _conversation_id(notification: Notification) -> int | None:
    if notification.conversation_id is not None:
        return notification.conversation_id
    ids = _ids(notification.refs, "conversation_ids")
    return ids[0] if ids else None


async def _contact_name(session: AsyncSession, account_id: int, conversation_id: int) -> str | None:
    """The chat's contact name, escaped; None when the chat is gone."""
    row = (
        await session.execute(
            select(Contact.display_name, Contact.username)
            .join(Conversation, Conversation.contact_id == Contact.id)
            .where(Conversation.account_id == account_id)
            .where(Contact.account_id == account_id)
            .where(Conversation.id == conversation_id)
        )
    ).first()
    if row is None:
        return None
    return html.escape(preferred_name(row[0], row[1]) or UNNAMED)


def _reasons(reasons: Sequence[Any]) -> list[str]:
    return [f"• {html.escape(r)}" for r in reasons if isinstance(r, str) and r.strip()]


async def render_triage(
    session: AsyncSession, notification: Notification, account: Account
) -> RenderedAlert | None:
    """A triage alert: the contact, the verdict's reasons, the next step."""
    kind = NotificationKind(notification.kind)
    conversation_id = _conversation_id(notification)
    if conversation_id is None:
        return None
    name = await _contact_name(session, account.id, conversation_id)
    verdict = (
        await session.execute(
            select(ConversationTriage.reasons)
            .where(ConversationTriage.account_id == account.id)
            .where(ConversationTriage.conversation_id == conversation_id)
        )
    ).scalar_one_or_none()
    if name is None or verdict is None:
        return None
    lines = [_HEADLINES[kind].format(name=name), *_reasons(verdict), "", _NEXT_STEPS[kind]]
    return RenderedAlert("\n".join(lines), conversation_id)


async def render_task(
    session: AsyncSession, notification: Notification, account: Account
) -> RenderedAlert | None:
    """A task alert (closed automatically, or due): its title and contact."""
    task_ids = _ids(notification.refs, "task_ids")
    if not task_ids:
        return None
    task = (
        await session.execute(
            select(Task.title, Task.conversation_id)
            .where(Task.account_id == account.id)
            .where(Task.id == task_ids[0])
        )
    ).first()
    if task is None:
        return None
    title, conversation_id = task
    name = (
        await _contact_name(session, account.id, conversation_id)
        if conversation_id is not None
        else None
    )
    who = f" ({name})" if name else ""
    if notification.kind == NotificationKind.TASK_CLOSED:
        text = (
            f"✅ Closed automatically: {html.escape(title)}{who}\n\n"
            "A later message in the chat showed it done or answered."
        )
    else:
        text = f"⏰ Due: {html.escape(title)}{who}"
    return RenderedAlert(text, conversation_id)


#: The built-in renderers; S05.4 registers `MONITOR_MATCH` on the delivery
#: service at wiring time.
RENDERERS: Mapping[NotificationKind, Renderer] = {
    NotificationKind.TRIAGE_URGENT: render_triage,
    NotificationKind.TRIAGE_DECISION: render_triage,
    NotificationKind.TRIAGE_NEEDS_REPLY: render_triage,
    NotificationKind.TRIAGE_SUSPICIOUS: render_triage,
    NotificationKind.TASK_CLOSED: render_task,
    NotificationKind.COMMITMENT_DUE: render_task,
}
