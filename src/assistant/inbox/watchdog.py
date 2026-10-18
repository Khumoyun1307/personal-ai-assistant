"""Ingestion watchdog (ARCHITECTURE §12.2 `system.ingestion_watchdog`, §17).

Two things the owner must hear about, through `notify_owner`:

- **ingestion stall** — the poller's heartbeat in `system_state` is older than
  `STALL_AFTER`. Telegram keeps updates for up to 24 h, so a stall is
  recoverable, but only if someone knows. At most one alert per hour.
- **dead-lettered updates** — a row the inbox set aside after repeated
  deterministic failures. One alert per row, ever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.core.clock import Clock
from assistant.db.models import KEY_POLLER_HEARTBEAT, SystemState
from assistant.db.uow import transaction
from assistant.inbox.models import InboundUpdate
from assistant.inbox.notify import AlertKind, OwnerAlerts

#: The same threshold the supervisor uses for the poller's liveness (§4.3).
STALL_AFTER = timedelta(minutes=3)
STALL_ALERT_COOLDOWN = timedelta(hours=1)
#: Dead letters older than this have been seen (or the owner was offline);
#: the backlog still shows them in /status.
DEAD_LETTER_LOOKBACK = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class WatchdogReport:
    heartbeat_age: timedelta | None
    stalled: bool
    dead_letters: int


async def poller_heartbeat_at(session: AsyncSession) -> datetime | None:
    row = await session.execute(
        select(SystemState.value).where(SystemState.key == KEY_POLLER_HEARTBEAT)
    )
    value = row.scalar_one_or_none()
    if not value or not value.get("at"):
        return None
    return datetime.fromisoformat(value["at"])


async def check_ingestion(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock, alerts: OwnerAlerts
) -> WatchdogReport:
    now = clock.now()
    async with transaction(session_factory) as uow:
        beat = await poller_heartbeat_at(uow.session)
        failed = await uow.session.execute(
            select(InboundUpdate.id)
            .where(InboundUpdate.status == "failed")
            .where(InboundUpdate.processed_at >= now - DEAD_LETTER_LOOKBACK)
            .order_by(InboundUpdate.id)
        )
        failed_ids = list(failed.scalars().all())

    # No heartbeat at all means the poller never started on this database:
    # nothing has stalled yet. The supervisor covers a poller that cannot start.
    age = None if beat is None else now - beat
    stalled = age is not None and age > STALL_AFTER
    if stalled:
        await alerts.notify_all_owners(
            AlertKind.INGESTION_STALL,
            dedupe_key="ingestion_stall",
            cooldown=STALL_ALERT_COOLDOWN,
        )
    for row_id in failed_ids:
        await alerts.notify_all_owners(AlertKind.DEAD_LETTER, dedupe_key=f"dead_letter:{row_id}")
    return WatchdogReport(heartbeat_age=age, stalled=stalled, dead_letters=len(failed_ids))
