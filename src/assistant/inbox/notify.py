"""`notify_owner` — the one path for system alerts (ARCHITECTURE §17, S01.5).

Every system alert (ingestion stall, dead-lettered updates, budget thresholds;
later connection, rights and delivery alerts) goes through `OwnerAlerts`.
M05 puts content-free notification and audit claims behind the same call.

Alerts of class `system` are never suppressed by quiet hours or batching
(§17), but each one is **deduplicated** by a key, so a watchdog that runs every
minute does not page the owner every minute. The send and the dedupe record
are both written to `audit_log` (content-free: kind, key, delivered).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.core.channel import Button, ControlChannel
from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.inbox.system_notifications import (
    EVENT_CLAIMED,
    EVENT_RESOLVED,
    SystemNotificationDelivery,
)
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

EVENT_ALERT = EVENT_RESOLVED
#: Committed before the channel call (CP4 ICR #132); unresolved, the alert
#: may be on screen.
EVENT_ALERT_CLAIMED = EVENT_CLAIMED


class AlertKind(StrEnum):
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    CONNECTION_STALE = "connection_stale"
    INGESTION_STALL = "ingestion_stall"
    DEAD_LETTER = "dead_letter"
    BUDGET_THRESHOLD = "budget_threshold"
    #: Connected, rights changed, disabled, reconnected (FR-ONB-07). The body is
    #: composed by `inbox.connection_alerts` from the registry's state.
    CONNECTION_CHANGE = "connection_change"
    #: The backup sidecar has not recorded a success for more than a day (§17).
    BACKUP_STALE = "backup_stale"
    #: Backups are produced but have not reached off-site storage for more
    #: than a day (§15.4); only checked while an off-site remote is configured.
    BACKUP_UPLOAD_STALE = "backup_upload_stale"
    #: The database was restored from a backup and post-restore ran (§15.6).
    RESTORE_NOTICE = "restore_notice"
    #: A change the owner confirmed was cut off while being applied (S03.1).
    CONFIRMATION_INTERRUPTED = "confirmation_interrupted"
    #: The privacy policy changed since the owner consented (AGENTS §16; policy
    #: v4). Sent once per version by `inbox.policy_notice`.
    POLICY_UPDATED = "policy_updated"


#: Owner-facing text per kind. Plain statements of fact (§6 invariant 11).
ALERT_TEXT: dict[AlertKind, str] = {
    AlertKind.INGESTION_STALL: (
        "⚠️ I have not been able to receive Telegram updates for a few minutes. "
        "Messages are queued by Telegram for up to 24 hours; I will catch up when this clears."
    ),
    AlertKind.DEAD_LETTER: (
        "⚠️ {count} incoming update(s) could not be processed after several attempts and "
        "were set aside. They can be retried once the problem is fixed, but only until "
        "{purge_date}: then their content is deleted and they cannot be recovered. "
        "I will remind you daily until then."
    ),
    AlertKind.BUDGET_THRESHOLD: "💰 This month's AI spend has reached {percent}% of the budget.",
    #: S04.4: the contact's name (escaped) is rendered at delivery from ids,
    #: never the reply's text; "uncertain" is never reported as sent (§6
    #: invariants 6, 11).
    AlertKind.DELIVERY_UNCERTAIN: (
        "⚠️ I'm not sure a reply reached {contact}: Telegram did not confirm it. "
        "Please check the chat. I will not resend it."
    ),
    AlertKind.CONNECTION_STALE: (
        "⚠️ Telegram stopped accepting replies through the old connection and no new one "
        "has arrived for {minutes} minutes. Open Chat Automation in Telegram and check "
        "Alviro is connected."
    ),
    AlertKind.CONNECTION_CHANGE: "{body}",
    AlertKind.BACKUP_STALE: (
        "⚠️ No successful database backup for {hours} hours. Check the backup container "
        "(runbooks/deploy.md) — until it works, a server loss would lose recent data."
    ),
    AlertKind.BACKUP_UPLOAD_STALE: (
        "⚠️ The off-site copy of the database backups has not been updated for {hours} "
        "hours. Backups are still kept on the server, but a server loss would lose them; "
        "check the backup container (runbooks/restore.md). If uploads failed for more "
        "than about a week, the next sync is refused on purpose — it would delete more "
        "old off-site dumps than the guard allows — and stays refused until it is run "
        "once by hand (runbooks/restore.md §C)."
    ),
    AlertKind.RESTORE_NOTICE: (
        "♻️ The database was restored from a backup taken {backup_taken_at}. Everything "
        "decided after that is undone: autonomy is paused, open approvals were cancelled, "
        "and messages from the gap may be missing (it is recorded as a coverage gap). "
        "Please check /status and re-apply any settings, consent or pause you changed since."
        "{actions}"
    ),
    AlertKind.CONFIRMATION_INTERRUPTED: (
        "⚠️ A change you confirmed (<code>{tool}</code>) was interrupted while it was being "
        "applied. It may or may not have taken effect; please check before asking again. "
        "I am not counting it as done."
    ),
    #: The body is per version (`inbox.policy_notice.POLICY_CHANGES`): it has
    #: to say what that version changes and how to opt out of it.
    AlertKind.POLICY_UPDATED: "📄 {body}",
}


@dataclass(frozen=True, slots=True)
class AlertResult:
    sent: bool
    reason: str
    #: The control-chat message id, when delivered (a card needs it).
    message_id: int | None = None
    #: Not delivered as far as we know, but it may be on screen (timeout).
    uncertain: bool = False


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
        self._system_delivery = SystemNotificationDelivery(session_factory, channel, clock)

    async def notify_owner(
        self,
        account_id: int,
        kind: AlertKind,
        *,
        dedupe_key: str,
        cooldown: timedelta | None = None,
        buttons: Sequence[Button] = (),
        **values: int | str,
    ) -> AlertResult:
        """Send one alert unless the same `dedupe_key` was sent recently.

        `cooldown=None` means once ever for that key (e.g. one per budget
        threshold per month). `buttons` make the alert a card (§6.7).
        An unresolved prior claim returns `in_progress`; it is neither a
        settled duplicate nor evidence of definite non-delivery (ICR #132).
        """
        if kind is AlertKind.RESTORE_NOTICE:
            values.setdefault("actions", "")
        outcome = await self._system_delivery.send(
            account_id=account_id,
            kind=kind.value,
            key=dedupe_key,
            cooldown=cooldown,
            rendered_text=ALERT_TEXT[kind].format(**values),
            buttons=buttons,
        )
        logger.info(
            "owner_alert",
            account_id=account_id,
            kind=kind.value,
            delivered=outcome.sent,
            uncertain=outcome.uncertain,
            reason=outcome.reason,
        )
        return AlertResult(
            outcome.sent,
            outcome.reason,
            outcome.message_id,
            uncertain=outcome.uncertain,
        )

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
                account_id, kind, dedupe_key=dedupe_key, cooldown=cooldown, buttons=(), **values
            )
            sent += int(outcome.sent)
        return sent
