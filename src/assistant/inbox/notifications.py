"""Notifications: one path for what the assistant tells the owner unprompted
(ARCHITECTURE §17; S05.2 §3.2, P2-T12, P2-T25).

- **`enqueue`** inserts a content-free row (kind, priority, class, ids,
  reason codes), deduplicated on `(account_id, dedupe_key)`, and — given a
  scheduler — defers `notify.deliver` after the transaction commits. A lost
  defer is recovered by the reconciler (`jobs/reconcile_inbox.py`).
- **`NotificationDelivery.deliver`** decides with the pure policy
  (`notification_policy.decide_delivery`) and delivers **at most once**: the
  row is claimed `uncertain` (reason `claimed`) and committed *before* the
  send, so a crash after the claim can never resend; a successful send moves
  it to `sent`, a definite failure back to `queued` for a bounded retry, an
  ambiguous outcome stays `uncertain` and is never retried (§6 invariant 6
  spirit, P2-T25).
- Texts are rendered at delivery from the rows the `refs` name
  (`inbox/alert_texts.py`); a vanished source suppresses the alert.

Standard alerts wait in `queued` while flag `notifications.deliver` is off;
system alerts are never gated, delayed or batched (policy rule 1).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import preferences
from assistant.accounts.callbacks import encode
from assistant.accounts.models import Account, PendingInput
from assistant.accounts.service import create_card
from assistant.conversations.models import Contact
from assistant.core.channel import Button, ControlChannel
from assistant.core.clock import Clock
from assistant.core.jobs import NOTIFY_TASK, JobScheduler, notification_queueing_lock
from assistant.db.uow import after_commit, transaction
from assistant.inbox.alert_texts import RENDERERS, RenderedAlert, Renderer
from assistant.inbox.models import Notification
from assistant.inbox.notification_policy import (
    MUTE_WINDOW,
    FeedbackMark,
    NotificationFacts,
    OwnerAlertSettings,
    RecentLoad,
    Verdict,
    decide_delivery,
    muted_pairs,
)
from assistant.inbox.notification_types import (
    AlertClass,
    NotificationKind,
    NotificationRefs,
    Priority,
)
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

#: Sends of one notification before a definite failure is final.
MAX_ATTEMPTS = 3
#: Wait before the next attempt, times the attempt number.
RETRY_BACKOFF = timedelta(seconds=30)
_CAP_WINDOW = timedelta(hours=1)
_SUSPICIOUS_WINDOW = timedelta(hours=24)
#: `reason_code` of a definite failure; `not_delivered:<n>` while retrying.
_NOT_DELIVERED = "not_delivered"

#: The alert card (F0-interfaces §7): `pending_inputs` kind, `ref_id` = the
#: notification. Its buttons are not single-use: the owner may change the
#: feedback, so a press looks the card up instead of claiming it.
NTF_CARD = "ntf"
OP_OPEN_CHAT = "op"
OP_USEFUL = "us"
OP_NOT_USEFUL = "nu"
NTF_BUTTONS = (("Open chat", OP_OPEN_CHAT), ("Useful", OP_USEFUL), ("Not useful", OP_NOT_USEFUL))
#: Long enough to answer an evening's alerts the next day; the rows go
#: with the notifications' own 30 days anyway.
NTF_CARD_TTL = timedelta(days=7)


def _refs_json(refs: NotificationRefs) -> dict[str, Any]:
    """Ids only; empty lists and an unset `alert_kind` are left out."""
    return {
        key: list(value) if isinstance(value, tuple) else value
        for key, value in asdict(refs).items()
        if value not in ((), None)
    }


async def defer_delivery(
    scheduler: JobScheduler, notification_id: int, *, at: datetime | None
) -> int | None:
    return await scheduler.defer(
        NOTIFY_TASK,
        args={"notification_id": notification_id},
        queueing_lock=notification_queueing_lock(notification_id),
        schedule_at=at,
    )


def _defer_after_commit(
    session: AsyncSession, scheduler: JobScheduler, notification_id: int, at: datetime | None
) -> None:
    # After commit, never inside: a queueing-lock conflict would abort the
    # caller's transaction (F0-interfaces §6.1).
    async def defer(_factory: async_sessionmaker[AsyncSession]) -> None:
        await defer_delivery(scheduler, notification_id, at=at)

    after_commit(session, defer)


async def enqueue(
    session: AsyncSession,
    *,
    account_id: int,
    kind: NotificationKind,
    priority: Priority,
    dedupe_key: str,
    refs: NotificationRefs,
    clock: Clock,
    alert_class: AlertClass = AlertClass.STANDARD,
    conversation_id: int | None = None,
    contact_id: int | None = None,
    bypass_quiet_hours: bool = False,
    scheduler: JobScheduler | None = None,
) -> int | None:
    """Queue one alert; None when this account already used `dedupe_key`.
    With `scheduler`, delivery is deferred once the caller's transaction
    commits; without one, the reconciler picks the row up within a minute."""
    now = clock.now()
    statement = (
        insert(Notification)
        .values(
            account_id=account_id,
            kind=kind.value,
            alert_class=alert_class.value,
            priority=priority.value,
            dedupe_key=dedupe_key,
            refs=_refs_json(refs),
            conversation_id=conversation_id,
            contact_id=contact_id,
            bypass_quiet_hours=bypass_quiet_hours,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_notifications_account_id_dedupe_key")
        .returning(Notification.id)
    )
    notification_id = (await session.execute(statement)).scalar_one_or_none()
    if notification_id is not None and scheduler is not None:
        _defer_after_commit(session, scheduler, notification_id, None)
    return notification_id


# ------------------------------------------------------------------ delivery
@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """What one `deliver` call did (codes only)."""

    status: str
    reason: str | None = None


def quiet_hours(value: object) -> tuple[time, time] | None:
    """`"HH:MM-HH:MM"` as a pair of local times; `"off"`, or anything the
    validator would refuse, as None."""
    try:
        start, end = preferences.check_quiet_hours(value).split("-")
    except preferences.PreferenceError, ValueError:
        return None
    return time.fromisoformat(start), time.fromisoformat(end)


def _attempts(reason_code: str | None) -> int:
    """Definite failures so far, from `not_delivered:<n>`."""
    prefix = f"{_NOT_DELIVERED}:"
    if reason_code and reason_code.startswith(prefix):
        tail = reason_code.removeprefix(prefix)
        return int(tail) if tail.isdigit() else 0
    return 0


class NotificationDelivery:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        channel: ControlChannel,
        clock: Clock,
        scheduler: JobScheduler,
        enabled: bool,
        renderers: Mapping[NotificationKind, Renderer] = RENDERERS,
    ) -> None:
        self._session_factory = session_factory
        self._channel = channel
        self._clock = clock
        self._scheduler = scheduler
        #: Flag `notifications.deliver`: standard alerts wait while it is off.
        self._enabled = enabled
        self._renderers: dict[NotificationKind, Renderer] = dict(renderers)

    def register_renderer(self, kind: NotificationKind, renderer: Renderer) -> None:
        """For kinds rendered outside this package (S05.4's `MONITOR_MATCH`)."""
        self._renderers[kind] = renderer

    async def deliver(self, notification_id: int) -> DeliveryResult:
        """Decide, claim, send, record. Idempotent: anything not `queued`
        (sent, claimed, batched…) is left alone, so a second job is a no-op."""
        now = self._clock.now()
        async with transaction(self._session_factory) as uow:
            session = uow.session
            notification = (
                await session.execute(
                    select(Notification).where(Notification.id == notification_id).with_for_update()
                )
            ).scalar_one_or_none()
            if notification is None or notification.status != "queued":
                return DeliveryResult("not_queued")
            if notification.deliver_after is not None and notification.deliver_after > now:
                # Early (a retry or a quiet-hours hold): its own job runs then.
                return DeliveryResult("not_due")
            account = (
                await session.execute(
                    select(Account)
                    .where(Account.id == notification.account_id)
                    .with_for_update(read=True)
                )
            ).scalar_one_or_none()
            if account is None or account.status == "deleted":
                return self._settle(notification, "suppressed", "account_gone", now)
            if notification.alert_class == AlertClass.STANDARD and not self._enabled:
                return DeliveryResult("flag_off")
            if account.control_chat_id is None:
                return self._settle(notification, "failed", "unreachable", now)
            decision = decide_delivery(
                await self._facts(session, notification),
                await self._settings(session, account, now),
                await self._load(session, notification, now),
                now,
            )
            if decision.verdict is Verdict.BATCH:
                return self._settle(notification, "batched", decision.reason, now)
            if decision.verdict is Verdict.SUPPRESS:
                return self._settle(notification, "suppressed", decision.reason, now)
            if decision.verdict is Verdict.DEFER:
                notification.deliver_after = decision.deliver_after
                notification.reason_code = decision.reason
                notification.updated_at = now
                _defer_after_commit(
                    session, self._scheduler, notification.id, decision.deliver_after
                )
                return DeliveryResult("deferred", decision.reason)
            rendered = await self._render(session, notification, account)
            if rendered is None:
                return self._settle(notification, "suppressed", "source_deleted", now)
            attempt = _attempts(notification.reason_code) + 1
            # The at-most-once claim (P2-T25): committed before the send.
            notification.status = "uncertain"
            notification.reason_code = "claimed"
            notification.updated_at = now
            card_id, buttons = await self._card(session, notification, rendered)
            account_id, chat_id, kind = account.id, account.control_chat_id, notification.kind

        sent = await self._channel.send(
            account_id=account_id, chat_id=chat_id, text=rendered.text, buttons=buttons
        )
        result = await self._record(
            notification_id,
            ok=sent.ok,
            uncertain=sent.uncertain,
            message_id=sent.message_id,
            attempt=attempt,
            card_id=card_id,
        )
        logger.info(
            "notification_delivered",
            account_id=account_id,
            notification_id=notification_id,
            kind=kind,
            status=result.status,
            reason=result.reason,
        )
        return result

    async def _record(
        self,
        notification_id: int,
        *,
        ok: bool,
        uncertain: bool,
        message_id: int | None,
        attempt: int,
        card_id: int | None,
    ) -> DeliveryResult:
        now = self._clock.now()
        async with transaction(self._session_factory) as uow:
            card = None if card_id is None else await uow.session.get(PendingInput, card_id)
            if card is not None:
                if ok:
                    card.prompt_message_id = message_id
                elif not uncertain:
                    # Definitely not on screen: the next attempt makes its own.
                    await uow.session.delete(card)
            notification = await uow.session.get(Notification, notification_id)
            if notification is None:  # its chat's data was deleted meanwhile
                return DeliveryResult("sent" if ok else "uncertain", "row_deleted")
            notification.updated_at = now
            if ok:
                notification.status = "sent"
                notification.reason_code = None
                notification.sent_at = now
                notification.message_id = message_id
                return DeliveryResult("sent")
            if uncertain:
                notification.reason_code = "ambiguous"
                return DeliveryResult("uncertain", "ambiguous")
            if attempt >= MAX_ATTEMPTS:
                notification.status = "failed"
                notification.reason_code = _NOT_DELIVERED
                return DeliveryResult("failed", _NOT_DELIVERED)
            retry_at = now + RETRY_BACKOFF * attempt
            notification.status = "queued"
            notification.reason_code = f"{_NOT_DELIVERED}:{attempt}"
            notification.deliver_after = retry_at
            _defer_after_commit(uow.session, self._scheduler, notification_id, retry_at)
            return DeliveryResult("retry", notification.reason_code)

    @staticmethod
    def _settle(
        notification: Notification, status: str, reason: str, now: datetime
    ) -> DeliveryResult:
        notification.status = status
        notification.reason_code = reason
        notification.updated_at = now
        return DeliveryResult(status, reason)

    async def _card(
        self, session: AsyncSession, notification: Notification, rendered: RenderedAlert
    ) -> tuple[int | None, tuple[Button, ...]]:
        """A standard alert about a chat is a card: Open chat, Useful, Not
        useful (S05.2 §3.2). Created in the claim transaction."""
        if notification.alert_class != AlertClass.STANDARD or rendered.conversation_id is None:
            return None, ()
        card = await create_card(
            session,
            account_id=notification.account_id,
            kind=NTF_CARD,
            clock=self._clock,
            ref_id=notification.id,
            ttl=NTF_CARD_TTL,
        )
        token = card.token or ""
        return card.id, tuple(
            Button(label, encode(NTF_CARD, token, op)) for label, op in NTF_BUTTONS
        )

    async def _render(
        self, session: AsyncSession, notification: Notification, account: Account
    ) -> RenderedAlert | None:
        try:
            kind = NotificationKind(notification.kind)
        except ValueError:
            return None
        renderer = self._renderers.get(kind)
        return None if renderer is None else await renderer(session, notification, account)

    # ------------------------------------------------------------ policy inputs
    async def _facts(self, session: AsyncSession, notification: Notification) -> NotificationFacts:
        contact_priority = None
        if notification.contact_id is not None:
            contact_priority = (
                await session.execute(
                    select(Contact.priority)
                    .where(Contact.account_id == notification.account_id)
                    .where(Contact.id == notification.contact_id)
                )
            ).scalar_one_or_none()
        return NotificationFacts(
            kind=NotificationKind(notification.kind),
            alert_class=AlertClass(notification.alert_class),
            priority=Priority(notification.priority),
            contact_id=notification.contact_id,
            contact_priority=contact_priority,
            bypass_quiet_hours=notification.bypass_quiet_hours,
        )

    async def _settings(
        self, session: AsyncSession, account: Account, now: datetime
    ) -> OwnerAlertSettings:
        values = preferences.current(account)
        marks = (
            await session.execute(
                select(
                    Notification.kind,
                    Notification.contact_id,
                    Notification.feedback,
                    Notification.updated_at,
                )
                .where(Notification.account_id == account.id)
                .where(Notification.feedback.is_not(None))
                .where(Notification.updated_at >= now - MUTE_WINDOW)
            )
        ).all()
        return OwnerAlertSettings(
            timezone=account.timezone,
            quiet_hours=quiet_hours(values["quiet_hours"]),
            urgent_bypass=bool(values["urgent_bypass"]),
            cap_per_hour=int(values["alert_cap_per_hour"]),
            muted=muted_pairs(
                (
                    FeedbackMark(kind, contact, feedback, at)
                    for kind, contact, feedback, at in marks
                    if feedback is not None
                ),
                now,
            ),
        )

    async def _load(
        self, session: AsyncSession, notification: Notification, now: datetime
    ) -> RecentLoad:
        since = now - _CAP_WINDOW
        # Claimed (`uncertain`) rows count like sent ones (TL ruling, #53).
        immediate = await session.scalar(
            select(func.count(Notification.id))
            .where(Notification.account_id == notification.account_id)
            .where(Notification.alert_class == AlertClass.STANDARD)
            .where(Notification.id != notification.id)
            .where(
                or_(
                    and_(Notification.status == "sent", Notification.sent_at >= since),
                    and_(Notification.status == "uncertain", Notification.updated_at >= since),
                )
            )
        )
        suspicious = 0
        if notification.contact_id is not None:
            suspicious = (
                await session.scalar(
                    select(func.count(Notification.id))
                    .where(Notification.account_id == notification.account_id)
                    .where(Notification.kind == NotificationKind.TRIAGE_SUSPICIOUS)
                    .where(Notification.contact_id == notification.contact_id)
                    .where(Notification.id != notification.id)
                    .where(Notification.created_at >= now - _SUSPICIOUS_WINDOW)
                )
                or 0
            )
        return RecentLoad(
            immediate_last_hour=immediate or 0, suspicious_from_contact_24h=suspicious
        )


# ------------------------------------------------------------------ /status
@dataclass(frozen=True, slots=True)
class NotificationCounts:
    """The `/status` notifications block (S05.2 §3.8): counts only."""

    queued: int = 0
    #: Held until the owner's quiet hours end.
    deferred: int = 0
    #: Waiting for the next digest.
    batched: int = 0
    #: Sent with an unknown outcome (or claimed) in the last 24 hours.
    uncertain_24h: int = 0


async def notification_counts(
    session: AsyncSession, *, account_id: int, now: datetime
) -> NotificationCounts:
    held = and_(Notification.deliver_after.is_not(None), Notification.deliver_after > now)
    row = (
        await session.execute(
            select(
                func.count().filter(and_(Notification.status == "queued", ~held)),
                func.count().filter(
                    and_(
                        Notification.status == "queued",
                        held,
                        Notification.reason_code == "quiet_hours",
                    )
                ),
                func.count().filter(
                    and_(Notification.status == "batched", Notification.digest_id.is_(None))
                ),
                func.count().filter(
                    and_(
                        Notification.status == "uncertain",
                        Notification.updated_at >= now - timedelta(hours=24),
                    )
                ),
            ).where(Notification.account_id == account_id)
        )
    ).one()
    return NotificationCounts(*(int(value) for value in row))


# ------------------------------------------------------------------ digest side
@dataclass(frozen=True, slots=True)
class NotificationView:
    """A batched alert as the digest reads it (ids, enums, times)."""

    notification_id: int
    kind: str
    priority: str
    conversation_id: int | None
    contact_id: int | None
    refs: Mapping[str, Any]
    created_at: datetime


async def notifications_batched_since(
    session: AsyncSession, *, account_id: int, since: datetime
) -> list[NotificationView]:
    """This account's alerts batched for the digest since `since` and not yet
    carried by one; oldest first."""
    rows = (
        await session.execute(
            select(Notification)
            .where(Notification.account_id == account_id)
            .where(Notification.status == "batched")
            .where(Notification.digest_id.is_(None))
            .where(Notification.created_at >= since)
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


async def mark_digested(
    session: AsyncSession, *, account_id: int, ids: Collection[int], digest_id: int | None
) -> int:
    """Link batched alerts to the digest that carries them, so the next one
    does not repeat them. Returns how many of this account's rows changed."""
    if not ids:
        return 0
    result = await session.execute(
        update(Notification)
        .where(Notification.account_id == account_id)
        .where(Notification.id.in_(sorted(ids)))
        .where(Notification.status == "batched")
        .where(Notification.digest_id.is_(None))
        .values(digest_id=digest_id)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]
