"""`notify_owner` — the one path for system alerts (ARCHITECTURE §17, S01.5).

Every system alert (ingestion stall, dead-lettered updates, budget thresholds;
later connection, rights and delivery alerts) goes through `OwnerAlerts`.
In M01 to M04 it sends straight to the control chat; M05 puts the `notifications`
table behind the same call.

Alerts of class `system` are never suppressed by quiet hours or batching
(§17), but each one is **deduplicated** by a key, so a watchdog that runs every
minute does not page the owner every minute. The send and the dedupe record
are both written to `audit_log` (content-free: kind, key, delivered).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, AuditLogEntry
from assistant.core.channel import ControlChannel
from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

EVENT_ALERT = "system_alert"


class AlertKind(StrEnum):
    INGESTION_STALL = "ingestion_stall"
    DEAD_LETTER = "dead_letter"
    BUDGET_THRESHOLD = "budget_threshold"
    #: Connected, rights changed, disabled, reconnected (FR-ONB-07). The body is
    #: composed by `inbox.connection_alerts` from the registry's state.
    CONNECTION_CHANGE = "connection_change"
    #: The backup sidecar has not recorded a success for more than a day (§17).
    BACKUP_STALE = "backup_stale"
    #: The database was restored from a backup and post-restore ran (§15.6).
    RESTORE_NOTICE = "restore_notice"


#: Owner-facing text per kind. Plain statements of fact (§6 invariant 11).
ALERT_TEXT: dict[AlertKind, str] = {
    AlertKind.INGESTION_STALL: (
        "⚠️ I have not been able to receive Telegram updates for a few minutes. "
        "Messages are queued by Telegram for up to 24 hours; I will catch up when this clears."
    ),
    AlertKind.DEAD_LETTER: (
        "⚠️ An incoming update could not be processed after several attempts and was set "
        "aside. Nothing was lost; it can be retried once the problem is fixed."
    ),
    AlertKind.BUDGET_THRESHOLD: "💰 This month's AI spend has reached {percent}% of the budget.",
    AlertKind.CONNECTION_CHANGE: "{body}",
    AlertKind.BACKUP_STALE: (
        "⚠️ No successful database backup for {hours} hours. Check the backup container "
        "(runbooks/deploy.md) — until it works, a server loss would lose recent data."
    ),
    AlertKind.RESTORE_NOTICE: (
        "♻️ The database was restored from a backup taken {backup_taken_at}. Everything "
        "decided after that is undone: autonomy is paused, open approvals were cancelled, "
        "and messages from the gap may be missing (it is recorded as a coverage gap). "
        "Please check /status and re-apply any settings, consent or pause you changed since."
    ),
}


@dataclass(frozen=True, slots=True)
class AlertResult:
    sent: bool
    reason: str


class OwnerAlerts:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        channel: ControlChannel,
        clock: Clock,
    ) -> None:
        self._session_factory = session_factory
        self._channel = channel
        self._clock = clock

    async def notify_owner(
        self,
        account_id: int,
        kind: AlertKind,
        *,
        dedupe_key: str,
        cooldown: timedelta | None = None,
        **values: int | str,
    ) -> AlertResult:
        """Send one alert unless the same `dedupe_key` was sent recently.

        `cooldown=None` means once ever for that key (e.g. one alert per
        dead-lettered row, one per budget threshold per month).
        """
        async with transaction(self._session_factory) as uow:
            account = await uow.session.get(Account, account_id)
            if account is None or account.control_chat_id is None:
                return AlertResult(False, "unreachable")
            if await self._recently_sent(uow.session, account_id, dedupe_key, cooldown):
                return AlertResult(False, "duplicate")
            chat_id = account.control_chat_id

        delivery = await self._channel.send(
            account_id=account_id, chat_id=chat_id, text=ALERT_TEXT[kind].format(**values)
        )
        async with transaction(self._session_factory) as uow:
            uow.session.add(
                AuditLogEntry(
                    account_id=account_id,
                    at=self._clock.now(),
                    actor="system",
                    event=EVENT_ALERT,
                    details={"kind": kind.value, "key": dedupe_key, "delivered": delivery.ok},
                )
            )
        logger.info("owner_alert", account_id=account_id, kind=kind.value, delivered=delivery.ok)
        return AlertResult(delivery.ok, "sent" if delivery.ok else "not_delivered")

    async def _recently_sent(
        self,
        session: AsyncSession,
        account_id: int,
        dedupe_key: str,
        cooldown: timedelta | None,
    ) -> bool:
        statement = (
            select(AuditLogEntry.id)
            .where(AuditLogEntry.account_id == account_id)
            .where(AuditLogEntry.event == EVENT_ALERT)
            .where(AuditLogEntry.details["key"].astext == dedupe_key)
            # Only delivered alerts count: an undelivered one should be retried.
            .where(AuditLogEntry.details["delivered"].astext == "true")
        )
        if cooldown is not None:
            statement = statement.where(AuditLogEntry.at >= self._clock.now() - cooldown)
        return (await session.execute(statement.limit(1))).first() is not None

    async def notify_all_owners(
        self,
        kind: AlertKind,
        *,
        dedupe_key: str,
        cooldown: timedelta | None = None,
        **values: int | str,
    ) -> int:
        """A deployment-level alert (e.g. ingestion stall) to every reachable owner."""
        async with transaction(self._session_factory) as uow:
            result = await uow.session.execute(
                select(Account.id).where(
                    Account.control_chat_id.is_not(None), Account.status != "deleted"
                )
            )
            account_ids = list(result.scalars().all())
        sent = 0
        for account_id in account_ids:
            outcome = await self.notify_owner(
                account_id, kind, dedupe_key=dedupe_key, cooldown=cooldown, **values
            )
            sent += int(outcome.sent)
        return sent
