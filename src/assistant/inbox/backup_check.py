"""Backup freshness and the restore notice (ARCHITECTURE §15.4, §15.6, §17).

The backup sidecar (`deploy/backup/backup.sh`) writes the UTC time of its
last successful dump to a file the app can read. Once a day the app checks
it and alerts the owner when it is missing or stale — silently failing
backups are the failure mode that hurts most, so this is a `system` alert
(never suppressed, §17).

After a restore, `post-restore` leaves `system_state.restore_notice_pending`;
the first watchdog run of the restarted app delivers the notice and clears
the key.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


@dataclass(frozen=True, slots=True)
class BackupReport:
    last_success_at: datetime | None
    stale: bool
    alerted: bool


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
    clock: Clock, alerts: OwnerAlerts, *, status_path: Path | None
) -> BackupReport | None:
    """None when the check is disabled (no path configured, e.g. in dev)."""
    if status_path is None:
        return None
    last = read_last_success(status_path)
    age = None if last is None else clock.now() - last
    stale = age is None or age > BACKUP_MAX_AGE
    alerted = False
    if stale:
        hours = "unknown" if age is None else str(int(age.total_seconds() // 3600))
        sent = await alerts.notify_all_owners(
            AlertKind.BACKUP_STALE,
            dedupe_key="backup_stale",
            cooldown=BACKUP_ALERT_COOLDOWN,
            hours=hours,
        )
        alerted = sent > 0
        logger.warning("backup_stale", hours=hours, alerted=alerted)
    return BackupReport(last_success_at=last, stale=stale, alerted=alerted)


async def deliver_restore_notice(
    session_factory: async_sessionmaker[AsyncSession], alerts: OwnerAlerts
) -> bool:
    """Send the §15.6 notice left by `post-restore`, once, then clear it."""
    async with transaction(session_factory) as uow:
        pending = await _notice(uow.session)
        if pending is None:
            return False
        value = dict(pending.value)
    sent = await alerts.notify_all_owners(
        AlertKind.RESTORE_NOTICE,
        dedupe_key=f"restore:{value.get('at', '')}",
        backup_taken_at=str(value.get("backup_taken_at", "unknown")),
    )
    if sent > 0:
        async with transaction(session_factory) as uow:
            pending = await _notice(uow.session)
            if pending is not None:
                await uow.session.delete(pending)
    return sent > 0


async def _notice(session: AsyncSession) -> SystemState | None:
    return await session.get(SystemState, KEY_RESTORE_NOTICE)
