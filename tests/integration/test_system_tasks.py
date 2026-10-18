"""System tasks against a real database (S01.5).

- the ingestion watchdog detects a stale poller heartbeat and alerts the owner,
  at most once per cooldown; a fresh heartbeat raises nothing;
- a dead-lettered update raises one owner alert, ever;
- the retention sweep clears inbound payloads after 7 days, except rows still
  pending;
- budget thresholds alert once per month each;
- an undelivered alert is not counted as sent, so it is tried again.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import service
from assistant.accounts.models import AuditLogEntry
from assistant.agent.budget import check_budget_thresholds
from assistant.agent.models import LlmCall
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.models import KEY_POLLER_HEARTBEAT
from assistant.db.uow import transaction
from assistant.inbox.models import InboundUpdate
from assistant.inbox.notify import ALERT_TEXT, AlertKind, OwnerAlerts
from assistant.inbox.watchdog import check_ingestion
from assistant.privacy.retention import sweep_inbound_payloads
from assistant.telegram.ingress.writer import write_state
from tests.fakes.channel import FakeChannel
from tests.fakes.clock import FakeClock

pytestmark = pytest.mark.integration

OWNER = 7000000001
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
async def factory(migrated_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_url)
    reset = "TRUNCATE app.accounts, app.inbound_updates, app.system_state CASCADE"
    async with engine.begin() as conn:
        await conn.exec_driver_sql(reset)
    yield build_session_factory(engine)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(reset)
    await engine.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


async def owner(factory: async_sessionmaker[AsyncSession], clock: FakeClock) -> int:
    async with transaction(factory) as uow:
        account, _ = await service.get_or_create_account(
            uow.session, telegram_user_id=OWNER, clock=clock
        )
        await service.set_control_chat(uow.session, account, OWNER)
        return account.id


async def heartbeat(factory: async_sessionmaker[AsyncSession], at: datetime) -> None:
    async with transaction(factory) as uow:
        await write_state(uow.session, KEY_POLLER_HEARTBEAT, {"at": at.isoformat(), "offset": 1})


async def update_row(
    factory: async_sessionmaker[AsyncSession],
    *,
    update_id: int,
    status: str,
    received_at: datetime,
    processed_at: datetime | None = None,
) -> int:
    async with transaction(factory) as uow:
        row = InboundUpdate(
            bot_id=8845134387,
            update_id=update_id,
            kind="message",
            payload={"update_id": update_id, "message": {"text": "synthetic"}},
            status=status,
            received_at=received_at,
            processed_at=processed_at,
        )
        uow.session.add(row)
        await uow.session.flush()
        return row.id


# ---------------------------------------------------------------- watchdog
async def test_a_stale_heartbeat_alerts_the_owner_once_per_cooldown(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    await owner(factory, clock)
    channel = FakeChannel()
    alerts = OwnerAlerts(factory, channel, clock)
    await heartbeat(factory, NOW - timedelta(minutes=4))

    first = await check_ingestion(factory, clock, alerts)
    clock.advance(minutes=1)
    await check_ingestion(factory, clock, alerts)
    clock.advance(hours=1)
    await check_ingestion(factory, clock, alerts)

    assert first.stalled is True
    assert channel.sends == [ALERT_TEXT[AlertKind.INGESTION_STALL]] * 2


async def test_a_fresh_heartbeat_raises_nothing(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    await owner(factory, clock)
    channel = FakeChannel()
    await heartbeat(factory, NOW - timedelta(seconds=40))

    report = await check_ingestion(factory, clock, OwnerAlerts(factory, channel, clock))

    assert report.stalled is False
    assert channel.sends == []


async def test_each_dead_letter_alerts_once(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    await owner(factory, clock)
    await heartbeat(factory, NOW)
    channel = FakeChannel()
    alerts = OwnerAlerts(factory, channel, clock)
    await update_row(factory, update_id=1, status="failed", received_at=NOW, processed_at=NOW)

    await check_ingestion(factory, clock, alerts)
    await check_ingestion(factory, clock, alerts)
    await update_row(factory, update_id=2, status="failed", received_at=NOW, processed_at=NOW)
    await check_ingestion(factory, clock, alerts)

    assert channel.sends == [ALERT_TEXT[AlertKind.DEAD_LETTER]] * 2


async def test_an_undelivered_alert_is_tried_again(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    account_id = await owner(factory, clock)
    channel = FakeChannel(deliver=False)
    alerts = OwnerAlerts(factory, channel, clock)

    first = await alerts.notify_owner(account_id, AlertKind.DEAD_LETTER, dedupe_key="dead_letter:9")
    channel.deliver = True
    second = await alerts.notify_owner(
        account_id, AlertKind.DEAD_LETTER, dedupe_key="dead_letter:9"
    )

    assert (first.sent, second.sent) == (False, True)
    async with factory() as session:
        entries = (await session.execute(select(AuditLogEntry))).scalars().all()
    assert [entry.details["delivered"] for entry in entries] == [False, True]
    assert all("synthetic" not in str(entry.details) for entry in entries)


# --------------------------------------------------------------- retention
async def test_inbound_payloads_are_cleared_after_seven_days_unless_pending(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    old_done = await update_row(
        factory, update_id=1, status="processed", received_at=NOW - timedelta(days=8)
    )
    old_pending = await update_row(
        factory, update_id=2, status="pending", received_at=NOW - timedelta(days=8)
    )
    recent = await update_row(
        factory, update_id=3, status="processed", received_at=NOW - timedelta(days=6)
    )

    cleared = await sweep_inbound_payloads(factory, clock)

    assert cleared == 1
    async with factory() as session:
        payloads = {
            row.id: row.payload
            for row in (await session.execute(select(InboundUpdate))).scalars().all()
        }
    assert payloads[old_done] is None
    assert payloads[old_pending] is not None
    assert payloads[recent] is not None


# ------------------------------------------------------------------ budget
async def test_budget_thresholds_alert_once_per_month_each(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    account_id = await owner(factory, clock)
    channel = FakeChannel()
    alerts = OwnerAlerts(factory, channel, clock)

    async def spend(amount: str) -> None:
        async with transaction(factory) as uow:
            uow.session.add(
                LlmCall(
                    account_id=account_id,
                    purpose="owner_request",
                    model="gpt-6-luna",
                    cost_estimate=Decimal(amount),
                    created_at=clock.now(),
                )
            )

    async def check() -> list[int]:
        return await check_budget_thresholds(
            factory, clock, alerts, account_id=account_id, monthly_budget_usd=Decimal("10")
        )

    await spend("4.00")
    assert await check() == []
    await spend("4.50")  # 85 %
    assert await check() == [50, 80]
    assert await check() == []
    await spend("2.00")  # 105 %
    assert await check() == [100]
    clock.set(datetime(2026, 11, 1, 0, 5, tzinfo=UTC))  # a new month starts from zero
    assert await check() == []
    assert len(channel.sends) == 3
