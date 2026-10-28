"""Synchronous, content-free system notification claims (CP4 ICR #132).

The audit claim survives notification retention and a process interruption.
Only a proven failed resolution permits another attempt. Text and buttons
remain in memory; unlike standard notifications, callers own retries.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import BigInteger, String, cast, exists, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from assistant.accounts.models import Account, AuditLogEntry
from assistant.core.channel import Button, ControlChannel, Delivery
from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.inbox.models import Notification

EVENT_CLAIMED = "system_alert_claimed"
EVENT_RESOLVED = "system_alert"
# Two-int4 advisory space is separate from the singleton bigint lock space.
SYSTEM_LOCK_NAMESPACE = 0x53595341


def system_lock_key(account_id: int, dedupe_key: str) -> int:
    encoded = json.dumps([account_id, dedupe_key], ensure_ascii=False, separators=(",", ":"))
    return int.from_bytes(hashlib.sha256(encoded.encode("utf-8")).digest()[:4], signed=True)


async def _serialize(session: AsyncSession, account_id: int, key: str) -> Account | None:
    account = await session.scalar(
        select(Account).where(Account.id == account_id).with_for_update(read=True, key_share=True)
    )
    if account is not None:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :key)"),
            {"namespace": SYSTEM_LOCK_NAMESPACE, "key": system_lock_key(account_id, key)},
        )
    return account


@dataclass(frozen=True, slots=True)
class SystemAlertOutcome:
    sent: bool
    reason: str
    message_id: int | None = None
    uncertain: bool = False


@dataclass(frozen=True, slots=True)
class SystemAlertClaim:
    account_id: int
    chat_id: int
    notification_id: int
    claim_id: int
    key: str
    kind: str


class SystemNotificationDelivery:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        channel: ControlChannel,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._channel = channel
        self._clock = clock

    async def send(
        self,
        *,
        account_id: int,
        kind: str,
        key: str,
        cooldown: timedelta | None,
        rendered_text: str,
        buttons: Sequence[Button],
    ) -> SystemAlertOutcome:
        claimed = await self._claim(account_id, kind, key, cooldown)
        if isinstance(claimed, SystemAlertOutcome):
            return claimed
        # An exception or cancellation leaves the committed claim unresolved.
        # No recovery process may guess that Telegram did not receive it.
        delivery = await self._channel.send(
            account_id=claimed.account_id,
            chat_id=claimed.chat_id,
            text=rendered_text,
            buttons=buttons,
        )
        return await self._record(claimed, delivery)

    async def _claim(
        self, account_id: int, kind: str, key: str, cooldown: timedelta | None
    ) -> SystemAlertClaim | SystemAlertOutcome:
        now = self._clock.now()
        async with transaction(self._session_factory) as uow:
            session = uow.session
            account = await _serialize(session, account_id, key)
            if account is None or account.control_chat_id is None:
                return SystemAlertOutcome(False, "unreachable")
            resolution = aliased(AuditLogEntry)
            resolved = exists().where(
                resolution.account_id == account_id,
                resolution.event == EVENT_RESOLVED,
                resolution.details["claim_id"].astext == cast(AuditLogEntry.id, String),
            )
            unresolved = await session.scalar(
                select(AuditLogEntry.id)
                .where(
                    AuditLogEntry.account_id == account_id,
                    AuditLogEntry.event == EVENT_CLAIMED,
                    AuditLogEntry.details["key"].astext == key,
                    ~resolved,
                )
                .limit(1)
            )
            if unresolved is not None:
                return SystemAlertOutcome(False, "in_progress")
            recent = select(AuditLogEntry.id).where(
                AuditLogEntry.account_id == account_id,
                AuditLogEntry.event == EVENT_RESOLVED,
                AuditLogEntry.details["key"].astext == key,
                or_(
                    AuditLogEntry.details["delivered"].astext == "true",
                    AuditLogEntry.details["uncertain"].astext == "true",
                ),
            )
            if cooldown is not None:
                recent = recent.where(AuditLogEntry.at >= now - cooldown)
            if await session.scalar(recent.limit(1)) is not None:
                return SystemAlertOutcome(False, "duplicate")
            prior_ms = await session.scalar(
                select(
                    func.max(cast(AuditLogEntry.details["attempt_ms"].astext, BigInteger))
                ).where(
                    AuditLogEntry.account_id == account_id,
                    AuditLogEntry.event == EVENT_CLAIMED,
                    AuditLogEntry.details["key"].astext == key,
                )
            )
            attempt_ms = max(int(now.timestamp() * 1000), (prior_ms or 0) + 1)
            # Keep names unique even with an existing row from an earlier build.
            while (
                await session.scalar(
                    select(Notification.id).where(
                        Notification.account_id == account_id,
                        Notification.dedupe_key == f"sys:{key}:{attempt_ms}",
                    )
                )
                is not None
            ):
                attempt_ms += 1
            notification = Notification(
                account_id=account_id,
                kind="system",
                alert_class="system",
                priority="urgent",
                status="uncertain",
                reason_code="claimed",
                dedupe_key=f"sys:{key}:{attempt_ms}",
                refs={"alert_kind": kind},
                created_at=now,
                updated_at=now,
            )
            session.add(notification)
            await session.flush()
            claim = AuditLogEntry(
                account_id=account_id,
                at=now,
                actor="system",
                event=EVENT_CLAIMED,
                details={
                    "kind": kind,
                    "key": key,
                    "notification_id": notification.id,
                    "attempt_ms": attempt_ms,
                },
            )
            session.add(claim)
            await session.flush()
            return SystemAlertClaim(
                account_id, account.control_chat_id, notification.id, claim.id, key, kind
            )

    async def _record(self, claim: SystemAlertClaim, delivery: Delivery) -> SystemAlertOutcome:
        now = self._clock.now()
        uncertain = not delivery.ok and delivery.uncertain
        async with transaction(self._session_factory) as uow:
            session = uow.session
            await _serialize(session, claim.account_id, claim.key)
            previous = await session.scalar(
                select(AuditLogEntry).where(
                    AuditLogEntry.account_id == claim.account_id,
                    AuditLogEntry.event == EVENT_RESOLVED,
                    AuditLogEntry.details["claim_id"].astext == str(claim.claim_id),
                )
            )
            if previous is not None:
                # A repeated resolution cannot change a recorded outcome.
                return self._outcome(
                    previous.details["delivered"] is True,
                    previous.details.get("uncertain") is True,
                    None,
                )
            notification = await session.scalar(
                select(Notification)
                .where(
                    Notification.account_id == claim.account_id,
                    Notification.id == claim.notification_id,
                )
                .with_for_update()
            )
            if notification is not None:
                notification.status = (
                    "sent" if delivery.ok else ("uncertain" if uncertain else "failed")
                )
                notification.reason_code = (
                    None if delivery.ok else ("ambiguous" if uncertain else "not_delivered")
                )
                notification.updated_at = now
                if delivery.ok:
                    notification.sent_at = now
                    notification.message_id = delivery.message_id
            details: dict[str, object] = {
                "kind": claim.kind,
                "key": claim.key,
                "delivered": delivery.ok,
                "claim_id": claim.claim_id,
                "notification_id": claim.notification_id,
            }
            if uncertain:
                details["uncertain"] = True
            session.add(
                AuditLogEntry(
                    account_id=claim.account_id,
                    at=now,
                    actor="system",
                    event=EVENT_RESOLVED,
                    details=details,
                )
            )
        return self._outcome(delivery.ok, uncertain, delivery.message_id)

    @staticmethod
    def _outcome(ok: bool, uncertain: bool, message_id: int | None) -> SystemAlertOutcome:
        return SystemAlertOutcome(
            ok,
            "sent" if ok else ("uncertain" if uncertain else "not_delivered"),
            message_id if ok else None,
            uncertain,
        )
