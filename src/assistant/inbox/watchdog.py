"""Ingestion watchdog (ARCHITECTURE §12.2 `system.ingestion_watchdog`, §17).

Two things the owner must hear about, through `notify_owner`:

- **ingestion stall** — the poller's heartbeat in `system_state` is older than
  `STALL_AFTER`. Telegram keeps updates for up to 24 h, so a stall is
  recoverable, but only if someone knows. At most one alert per hour.
- **dead-lettered updates** — rows the inbox set aside after repeated
  deterministic failures. They can be requeued only while they still hold a
  payload, and the retention sweep clears it after the raw-update period (7
  days unless the owner shortened it; S03.5), so the alert says by when,
  goes to the account the rows belong to, and repeats daily until the
  payloads are gone (§6.2). It is content-free: a count and a date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.core.clock import Clock
from assistant.db.models import KEY_POLLER_HEARTBEAT, SystemState
from assistant.db.uow import transaction
from assistant.inbox.models import InboundUpdate
from assistant.inbox.notify import AlertKind, OwnerAlerts
from assistant.privacy import retention_policy

#: The same threshold the supervisor uses for the poller's liveness (§4.3).
STALL_AFTER = timedelta(minutes=3)
STALL_ALERT_COOLDOWN = timedelta(hours=1)
#: A set of recoverable dead letters is announced again after this long.
DEAD_LETTER_REPEAT = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class WatchdogReport:
    heartbeat_age: timedelta | None
    stalled: bool
    dead_letters: int


@dataclass(frozen=True, slots=True)
class _DeadLetters:
    """The recoverable dead letters of one account (None: not attributable)."""

    account_id: int | None
    count: int
    newest_row_id: int
    purge_at: datetime
    timezone: str


async def poller_heartbeat_at(session: AsyncSession) -> datetime | None:
    row = await session.execute(
        select(SystemState.value).where(SystemState.key == KEY_POLLER_HEARTBEAT)
    )
    value = row.scalar_one_or_none()
    if not value or not value.get("at"):
        return None
    return datetime.fromisoformat(value["at"])


async def _recoverable_dead_letters(session: AsyncSession) -> list[_DeadLetters]:
    """Failed rows that still hold a payload, grouped by account."""
    result = await session.execute(
        select(
            InboundUpdate.id,
            InboundUpdate.account_id,
            InboundUpdate.received_at,
            Account.timezone,
            Account.settings,
        )
        .outerjoin(Account, Account.id == InboundUpdate.account_id)
        .where(InboundUpdate.status == "failed")
        .where(InboundUpdate.payload.is_not(None))
        .order_by(InboundUpdate.id)
    )
    rows = result.all()
    # Rows with no account are swept after the shortest period any owner
    # chose (S03.5, M03 D80): the same rule as the sweep, so the date holds.
    all_settings = (await session.execute(select(Account.settings))).scalars().all()
    unrouted = retention_policy.unrouted_period(all_settings, retention_policy.RAW_UPDATES)
    groups: dict[int | None, _DeadLetters] = {}
    for row_id, account_id, received_at, timezone, settings in rows:
        # The owner's shorter raw-update retention, if set (S03.5).
        purge_at = received_at + (
            unrouted
            if account_id is None
            else retention_policy.period(settings, retention_policy.RAW_UPDATES)
        )
        known = groups.get(account_id)
        groups[account_id] = _DeadLetters(
            account_id=account_id,
            count=1 if known is None else known.count + 1,
            newest_row_id=row_id if known is None else max(known.newest_row_id, row_id),
            purge_at=purge_at if known is None else min(known.purge_at, purge_at),
            timezone=timezone or "UTC",
        )
    return list(groups.values())


def _local(value: datetime, timezone: str) -> str:
    """Presentation only, in the owner's timezone (AGENTS §9)."""
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        zone = ZoneInfo("UTC")
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


async def check_ingestion(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock, alerts: OwnerAlerts
) -> WatchdogReport:
    now = clock.now()
    async with transaction(session_factory) as uow:
        beat = await poller_heartbeat_at(uow.session)
        dead_letters = await _recoverable_dead_letters(uow.session)

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
    for group in dead_letters:
        # Keyed by the newest row: a new failure alerts at once; the same set
        # repeats daily until the sweep has cleared its payloads.
        key = f"dead_letter:{group.newest_row_id}"
        if group.account_id is None:
            # Not attributable (a row that failed before its account was
            # known): a deployment-level alert, content-free.
            await alerts.notify_all_owners(
                AlertKind.DEAD_LETTER,
                dedupe_key=key,
                cooldown=DEAD_LETTER_REPEAT,
                count=group.count,
                purge_date=_local(group.purge_at, "UTC"),
            )
            continue
        await alerts.notify_owner(
            group.account_id,
            AlertKind.DEAD_LETTER,
            dedupe_key=key,
            cooldown=DEAD_LETTER_REPEAT,
            count=group.count,
            purge_date=_local(group.purge_at, group.timezone),
        )
    total = sum(group.count for group in dead_letters)
    return WatchdogReport(heartbeat_age=age, stalled=stalled, dead_letters=total)
