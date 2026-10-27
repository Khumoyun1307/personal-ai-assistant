"""Backup freshness and the restore notice (ARCHITECTURE §15.4, §15.6, §17).

The backup sidecar (`deploy/backup/backup.sh`) writes the UTC time of its
last successful dump to a file the app can read, and — in a separate marker
next to it — the time of its last complete off-site sync. Once a day the app
checks both and alerts the owner when one is missing or stale (the upload
marker only while an off-site remote is configured). Silently failing
backups are the failure mode that hurts most, so these are `system` alerts
(never suppressed, §17).

After a restore, `post-restore` leaves `system_state.restore_notice_pending`;
the first watchdog run of the restarted app delivers the notice and clears
the key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.core.clock import Clock
from assistant.db.models import SystemState
from assistant.db.uow import transaction
from assistant.inbox.notify import AlertKind, OwnerAlerts
from assistant.observability.logging import get_logger
from assistant.privacy.post_restore import KEY_RESTORE_NOTICE

logger = get_logger(__name__)

#: A daily backup that is more than this old has missed a run.
BACKUP_MAX_AGE = timedelta(hours=26)
BACKUP_ALERT_COOLDOWN = timedelta(hours=23)
#: The sidecar's off-site marker, in the same directory as `last_success`.
UPLOAD_MARKER_NAME = "last_upload_success"


class AlertSink(Protocol):
    """The part of `OwnerAlerts` this check uses."""

    async def notify_all_owners(
        self,
        kind: AlertKind,
        *,
        dedupe_key: str,
        cooldown: timedelta | None = None,
        **values: int | str,
    ) -> int: ...


@dataclass(frozen=True, slots=True)
class BackupReport:
    last_success_at: datetime | None
    stale: bool
    alerted: bool
    #: None when no off-site remote is configured (the upload is not checked).
    last_upload_at: datetime | None = None
    upload_stale: bool | None = None
    upload_alerted: bool = False


def read_last_success(path: Path) -> datetime | None:
    """The marker's timestamp, or None when absent or unreadable."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


async def check_backup(
    clock: Clock,
    alerts: AlertSink,
    *,
    status_path: Path | None,
    upload_required: bool = False,
) -> BackupReport | None:
    """None when the check is disabled (no path configured, e.g. in dev).

    `upload_required` is true while an off-site remote is configured
    (`BACKUP_REMOTE`); the upload marker is then checked as well, because a
    dump that never leaves the host does not survive the loss of the host.
    """
    if status_path is None:
        return None
    last, stale, alerted = await _check_marker(
        clock, alerts, status_path, AlertKind.BACKUP_STALE, event="backup_stale"
    )
    if not upload_required:
        return BackupReport(last_success_at=last, stale=stale, alerted=alerted)
    last_upload, upload_stale, upload_alerted = await _check_marker(
        clock,
        alerts,
        status_path.with_name(UPLOAD_MARKER_NAME),
        AlertKind.BACKUP_UPLOAD_STALE,
        event="backup_upload_stale",
    )
    return BackupReport(
        last_success_at=last,
        stale=stale,
        alerted=alerted,
        last_upload_at=last_upload,
        upload_stale=upload_stale,
        upload_alerted=upload_alerted,
    )


async def _check_marker(
    clock: Clock, alerts: AlertSink, path: Path, kind: AlertKind, *, event: str
) -> tuple[datetime | None, bool, bool]:
    """(timestamp, stale, alerted) for one marker; alerts at most once a day."""
    last = read_last_success(path)
    age = None if last is None else clock.now() - last
    stale = age is None or age > BACKUP_MAX_AGE
    alerted = False
    if stale:
        hours = "unknown" if age is None else str(int(age.total_seconds() // 3600))
        sent = await alerts.notify_all_owners(
            kind, dedupe_key=event, cooldown=BACKUP_ALERT_COOLDOWN, hours=hours
        )
        alerted = sent > 0
        logger.warning(event, hours=hours, alerted=alerted)
    return last, stale, alerted


async def deliver_restore_notice(
    session_factory: async_sessionmaker[AsyncSession], alerts: OwnerAlerts
) -> bool:
    """Send the §15.6 notice left by `post-restore`, once, then clear it."""
    async with transaction(session_factory) as uow:
        pending = await _notice(uow.session)
        if pending is None:
            return False
        value = dict(pending.value)
        accounts = (
            await uow.session.execute(
                select(Account.id, Account.timezone).where(
                    Account.control_chat_id.is_not(None),
                    Account.status != "deleted",
                )
            )
        ).all()
    sent = 0
    action_notices = value.get("actions", {})
    for account_id, timezone in accounts:
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError, ValueError, KeyError:
            zone = ZoneInfo("UTC")
        account_actions = (
            action_notices.get(str(account_id), {}) if isinstance(action_notices, dict) else {}
        )
        parts = []
        for kind, label in (
            ("cancelled", "Cancelled actions"),
            ("uncertain", "Delivery uncertain"),
        ):
            rows = account_actions.get(kind, []) if isinstance(account_actions, dict) else []
            entries = []
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("id"), int):
                        continue
                    created_at = row.get("created_at")
                    if not isinstance(created_at, str):
                        continue
                    try:
                        local = datetime.fromisoformat(created_at).astimezone(zone)
                    except ValueError:
                        continue
                    entries.append(f"#{row['id']} ({local:%Y-%m-%d %H:%M %Z})")
            if isinstance(account_actions, dict) and kind in account_actions:
                parts.append(
                    f"{label}: {len(entries)}" + (" — " + "; ".join(entries) if entries else "")
                )
        outcome = await alerts.notify_owner(
            account_id,
            AlertKind.RESTORE_NOTICE,
            dedupe_key=f"restore:{value.get('at', '')}",
            backup_taken_at=str(value.get("backup_taken_at", "unknown")),
            actions="\n" + "\n".join(parts) if parts else "",
            buttons=(),
        )
        sent += int(outcome.sent)
    if sent > 0:
        async with transaction(session_factory) as uow:
            pending = await _notice(uow.session)
            if pending is not None:
                await uow.session.delete(pending)
    return sent > 0


async def _notice(session: AsyncSession) -> SystemState | None:
    return await session.get(SystemState, KEY_RESTORE_NOTICE)
