"""Monitors: which apply to a chat, what matches, and what a match does
(S05.4 §3.1, §3.3, §3.4, §3.6, §3.7; ARCHITECTURE §7.5, §10.3).

Domain code only: no model call, no Telegram call. A monitor is an
`automations` row of kind `monitor` whose `spec` is a `MonitorRule`.

- **Scope** (`applicable`): an active, unexpired monitor applies to a chat in
  Observe or Assist mode — never Ignore, never a bot chat — when it watches
  every observed chat, or names the chat or its contact. A message counts
  only when it is a new contact message sent at or after the monitor was
  confirmed (`MonitorView.covers`): a backlog message never fires it.
- **Deterministic matching** (`matches`): `contact_replied`, `keywords` (whole
  words, through `memory.search.normalize` on both sides, so scripts and
  apostrophes agree) and `media_received`. Semantic kinds are judged by the
  `evaluate_monitor` run instead (`agent/monitor_eval.py`).
- **A match** (`record_match`): a one-shot monitor completes with a
  conditional update, so it fires exactly once even for two messages of one
  burst; then one `MONITOR_MATCH` notification (priority `high`, deduplicated
  per monitor and message) and content-free audit rows.

Owner-facing texts (`describe`, the alert renderer) are plain words around
names and the owner's own terms; never message text.
"""

from __future__ import annotations

import html
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pydantic
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, AuditLogEntry
from assistant.automation import repository
from assistant.automation.models import Automation
from assistant.automation.rules import MonitorRule
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.core.errors import NotInAccountError
from assistant.core.jobs import JobScheduler
from assistant.inbox.alert_texts import RenderedAlert
from assistant.inbox.models import ConversationTriage, Notification
from assistant.inbox.notification_types import NotificationKind, NotificationRefs, Priority
from assistant.inbox.notifications import enqueue
from assistant.memory.summaries import preferred_name
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

KIND = "monitor"
#: Chat modes monitors watch (S05.4 §3.3).
WATCHED_MODES = frozenset({"observe", "assist"})
DETERMINISTIC_KINDS = frozenset({"contact_replied", "keywords", "media_received"})
SEMANTIC_KINDS = frozenset({"semantic", "decision_requested", "urgent"})
#: `pending_inputs` kind of the Stop card (F0-interfaces §7).
MON_CARD = "mon"
OP_STOP = "stop"

EVENT_CREATED = "monitor_created"
EVENT_MATCHED = "monitor_matched"
EVENT_COMPLETED = "monitor_completed"
EVENT_STOPPED = "monitor_stopped"
EVENT_EXPIRED = "monitor_expired"
EVENT_INVALID = "automation_invalid"

_WORD = re.compile(r"[a-z0-9]+")
#: A contact or chat without a usable name.
UNNAMED = "a contact"


@dataclass(frozen=True, slots=True)
class MonitorView:
    """A monitor as matching and listing read it (ids, rule, times)."""

    automation_id: int
    account_id: int
    status: str
    rule: MonitorRule
    confirmed_at: datetime | None
    expires_at: datetime | None
    created_at: datetime

    @property
    def semantic(self) -> bool:
        return self.rule.condition.kind in SEMANTIC_KINDS

    def covers(self, sent_at: datetime | None) -> bool:
        """A message counts only from the monitor's confirmation on (TL
        decision, S05.4 §3.3). Fail closed: never confirmed, never fires."""
        return (
            self.confirmed_at is not None and sent_at is not None and sent_at >= self.confirmed_at
        )


def parse_rule(spec: Mapping[str, Any] | None) -> MonitorRule | None:
    """The stored rule, or None when it no longer validates."""
    try:
        return MonitorRule.model_validate(dict(spec or {}))
    except pydantic.ValidationError:
        return None


def _view(row: Automation, rule: MonitorRule) -> MonitorView:
    return MonitorView(
        automation_id=row.id,
        account_id=row.account_id,
        status=row.status,
        rule=rule,
        confirmed_at=row.confirmed_at,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


async def _pause_invalid(session: AsyncSession, row: Automation, now: datetime) -> None:
    """A stored rule that no longer validates is paused, never guessed at
    (F0-interfaces §11 `automation_invalid`)."""
    changed = await session.scalar(
        update(Automation)
        .where(Automation.id == row.id)
        .where(Automation.account_id == row.account_id)
        .where(Automation.status == "active")
        .values(status="paused", updated_at=now)
        .returning(Automation.id)
    )
    if changed is not None:
        _audit(session, row.account_id, now, EVENT_INVALID, row.id, {"kind": KIND})
        logger.warning("monitor_rule_invalid", automation_id=row.id)


@dataclass(frozen=True, slots=True)
class ChatFacts:
    contact_id: int
    mode: str
    is_bot: bool


async def chat_facts(
    session: AsyncSession, *, account_id: int, conversation_id: int
) -> ChatFacts | None:
    row = (
        await session.execute(
            select(Conversation.contact_id, Conversation.mode, Contact.is_bot)
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(Conversation.account_id == account_id)
            .where(Contact.account_id == account_id)
            .where(Conversation.id == conversation_id)
        )
    ).first()
    if row is None:
        return None
    return ChatFacts(int(row[0]), str(row[1]), bool(row[2]))


async def applicable(
    session: AsyncSession, *, account_id: int, conversation_id: int, now: datetime
) -> list[MonitorView]:
    """The active, unexpired monitors that watch this chat (S05.4 §3.3);
    none for an Ignore-mode chat, a bot chat or a chat not of this account.
    The account's monitors are read first (one indexed query): the
    ingestion path of an owner without monitors stops there."""
    rows = (
        (
            await session.execute(
                select(Automation)
                .where(Automation.account_id == account_id)
                .where(Automation.kind == KIND)
                .where(Automation.status == "active")
                .where(or_(Automation.expires_at.is_(None), Automation.expires_at > now))
                .order_by(Automation.id)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    chat = await chat_facts(session, account_id=account_id, conversation_id=conversation_id)
    if chat is None or chat.is_bot or chat.mode not in WATCHED_MODES:
        return []
    found: list[MonitorView] = []
    for row in rows:
        rule = parse_rule(row.spec)
        if rule is None:
            await _pause_invalid(session, row, now)
            continue
        scope = rule.scope
        if (
            scope.all_observed
            or conversation_id in scope.conversation_ids
            or chat.contact_id in scope.contact_ids
        ):
            found.append(_view(row, rule))
    return found


# ------------------------------------------------------------------ matching
@dataclass(frozen=True, slots=True)
class MessageFacts:
    """What deterministic matching reads of one stored message."""

    message_id: int
    contact_id: int
    direction: str | None
    sent_at: datetime | None
    created_at: datetime
    search_key: str | None
    content_type: str
    media_kind: str | None


async def message_facts(
    session: AsyncSession, *, account_id: int, conversation_id: int, message_id: int
) -> MessageFacts | None:
    row = (
        await session.execute(
            select(
                Message.id,
                Conversation.contact_id,
                Message.direction,
                Message.sent_at,
                Message.created_at,
                Message.search_key,
                Message.content_type,
                Message.media,
            )
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Message.account_id == account_id)
            .where(Conversation.account_id == account_id)
            .where(Message.conversation_id == conversation_id)
            .where(Message.id == message_id)
        )
    ).first()
    if row is None:
        return None
    media = row[7] if isinstance(row[7], Mapping) else None
    kind = media.get("kind") if media else None
    return MessageFacts(
        message_id=int(row[0]),
        contact_id=int(row[1]),
        direction=row[2],
        sent_at=row[3],
        created_at=row[4],
        search_key=row[5],
        content_type=str(row[6]),
        media_kind=kind if isinstance(kind, str) else None,
    )


def words(text: str | None) -> list[str]:
    """The `[a-z0-9]+` words of an already normalized text, in order."""
    return _WORD.findall(text or "")


def term_matches(normalized_term: str, search_key: str | None) -> bool:
    """Every word of the term appears in the message as a whole word; a
    multi-word term must appear as consecutive words. Both sides went through
    `memory.search.normalize` (the term at creation, the key at ingestion)."""
    term = words(normalized_term)
    if not term:
        return False
    key = words(search_key)
    size = len(term)
    return any(key[start : start + size] == term for start in range(len(key) - size + 1))


#: Stored message kinds that carry a media file (`telegram/normalize.py`).
MEDIA_CONTENT = frozenset(
    {"photo", "video", "voice", "video_note", "audio", "document", "animation", "sticker"}
)
#: The stored kinds each kind a monitor names covers.
_MEDIA_ALIASES = {
    "photo": frozenset({"photo"}),
    "document": frozenset({"document"}),
    "voice": frozenset({"voice"}),
    "video": frozenset({"video", "video_note"}),
}


def media_matches(kinds: Collection[str], facts: MessageFacts) -> bool:
    """`any` is any media file; a location, contact or poll is not media."""
    kind = facts.media_kind or facts.content_type
    if kind not in MEDIA_CONTENT:
        return False
    if "any" in kinds:
        return True
    return any(kind in _MEDIA_ALIASES.get(wanted, frozenset()) for wanted in kinds)


def matches(rule: MonitorRule, facts: MessageFacts) -> bool:
    """A deterministic condition against one new contact message."""
    condition = rule.condition
    if condition.kind == "contact_replied":
        return condition.contact_id is not None and condition.contact_id == facts.contact_id
    if condition.kind == "keywords":
        return any(term_matches(term, facts.search_key) for term in condition.normalized_terms)
    if condition.kind == "media_received":
        return media_matches(condition.media_kinds, facts)
    return False


# ------------------------------------------------------------------ effects
def _audit(
    session: AsyncSession,
    account_id: int,
    now: datetime,
    event: str,
    automation_id: int,
    details: Mapping[str, Any],
    *,
    actor: str = "system",
) -> None:
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=now,
            actor=actor,
            event=event,
            subject_type="automation",
            subject_id=automation_id,
            details={"automation_id": automation_id, **details},
        )
    )


async def complete_one_shot(
    session: AsyncSession, *, account_id: int, automation_id: int, now: datetime
) -> bool:
    """`active` → `completed`, conditionally: True for the one caller that
    changed the row, so a one-shot monitor fires exactly once."""
    changed = await session.scalar(
        update(Automation)
        .where(Automation.id == automation_id)
        .where(Automation.account_id == account_id)
        .where(Automation.kind == KIND)
        .where(Automation.status == "active")
        .values(status="completed", ended_at=now, last_run_at=now, updated_at=now)
        .returning(Automation.id)
    )
    return changed is not None


async def _touch_standing(
    session: AsyncSession, *, account_id: int, automation_id: int, now: datetime
) -> bool:
    """A standing monitor stays active; it records when it last fired. False
    when it ended meanwhile (stopped, expired)."""
    changed = await session.scalar(
        update(Automation)
        .where(Automation.id == automation_id)
        .where(Automation.account_id == account_id)
        .where(Automation.kind == KIND)
        .where(Automation.status == "active")
        .values(last_run_at=now, updated_at=now)
        .returning(Automation.id)
    )
    return changed is not None


async def record_match(
    session: AsyncSession,
    view: MonitorView,
    *,
    conversation_id: int,
    contact_id: int,
    message_ids: Sequence[int],
    clock: Clock,
    scheduler: JobScheduler | None,
    via: str,
) -> int | None:
    """One match: the one-shot completion, then one `MONITOR_MATCH` alert.
    Returns the notification id, or None when nothing was queued (the
    monitor already fired or ended, or this message already alerted)."""
    now = clock.now()
    account_id, automation_id = view.account_id, view.automation_id
    one_shot = view.rule.lifecycle == "one_shot"
    if one_shot:
        if not await complete_one_shot(
            session, account_id=account_id, automation_id=automation_id, now=now
        ):
            return None
    elif not await _touch_standing(
        session, account_id=account_id, automation_id=automation_id, now=now
    ):
        return None
    newest = max(message_ids)
    notification_id = await enqueue(
        session,
        account_id=account_id,
        kind=NotificationKind.MONITOR_MATCH,
        priority=Priority.HIGH,
        dedupe_key=f"monitor:{automation_id}:{newest}",
        refs=NotificationRefs(
            conversation_ids=(conversation_id,),
            contact_ids=(contact_id,),
            message_ids=tuple(sorted(set(message_ids))),
            automation_ids=(automation_id,),
        ),
        clock=clock,
        conversation_id=conversation_id,
        contact_id=contact_id,
        bypass_quiet_hours=view.rule.bypass_quiet_hours,
        scheduler=scheduler,
    )
    if notification_id is None:
        return None
    details = {
        "conversation_id": conversation_id,
        "message_id": newest,
        "notification_id": notification_id,
        "condition": view.rule.condition.kind,
        "via": via,
    }
    _audit(session, account_id, now, EVENT_MATCHED, automation_id, details)
    if one_shot:
        _audit(session, account_id, now, EVENT_COMPLETED, automation_id, {"reason": "fired"})
    logger.info(
        "monitor_matched",
        account_id=account_id,
        automation_id=automation_id,
        conversation_id=conversation_id,
        notification_id=notification_id,
        via=via,
    )
    return notification_id


# ------------------------------------------------------------------ lifecycle
async def create(
    session: AsyncSession,
    *,
    account_id: int,
    rule: MonitorRule,
    expires_at: datetime | None,
    timezone: str,
    clock: Clock,
    run_id: int | None,
    tool_call_id: str | None,
) -> tuple[Automation, bool]:
    """Create a confirmed monitor; idempotent on `(run_id, tool_call_id)`.
    Returns the row and whether this call created it. Foreign ids raise
    `NotInAccountError` (the repository checks every `refs` id)."""
    if run_id is not None and tool_call_id is not None:
        existing = await session.scalar(
            select(Automation)
            .where(Automation.account_id == account_id)
            .where(Automation.run_id == run_id)
            .where(Automation.tool_call_id == tool_call_id)
        )
        if existing is not None:
            return existing, False
    scope = rule.scope
    contact_ids = list(scope.contact_ids)
    if rule.condition.contact_id is not None and rule.condition.contact_id not in contact_ids:
        contact_ids.append(rule.condition.contact_id)
    now = clock.now()
    row = await repository.create(
        session,
        account_id=account_id,
        kind=KIND,
        spec=rule,
        refs={"conversation_ids": list(scope.conversation_ids), "contact_ids": contact_ids},
        timezone=timezone,
        tz_policy="fixed",
        clock=clock,
        next_run_at=None,
        expires_at=expires_at,
        run_id=run_id,
        tool_call_id=tool_call_id,
        confirmed_at=now,
    )
    _audit(
        session,
        account_id,
        now,
        EVENT_CREATED,
        row.id,
        {
            "condition": rule.condition.kind,
            "lifecycle": rule.lifecycle,
            "action": rule.action,
            "all_observed": scope.all_observed,
            "conversations": len(scope.conversation_ids),
            "contacts": len(contact_ids),
        },
        actor="agent",
    )
    return row, True


class StopResult(StrEnum):
    STOPPED = "stopped"
    ALREADY_ENDED = "already_ended"
    NOT_FOUND = "not_found"


async def stop(
    session: AsyncSession, *, account_id: int, automation_id: int, clock: Clock, actor: str
) -> StopResult:
    """`active`/`paused` → `cancelled`, once; account-scoped (another
    account's id is NOT_FOUND, AGENTS §6.12)."""
    row = await repository.get(session, account_id=account_id, automation_id=automation_id)
    if row is None or row.kind != KIND:
        return StopResult.NOT_FOUND
    now = clock.now()
    changed = await session.scalar(
        update(Automation)
        .where(Automation.id == automation_id)
        .where(Automation.account_id == account_id)
        .where(Automation.status.in_(("active", "paused")))
        .values(status="cancelled", ended_at=now, updated_at=now)
        .returning(Automation.id)
    )
    if changed is None:
        return StopResult.ALREADY_ENDED
    _audit(session, account_id, now, EVENT_STOPPED, automation_id, {}, actor=actor)
    return StopResult.STOPPED


async def listed(session: AsyncSession, *, account_id: int, now: datetime) -> list[MonitorView]:
    """The owner's monitors that are on or paused (expired ones are over even
    before the hourly sweep marks them), oldest first."""
    rows = (
        await session.execute(
            select(Automation)
            .where(Automation.account_id == account_id)
            .where(Automation.kind == KIND)
            .where(Automation.status.in_(("active", "paused")))
            .where(or_(Automation.expires_at.is_(None), Automation.expires_at > now))
            .order_by(Automation.id)
        )
    ).scalars()
    found = []
    for row in rows.all():
        rule = parse_rule(row.spec)
        if rule is not None:
            found.append(_view(row, rule))
    return found


async def expire_due(session: AsyncSession, *, now: datetime) -> int:
    """`system.expire_monitors`: active monitors past `expires_at` →
    `expired` (every account; each row keeps its own account id)."""
    rows = (
        await session.execute(
            update(Automation)
            .where(Automation.kind == KIND)
            .where(Automation.status == "active")
            .where(Automation.expires_at.is_not(None))
            .where(Automation.expires_at <= now)
            .values(status="expired", ended_at=now, updated_at=now)
            .returning(Automation.id, Automation.account_id)
        )
    ).all()
    for automation_id, account_id in rows:
        _audit(session, int(account_id), now, EVENT_EXPIRED, int(automation_id), {})
    return len(rows)


# ------------------------------------------------------------------ owner-facing words
def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


def local_moment(moment: datetime, timezone: str) -> str:
    """`25 Nov 18:00` in the owner's timezone."""
    return moment.astimezone(_zone(timezone)).strftime("%d %b %H:%M")


async def contact_names(
    session: AsyncSession, account_id: int, contact_ids: Collection[int]
) -> dict[int, str]:
    """Contact-chosen names (plain text), by id; `NotInAccountError` when an
    id is not this account's contact."""
    if not contact_ids:
        return {}
    rows = (
        await session.execute(
            select(Contact.id, Contact.display_name, Contact.username)
            .where(Contact.account_id == account_id)
            .where(Contact.id.in_(sorted(contact_ids)))
        )
    ).all()
    if len(rows) != len(set(contact_ids)):
        raise NotInAccountError("contact not found in this account")
    return {int(i): preferred_name(name, user) or UNNAMED for i, name, user in rows}


async def chat_contacts(
    session: AsyncSession, account_id: int, conversation_ids: Collection[int]
) -> dict[int, int]:
    """Each chat's contact id; `NotInAccountError` for a chat not of this account."""
    if not conversation_ids:
        return {}
    rows = (
        await session.execute(
            select(Conversation.id, Conversation.contact_id)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id.in_(sorted(conversation_ids)))
        )
    ).all()
    if len(rows) != len(set(conversation_ids)):
        raise NotInAccountError("conversation not found in this account")
    return {int(i): int(c) for i, c in rows}


_MEDIA_WORDS = {
    "photo": "a photo",
    "document": "a document",
    "voice": "a voice message",
    "video": "a video",
}


def _join(items: Sequence[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " or " + items[-1]


async def describe(
    session: AsyncSession,
    *,
    account_id: int,
    rule: MonitorRule,
    expires_at: datetime | None,
    timezone: str,
    with_expiry: bool = True,
    expiry_note: str | None = None,
) -> str:
    """The monitor in plain words, as the owner sees it on the confirmation
    card, the "Monitor on" notice and its alerts. Plain text: each caller
    escapes it (the card also makes hidden characters visible). Names are
    contact-chosen; ids not of this account raise `NotInAccountError`."""
    scope, condition = rule.scope, rule.condition
    chats = await chat_contacts(session, account_id, scope.conversation_ids)
    named = list(dict.fromkeys([*scope.contact_ids, *chats.values()]))
    if condition.contact_id is not None:
        named = list(dict.fromkeys([condition.contact_id, *named]))
    names = await contact_names(session, account_id, named)
    if scope.all_observed:
        where = "any chat I observe"
    else:
        listed_names = [names[i] for i in dict.fromkeys([*scope.contact_ids, *chats.values()])]
        where = "the chat with " + _join(listed_names) if listed_names else "the named chats"
    every = "once when" if rule.lifecycle == "one_shot" else "every time"
    if condition.kind == "contact_replied":
        who = names.get(condition.contact_id or 0, UNNAMED)
        what = f"Alert you {every} {who} writes"
    elif condition.kind == "keywords":
        terms = ", ".join(f"“{term}”" for term in condition.terms)
        what = f"Alert you {every} a message in {where} mentions {terms}"
    elif condition.kind == "media_received":
        kinds = (
            ["any media"]
            if "any" in condition.media_kinds
            else [_MEDIA_WORDS.get(k, k) for k in condition.media_kinds]
        )
        what = f"Alert you {every} {_join(kinds)} arrives in {where}"
    else:
        topic = f" about: {condition.description}" if condition.description else ""
        looked_for = {
            "semantic": f"a new message in {where} is about: {condition.description}",
            "decision_requested": f"someone in {where} asks you to decide{topic}",
            "urgent": f"a new message in {where} is urgent{topic}",
        }[condition.kind]
        what = f"Ask the AI to check new messages and alert you {every} {looked_for}"
    extras = []
    if rule.action == "alert_with_summary":
        extras.append("with the chat's latest triage reasons")
    if rule.bypass_quiet_hours:
        extras.append("also during your quiet hours")
    if with_expiry and expires_at is not None:
        extras.append(f"until {local_moment(expires_at, timezone)}")
    elif with_expiry and expiry_note is not None:
        extras.append(expiry_note)
    return what + (f" ({'; '.join(extras)})" if extras else "")


# ------------------------------------------------------------------ the alert
def _ids(refs: Mapping[str, Any], key: str) -> list[int]:
    value = refs.get(key)
    if not isinstance(value, list):
        return []
    return [i for i in value if isinstance(i, int) and not isinstance(i, bool)]


STOP_HINT = (
    "This monitor keeps watching. To stop it, tap Stop on its “Monitor on” message or "
    "ask me to stop monitor {id}."
)


async def render_monitor_match(
    session: AsyncSession, notification: Notification, account: Account
) -> RenderedAlert | None:
    """`MONITOR_MATCH` (S05.4 §3.7): "Monitor: <interpretation> — <contact>
    wrote at <local time>", never message text. `alert_with_summary` adds
    the chat's latest triage reasons when that verdict covers the matched
    message (no model call). None — the alert is suppressed — when the
    monitor or the chat is gone (their data was deleted)."""
    automation_ids = _ids(notification.refs, "automation_ids")
    conversation_id = notification.conversation_id
    if not automation_ids or conversation_id is None:
        return None
    row = await repository.get(session, account_id=account.id, automation_id=automation_ids[0])
    rule = None if row is None else parse_rule(row.spec)
    chat = await chat_facts(session, account_id=account.id, conversation_id=conversation_id)
    if row is None or rule is None or chat is None:
        return None
    try:
        interpretation = await describe(
            session,
            account_id=account.id,
            rule=rule,
            expires_at=row.expires_at,
            timezone=account.timezone,
            with_expiry=False,
        )
        name = (await contact_names(session, account.id, [chat.contact_id]))[chat.contact_id]
    except NotInAccountError:
        return None
    message_ids = _ids(notification.refs, "message_ids")
    sent_at = None
    if message_ids:
        sent_at = await session.scalar(
            select(Message.sent_at)
            .where(Message.account_id == account.id)
            .where(Message.conversation_id == conversation_id)
            .where(Message.id == max(message_ids))
        )
    when = f" at {local_moment(sent_at, account.timezone)}" if sent_at is not None else ""
    lines = [
        f"🔔 <b>Monitor:</b> {html.escape(interpretation)} — "
        f"<b>{html.escape(name)}</b> wrote{when}."
    ]
    if rule.action == "alert_with_summary" and message_ids:
        verdict = (
            await session.execute(
                select(ConversationTriage.covers_through_message_id, ConversationTriage.reasons)
                .where(ConversationTriage.account_id == account.id)
                .where(ConversationTriage.conversation_id == conversation_id)
            )
        ).first()
        if verdict is not None and verdict[0] >= max(message_ids):
            reasons = [r for r in verdict[1] or () if isinstance(r, str) and r.strip()]
            lines += [f"• {html.escape(reason)}" for reason in reasons]
    if rule.lifecycle == "standing" and row.status == "active":
        lines += ["", STOP_HINT.format(id=row.id)]
    return RenderedAlert("\n".join(lines), conversation_id)
