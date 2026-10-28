"""The daily digest: composed deterministically, delivered at most once
(S05.3b §3.4-§3.6; P2-T13; ARCHITECTURE §12.2, §17).

**Content** (`build_digest`) is read from what P2 already stores — no model
call: the batched alerts that need the owner, the chats waiting for a reply
(attention list v1 selection, narrowed by a triage verdict that covers the
newest contact message), commitments due, tasks triage closed, counts of the
other batched alerts, deletions in the period (never who deleted — TF §12
LV-07; owner decision E2), and the period's coverage caveats. Contact names,
verdict reasons and task titles are the only content in it — what alerts and
the attention list already show; never message text.

**Delivery** (`DigestDelivery.build`) for one account and `period_end`:
`period_start` is the last *sent* digest's `period_end` (else 24 h before,
never more than 48 h). In one short transaction, under the account's row
lock (§9.4: the account before its notifications, as alert delivery): the
`digests` row is claimed (`UNIQUE (account_id, period_start, period_end)`,
and any row already ending at `period_end` makes this a no-op), the content
built, the row set `uncertain` and the batched alerts it carries linked to
it (`mark_digested`) — the at-most-once claim, committed before the send
(same pattern as notifications, P2-T25). After the send: `sent`; a definite
failure → `failed`, its alerts unlinked so the next digest carries them; an
ambiguous outcome stays `uncertain` and is never resent (§6 invariant 6
spirit). `building` is never committed.

The digest goes only to the owner's control chat (`ControlChannel`). Its
"Open: <contact>" buttons are `pending_inputs` cards of kind `dg` (`ref_id`
= the conversation; the `ntf` kind's `ref_id` is a notification, and a
waiting chat may have none), at most five, on the last chunk.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.callbacks import encode
from assistant.accounts.models import Account, AuditLogEntry, PendingInput
from assistant.accounts.service import create_card
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.channel import Button, ControlChannel, Delivery
from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.inbox.models import ConversationTriage, Digest, Notification, Task
from assistant.inbox.notification_types import AlertClass, NotificationKind
from assistant.inbox.notifications import (
    NotificationView,
    mark_digested,
    notifications_batched_since,
)
from assistant.memory import catch_up
from assistant.memory.summaries import preferred_name
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

#: The `pending_inputs` kind of an "Open: <contact>" button (`ref_id` = the
#: conversation), and its one op.
DIGEST_CARD = "dg"
OP_OPEN_CHAT = "op"
MAX_OPEN_BUTTONS = 5
#: Long enough to open a chat from yesterday's digest.
DIGEST_CARD_TTL = timedelta(days=7)
#: Characters of a contact's name on a button.
BUTTON_NAME_CHARS = 24

#: A first digest covers the day before it; no digest reaches further back.
FIRST_PERIOD = timedelta(hours=24)
MAX_PERIOD = timedelta(hours=48)
#: Commitments due before this long after the period ends are listed
#: (J7 step 2: due today and still open → in the next digest).
COMMITMENT_HORIZON = timedelta(hours=24)
#: The attention list's default window (`get_attention_list`) and size.
WAITING_WINDOW = timedelta(days=7)
WAITING_CANDIDATES = 100
MAX_WAITING = 10
#: Task lines per section; the rest are counted.
MAX_TASK_LINES = 15

#: Alerts that need the owner (S05.3b §3.4 item 1).
NEEDS_YOU_KINDS = frozenset({NotificationKind.TRIAGE_URGENT, NotificationKind.TRIAGE_DECISION})
COMMITMENT_KINDS = ("owner_commitment", "contact_commitment")
RESOLVED_BY_TRIAGE = "resolved_by_triage"
#: Audit events of deletions the owner asked for (`privacy/deletion.py`).
OWNER_DELETION_EVENTS = catch_up.DELETION_EVENTS
EVENT_DIGEST_SENT = "digest_sent"


# ------------------------------------------------------------ content
@dataclass(frozen=True, slots=True)
class ChatLine:
    """A chat the owner should look at. `name` (contact-chosen) and `reason`
    (model-written) are content; the renderer escapes them."""

    conversation_id: int
    name: str | None
    reason: str | None = None
    #: When the contact wrote last (waiting chats).
    since: datetime | None = None


@dataclass(frozen=True, slots=True)
class TaskLine:
    task_id: int
    kind: str
    #: Model-written: content.
    title: str
    name: str | None
    due_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ChatDeletions:
    """Messages deleted in one chat in the period, by whose message it was
    (Telegram does not say who deleted it)."""

    conversation_id: int
    name: str | None
    contact: int = 0
    owner: int = 0
    assistant: int = 0
    #: Deletions of messages never stored (no direction known).
    unknown: int = 0

    @property
    def total(self) -> int:
        return self.contact + self.owner + self.assistant + self.unknown


@dataclass(frozen=True, slots=True)
class OwnerDeletion:
    """A deletion the owner asked for: when, what scope, how many messages."""

    at: datetime
    scope: str
    messages: int | None


@dataclass(frozen=True, slots=True)
class DigestContent:
    account_id: int
    period_start: datetime
    period_end: datetime
    timezone: str
    needs_you: tuple[ChatLine, ...] = ()
    waiting: tuple[ChatLine, ...] = ()
    commitments: tuple[TaskLine, ...] = ()
    commitments_more: int = 0
    closed: tuple[TaskLine, ...] = ()
    closed_more: int = 0
    #: `(kind, count)` of the other batched alerts.
    other_alerts: tuple[tuple[str, int], ...] = ()
    deletions: tuple[ChatDeletions, ...] = ()
    owner_deletions: tuple[OwnerDeletion, ...] = ()
    #: Coverage caveats for the period (`memory.catch_up.caveats`).
    caveats: tuple[str, ...] = ()
    #: The batched alerts this digest carries (marked digested at the claim).
    covered_notification_ids: frozenset[int] = field(default_factory=frozenset)

    @property
    def quiet(self) -> bool:
        """Nothing in items 1-5 (S05.3b §3.4 item 7)."""
        return not (
            self.needs_you or self.waiting or self.commitments or self.closed or self.other_alerts
        )

    def open_chat_targets(self) -> tuple[ChatLine, ...]:
        """The chats of items 1-2 that get an "Open" button: the first
        `MAX_OPEN_BUTTONS` listed, each once."""
        seen: set[int] = set()
        targets: list[ChatLine] = []
        for line in (*self.needs_you, *self.waiting):
            if line.conversation_id in seen:
                continue
            seen.add(line.conversation_id)
            targets.append(line)
            if len(targets) == MAX_OPEN_BUTTONS:
                break
        return tuple(targets)

    def counts(self) -> dict[str, int | bool]:
        """Counts only: the audit row and the log line."""
        return {
            "needs_you": len(self.needs_you),
            "waiting": len(self.waiting),
            "commitments": len(self.commitments) + self.commitments_more,
            "closed": len(self.closed) + self.closed_more,
            "other_alerts": sum(count for _, count in self.other_alerts),
            "deletions": sum(d.total for d in self.deletions) + len(self.owner_deletions),
            "caveats": len(self.caveats),
            "quiet": self.quiet,
        }


async def build_digest(
    session: AsyncSession, *, account_id: int, period_start: datetime, period_end: datetime
) -> DigestContent:
    """The digest of one account for `[period_start, period_end)`. Read-only;
    every statement is scoped to the account (§6 invariant 12, RG-21)."""
    timezone = await session.scalar(select(Account.timezone).where(Account.id == account_id))
    if timezone is None:
        raise LookupError("account")
    # An alert no digest carried is still news up to the longest period, even
    # when the digest before failed (it never linked its alerts).
    batched = await notifications_batched_since(
        session, account_id=account_id, since=period_end - MAX_PERIOD
    )
    held = await _held_needs_you(session, account_id, period_start, period_end)
    needs_you_alerts = [
        n for n in (*batched, *held) if n.kind in NEEDS_YOU_KINDS and n.conversation_id is not None
    ]
    others = Counter(n.kind for n in batched if n.kind not in NEEDS_YOU_KINDS)

    candidates = await catch_up.unanswered(
        session, account_id, since=period_end - WAITING_WINDOW, limit=WAITING_CANDIDATES
    )
    alert_chats = list(
        dict.fromkeys(n.conversation_id for n in needs_you_alerts if n.conversation_id)
    )
    verdicts = await _verdicts(
        session, account_id, [*alert_chats, *(c.conversation_id for c in candidates)]
    )
    covering = await _covering(session, account_id, [c.conversation_id for c in candidates])
    names = await _names(session, account_id, alert_chats)

    needs_you = tuple(
        ChatLine(
            conversation_id, names[conversation_id], _first_reason(verdicts.get(conversation_id))
        )
        for conversation_id in alert_chats
        if conversation_id in names  # a chat whose data is gone is not listed
    )
    listed = {line.conversation_id for line in needs_you}
    waiting: list[tuple[int, ChatLine]] = []
    for candidate in candidates:
        if candidate.conversation_id in listed:
            continue
        verdict = verdicts.get(candidate.conversation_id)
        rank = 1
        if candidate.conversation_id in covering and verdict is not None:
            if not (verdict.needs_reply or verdict.requests_decision):
                continue  # triage read the newest message: nothing is owed
            rank = 0
        name = preferred_name(candidate.display_name, candidate.username)
        reason = _first_reason(verdict) if rank == 0 else None
        waiting.append(
            (rank, ChatLine(candidate.conversation_id, name, reason, candidate.last_contact_msg_at))
        )
    # Stable: within a rank, the chats waiting longest stay first.
    waiting.sort(key=lambda item: item[0])

    commitments, commitments_more = await _commitments(session, account_id, period_end)
    closed, closed_more = await _closed(session, account_id, period_start, period_end)
    deletions = await _chat_deletions(session, account_id, period_start, period_end)
    owner_deletions = await _owner_deletions(session, account_id, period_start, period_end)
    coverage = await catch_up.period_coverage(
        session, account_id, start=period_start, end=period_end
    )
    return DigestContent(
        account_id=account_id,
        period_start=period_start,
        period_end=period_end,
        timezone=timezone,
        needs_you=needs_you,
        waiting=tuple(line for _, line in waiting[:MAX_WAITING]),
        commitments=commitments,
        commitments_more=commitments_more,
        closed=closed,
        closed_more=closed_more,
        other_alerts=tuple(sorted(others.items())),
        deletions=deletions,
        owner_deletions=owner_deletions,
        caveats=tuple(catch_up.caveats(coverage, timezone)),
        covered_notification_ids=frozenset(n.notification_id for n in batched),
    )


def _first_reason(verdict: ConversationTriage | None) -> str | None:
    if verdict is None:
        return None
    for reason in verdict.reasons or ():
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
    return None


async def _held_needs_you(
    session: AsyncSession, account_id: int, period_start: datetime, period_end: datetime
) -> list[NotificationView]:
    """Urgent/decision alerts of the period still held (quiet hours): they
    are delivered when the hold ends, and listed here meanwhile."""
    rows = (
        await session.execute(
            select(Notification)
            .where(Notification.account_id == account_id)
            .where(Notification.alert_class == AlertClass.STANDARD)
            .where(Notification.status == "queued")
            .where(Notification.deliver_after > period_end)
            .where(Notification.kind.in_(sorted(NEEDS_YOU_KINDS)))
            .where(Notification.created_at >= period_start)
            .where(Notification.created_at < period_end)
            .order_by(Notification.created_at, Notification.id)
        )
    ).scalars()
    return [
        NotificationView(
            notification_id=row.id,
            kind=row.kind,
            priority=row.priority,
            conversation_id=row.conversation_id,
            contact_id=row.contact_id,
            refs=dict(row.refs),
            created_at=row.created_at,
        )
        for row in rows
    ]


async def _verdicts(
    session: AsyncSession, account_id: int, conversation_ids: Sequence[int]
) -> dict[int, ConversationTriage]:
    """Each chat's stored triage verdict. (S05.1's `triage_store.latest`
    reads the same rows; switch to it once S05.1's pipeline is merged.)"""
    if not conversation_ids:
        return {}
    rows = await session.execute(
        select(ConversationTriage)
        .where(ConversationTriage.account_id == account_id)
        .where(ConversationTriage.conversation_id.in_(sorted(set(conversation_ids))))
    )
    return {row.conversation_id: row for row in rows.scalars()}


async def _covering(
    session: AsyncSession, account_id: int, conversation_ids: Sequence[int]
) -> set[int]:
    """The chats whose verdict has read their newest contact message
    (attention list v2, S05.1 §3.9): only then does the verdict decide."""
    if not conversation_ids:
        return set()
    newest = (
        select(Message.conversation_id, func.max(Message.id).label("newest"))
        .where(Message.account_id == account_id)
        .where(Message.conversation_id.in_(sorted(set(conversation_ids))))
        .where(Message.direction == "contact")
        .group_by(Message.conversation_id)
        .subquery()
    )
    rows = await session.execute(
        select(ConversationTriage.conversation_id)
        .join(newest, newest.c.conversation_id == ConversationTriage.conversation_id)
        .where(ConversationTriage.account_id == account_id)
        .where(ConversationTriage.covers_through_message_id >= newest.c.newest)
    )
    return {int(conversation_id) for conversation_id in rows.scalars()}


async def _names(
    session: AsyncSession, account_id: int, conversation_ids: Collection[int]
) -> dict[int, str | None]:
    """Each existing chat's contact name (None when it has none); a chat
    that is not this account's, or no longer exists, is absent."""
    if not conversation_ids:
        return {}
    rows = await session.execute(
        select(Conversation.id, Contact.display_name, Contact.username)
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.account_id == account_id)
        .where(Contact.account_id == account_id)
        .where(Conversation.id.in_(sorted(set(conversation_ids))))
    )
    return {int(c): preferred_name(d, u) for c, d, u in rows.all()}


async def _task_lines(
    session: AsyncSession, account_id: int, tasks: Sequence[Task]
) -> tuple[TaskLine, ...]:
    names = await _names(
        session, account_id, [t.conversation_id for t in tasks if t.conversation_id is not None]
    )
    return tuple(
        TaskLine(
            task_id=task.id,
            kind=task.kind,
            title=task.title,
            name=None if task.conversation_id is None else names.get(task.conversation_id),
            due_at=task.due_at,
        )
        for task in tasks
    )


async def _commitments(
    session: AsyncSession, account_id: int, period_end: datetime
) -> tuple[tuple[TaskLine, ...], int]:
    """Open commitments (either side) due before the horizon, soonest first,
    overdue ones included; a snoozed one is left out until its snooze ends.
    (S05.1's `tasks.due_between` covers a bounded window; this one has no
    lower bound.)"""
    due = and_(
        Task.account_id == account_id,
        Task.status == "open",
        Task.kind.in_(COMMITMENT_KINDS),
        Task.due_at.is_not(None),
        Task.due_at < period_end + COMMITMENT_HORIZON,
        or_(Task.snoozed_until.is_(None), Task.snoozed_until <= period_end),
    )
    total = int(await session.scalar(select(func.count(Task.id)).where(due)) or 0)
    rows = (
        await session.execute(
            select(Task).where(due).order_by(Task.due_at, Task.id).limit(MAX_TASK_LINES)
        )
    ).scalars()
    lines = await _task_lines(session, account_id, list(rows))
    return lines, max(0, total - len(lines))


async def _closed(
    session: AsyncSession, account_id: int, period_start: datetime, period_end: datetime
) -> tuple[tuple[TaskLine, ...], int]:
    """Tasks triage closed in the period (FR-TRI-05: reported in the digest)."""
    closed = and_(
        Task.account_id == account_id,
        Task.closed_reason == RESOLVED_BY_TRIAGE,
        Task.closed_at >= period_start,
        Task.closed_at < period_end,
    )
    total = int(await session.scalar(select(func.count(Task.id)).where(closed)) or 0)
    rows = (
        await session.execute(
            select(Task).where(closed).order_by(Task.closed_at, Task.id).limit(MAX_TASK_LINES)
        )
    ).scalars()
    lines = await _task_lines(session, account_id, list(rows))
    return lines, max(0, total - len(lines))


_BY_DIRECTION = {
    "contact": "contact",
    "owner_manual": "owner",
    "owner_automated": "owner",
    "assistant_sent": "assistant",
}


async def _chat_deletions(
    session: AsyncSession, account_id: int, period_start: datetime, period_end: datetime
) -> tuple[ChatDeletions, ...]:
    """Messages deleted in the period, per chat (not bot chats)."""
    rows = (
        await session.execute(
            select(Message.conversation_id, Message.direction, func.count(Message.id))
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(Message.account_id == account_id)
            .where(Conversation.account_id == account_id)
            .where(Contact.is_bot.is_(False))
            .where(Message.deleted_at >= period_start)
            .where(Message.deleted_at < period_end)
            .group_by(Message.conversation_id, Message.direction)
        )
    ).all()
    per_chat: dict[int, Counter[str]] = {}
    for conversation_id, direction, count in rows:
        side = _BY_DIRECTION.get(direction or "", "unknown")
        per_chat.setdefault(int(conversation_id), Counter())[side] += int(count)
    names = await _names(session, account_id, per_chat)
    return tuple(
        ChatDeletions(
            conversation_id=conversation_id,
            name=names.get(conversation_id),
            contact=sides["contact"],
            owner=sides["owner"],
            assistant=sides["assistant"],
            unknown=sides["unknown"],
        )
        for conversation_id, sides in sorted(per_chat.items())
    )


async def _owner_deletions(
    session: AsyncSession, account_id: int, period_start: datetime, period_end: datetime
) -> tuple[OwnerDeletion, ...]:
    """The owner's own deletions in the period, from their content-free
    audit rows: scope, time and the message count."""
    rows = (
        await session.execute(
            select(AuditLogEntry.at, AuditLogEntry.event, AuditLogEntry.details)
            .where(AuditLogEntry.account_id == account_id)
            .where(AuditLogEntry.event.in_(OWNER_DELETION_EVENTS))
            .where(AuditLogEntry.at >= period_start)
            .where(AuditLogEntry.at < period_end)
            .order_by(AuditLogEntry.at, AuditLogEntry.id)
        )
    ).all()
    found: list[OwnerDeletion] = []
    for at, event, details in rows:
        values = details if isinstance(details, dict) else {}
        scope = values.get("scope") if event == "data_deleted" else "managed"
        messages = values.get("messages")
        found.append(
            OwnerDeletion(
                at=at,
                scope=scope if isinstance(scope, str) else "unknown",
                messages=messages if type(messages) is int else None,
            )
        )
    return tuple(found)


# ------------------------------------------------------------ delivery
DigestRenderer = Callable[[DigestContent], str]


@dataclass(frozen=True, slots=True)
class DigestResult:
    """What one `build` did (codes only)."""

    status: str
    digest_id: int | None = None
    reason: str | None = None


def button_label(name: str | None) -> str:
    shown = (name or "chat").strip() or "chat"
    if len(shown) > BUTTON_NAME_CHARS:
        shown = shown[: BUTTON_NAME_CHARS - 1].rstrip() + "…"
    return f"Open: {shown}"


class DigestDelivery:
    """`digest.build` (S05.3b §3.5) and `/digest` (§3.6)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        channel: ControlChannel,
        clock: Clock,
        render: DigestRenderer,
    ) -> None:
        self._session_factory = session_factory
        self._channel = channel
        self._clock = clock
        self._render = render

    async def build(self, account_id: int, period_end: datetime) -> DigestResult:
        """Build and send the digest ending at `period_end`, at most once."""
        now = self._clock.now()
        async with transaction(self._session_factory) as uow:
            session = uow.session
            # The account first (§9.4), FOR NO KEY UPDATE like a standard
            # alert's delivery: no alert is batched or claimed mid-build.
            account = (
                await session.execute(
                    select(Account).where(Account.id == account_id).with_for_update(key_share=True)
                )
            ).scalar_one_or_none()
            if account is None or account.status != "active" or account.control_chat_id is None:
                return DigestResult("skipped", reason="account_inactive")
            existing = await session.scalar(
                select(Digest)
                .where(Digest.account_id == account_id)
                .where(Digest.period_end == period_end)
                .order_by(Digest.id.desc())
                .limit(1)
            )
            if existing is not None:
                return DigestResult("duplicate", existing.id, existing.status)
            last_sent = await session.scalar(
                select(func.max(Digest.period_end))
                .where(Digest.account_id == account_id)
                .where(Digest.status == "sent")
            )
            if last_sent is not None and last_sent >= period_end:
                return DigestResult("covered", reason="newer_digest_sent")
            period_start = max(
                last_sent if last_sent is not None else period_end - FIRST_PERIOD,
                period_end - MAX_PERIOD,
            )
            digest_id = await session.scalar(
                insert(Digest)
                .values(
                    account_id=account_id,
                    period_start=period_start,
                    period_end=period_end,
                    status="building",
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_digests_account_id_period_start_period_end")
                .returning(Digest.id)
            )
            if digest_id is None:
                return DigestResult("duplicate")
            content = await build_digest(
                session, account_id=account_id, period_start=period_start, period_end=period_end
            )
            text = self._render(content)
            # The at-most-once claim (§3.5 step 3): committed before the send.
            await session.execute(
                update(Digest)
                .where(Digest.id == digest_id)
                .where(Digest.status == "building")
                .values(status="uncertain", updated_at=now)
            )
            await mark_digested(
                session,
                account_id=account_id,
                ids=content.covered_notification_ids,
                digest_id=digest_id,
            )
            card_ids, buttons = await self._buttons(session, account_id, content)
            chat_id = account.control_chat_id

        delivery = await self._channel.send(
            account_id=account_id, chat_id=chat_id, text=text, buttons=buttons
        )
        result = await self._record(digest_id, account_id, content, delivery, card_ids)
        logger.info(
            "digest_delivered",
            account_id=account_id,
            digest_id=digest_id,
            status=result.status,
            **content.counts(),
        )
        return result

    async def _buttons(
        self, session: AsyncSession, account_id: int, content: DigestContent
    ) -> tuple[list[int], tuple[Button, ...]]:
        card_ids: list[int] = []
        buttons: list[Button] = []
        for line in content.open_chat_targets():
            card = await create_card(
                session,
                account_id=account_id,
                kind=DIGEST_CARD,
                clock=self._clock,
                ref_id=line.conversation_id,
                ttl=DIGEST_CARD_TTL,
            )
            card_ids.append(card.id)
            buttons.append(
                Button(button_label(line.name), encode(DIGEST_CARD, card.token or "", OP_OPEN_CHAT))
            )
        return card_ids, tuple(buttons)

    async def _record(
        self,
        digest_id: int,
        account_id: int,
        content: DigestContent,
        delivery: Delivery,
        card_ids: Sequence[int],
    ) -> DigestResult:
        now = self._clock.now()
        async with transaction(self._session_factory) as uow:
            session = uow.session
            digest = await session.scalar(
                select(Digest).where(Digest.account_id == account_id).where(Digest.id == digest_id)
            )
            cards = (
                (
                    await session.execute(
                        select(PendingInput)
                        .where(PendingInput.account_id == account_id)
                        .where(PendingInput.id.in_(list(card_ids)))
                    )
                )
                .scalars()
                .all()
                if card_ids
                else []
            )
            if delivery.ok:
                for card in cards:
                    card.prompt_message_id = delivery.message_id
            elif not delivery.uncertain:
                for card in cards:  # definitely not on screen
                    await session.delete(card)
            if digest is None:  # the account's data was deleted meanwhile
                return DigestResult(
                    "sent" if delivery.ok else "uncertain", digest_id, "row_deleted"
                )
            digest.updated_at = now
            if delivery.ok:
                digest.status = "sent"
                digest.sent_at = now
                digest.message_id = delivery.message_id
                session.add(
                    AuditLogEntry(
                        account_id=account_id,
                        at=now,
                        actor="system",
                        event=EVENT_DIGEST_SENT,
                        subject_type="digest",
                        subject_id=digest_id,
                        details=content.counts(),
                    )
                )
                return DigestResult("sent", digest_id)
            if delivery.uncertain:
                return DigestResult("uncertain", digest_id, "ambiguous")
            digest.status = "failed"
            # The next digest carries these alerts instead.
            await session.execute(
                update(Notification)
                .where(Notification.account_id == account_id)
                .where(Notification.digest_id == digest_id)
                .values(digest_id=None)
            )
            return DigestResult("failed", digest_id, "not_delivered")
