"""Owner-facing alert texts, rendered at delivery from ids (S05.2 §3.3, P2-T12).

`notifications` rows hold no content: a renderer reads the rows its `refs`
name — the contact's name, the triggering verdict snapshot, a task — inside
the delivery transaction. A vanished chat suppresses the alert; expired
source content produces an explicit content-free availability notice.

Each alert says what happened, why (the verdict's reasons, as triage wrote
them) and what the owner can do next (PRD §14). Never message text: only
the names, reasons and task titles the product already shows elsewhere,
HTML-escaped. Deterministic wording follows the saved owner locale (I28).
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
from assistant.core.channel import Button
from assistant.core.control_texts import text
from assistant.inbox.models import Notification, Task
from assistant.inbox.notification_sources import UNAVAILABLE, explanation
from assistant.inbox.notification_types import NotificationKind
from assistant.inbox.tasks import resolution_ids
from assistant.memory.summaries import preferred_name


@dataclass(frozen=True, slots=True)
class RenderedAlert:
    text: str
    #: The conversation the alert is about (its "Open chat" button), if any.
    conversation_id: int | None = None
    #: Buttons the renderer adds after the delivery's own (ICR-4: a standing
    #: monitor's Stop, S05.4 §3.7). Empty for every other alert.
    extra_buttons: tuple[Button, ...] = ()


Renderer = Callable[[AsyncSession, Notification, Account], Awaitable[RenderedAlert | None]]


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


async def _contact_name(
    session: AsyncSession, account_id: int, conversation_id: int, locale: str | None = None
) -> str | None:
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
    return html.escape(preferred_name(row[0], row[1]) or text("alert_unnamed", locale))


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
    name = await _contact_name(session, account.id, conversation_id, account.locale_pref)
    if name is None:
        return None
    event = await explanation(
        session,
        account_id=account.id,
        notification_id=notification.id,
        conversation_id=conversation_id,
        refs=notification.refs,
    )
    if event.current:
        lines = [
            text(f"{kind.value}_headline", account.locale_pref, name=name),
            *_reasons(event.reasons),
            "",
            text(f"{kind.value}_next", account.locale_pref),
        ]
    else:
        lines = [
            text(
                "triage_earlier",
                account.locale_pref,
                name=name,
                event=text(f"{kind.value}_event", account.locale_pref),
            ),
            *_reasons(event.reasons),
            "",
            text(
                "triage_unavailable" if event.note == UNAVAILABLE else "triage_historical",
                account.locale_pref,
            ),
        ]
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
            select(Task.title, Task.conversation_id, Task.status, Task.details)
            .where(Task.account_id == account.id)
            .where(Task.id == task_ids[0])
        )
    ).first()
    if task is None:
        return None
    title, conversation_id, status, details = task
    name = (
        await _contact_name(session, account.id, conversation_id, account.locale_pref)
        if conversation_id is not None
        else None
    )
    who = f" ({name})" if name else ""
    if notification.kind == NotificationKind.TASK_CLOSED:
        body = text("task_closed", account.locale_pref, title=html.escape(title), who=who)
        evidence = resolution_ids(details)
        if evidence:
            body += "\n" + text(
                "task_evidence",
                account.locale_pref,
                evidence=", ".join(f"[msg:{value}]" for value in evidence),
            )
    elif status != "open":
        body = text(
            "task_earlier",
            account.locale_pref,
            title=html.escape(title),
            who=who,
            status=text(f"task_status_{status}", account.locale_pref),
        )
    else:
        body = text("task_due", account.locale_pref, title=html.escape(title), who=who)
    return RenderedAlert(body, conversation_id)


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
