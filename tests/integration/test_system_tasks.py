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
from pathlib import Path

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import service
from assistant.accounts.models import (
    AuditLogEntry,
    BusinessConnectionEvent,
    ControlTurn,
    ForeignConnection,
)
from assistant.agent.budget import check_budget_thresholds
from assistant.agent.models import AgentRun, LlmCall
from assistant.conversations.models import Contact, Conversation, Message
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.models import KEY_POLLER_HEARTBEAT
from assistant.db.uow import transaction
from assistant.inbox.backup_check import check_backup
from assistant.inbox.models import InboundUpdate
from assistant.inbox.notify import ALERT_TEXT, AlertKind, OwnerAlerts
from assistant.inbox.watchdog import check_ingestion
from assistant.privacy.retention import sweep_all, sweep_inbound_payloads
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


async def test_connection_history_and_foreign_connections_expire(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    """Connection events: 1 year. Foreign connections: 30 days after last seen."""
    account_id = await owner(factory, clock)
    async with transaction(factory) as uow:
        for age_days in (366, 10):
            uow.session.add(
                BusinessConnectionEvent(
                    account_id=account_id,
                    connection_id=f"synthetic-{age_days}",
                    payload_digest="synthetic",
                    rights=[],
                    is_enabled=True,
                    change="connected",
                    source="update",
                    observed_at=NOW - timedelta(days=age_days),
                )
            )
            uow.session.add(
                ForeignConnection(
                    connection_id=f"synthetic-foreign-{age_days}",
                    telegram_user_id=7000000009,
                    first_seen_at=NOW - timedelta(days=age_days),
                    last_seen_at=NOW - timedelta(days=age_days if age_days < 30 else 31),
                )
            )

    try:
        report = await sweep_all(factory, clock)

        assert (report.connection_events, report.foreign_connections) == (1, 1)
        async with factory() as session:
            kept_events = (
                (await session.execute(select(BusinessConnectionEvent.connection_id)))
                .scalars()
                .all()
            )
            kept_foreign = (
                (await session.execute(select(ForeignConnection.connection_id))).scalars().all()
            )
        assert kept_events == ["synthetic-10"]
        assert kept_foreign == ["synthetic-foreign-10"]
    finally:
        async with transaction(factory) as uow:
            await uow.session.execute(delete(ForeignConnection))


async def test_every_policy_retention_rule_is_applied(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    """The privacy policy's retention table, enforced (FR-PRV-03, AGENTS §9)."""
    account_id = await owner(factory, clock)
    async with transaction(factory) as uow:
        old_turn = await service.record_control_turn(
            uow.session,
            account_id=account_id,
            role="owner",
            text="Synthetic old",
            created_at=NOW - timedelta(days=91),
        )
        new_turn = await service.record_control_turn(
            uow.session,
            account_id=account_id,
            role="owner",
            text="Synthetic new",
            created_at=NOW - timedelta(days=89),
        )
        for age_days in (366, 10):
            uow.session.add(
                LlmCall(
                    account_id=account_id,
                    purpose="owner_request",
                    model="gpt-6-luna",
                    created_at=NOW - timedelta(days=age_days),
                )
            )
            uow.session.add(
                AuditLogEntry(
                    account_id=account_id,
                    actor="system",
                    event="synthetic",
                    at=NOW - timedelta(days=age_days),
                )
            )
            uow.session.add(
                AgentRun(
                    account_id=account_id,
                    run_type="owner_request",
                    model="gpt-6-luna",
                    status="succeeded",
                    started_at=NOW - timedelta(days=age_days),
                )
            )

    report = await sweep_all(factory, clock)

    assert (report.control_turn_texts, report.llm_calls, report.agent_runs) == (1, 1, 1)
    assert report.audit_entries == 1
    async with factory() as session:
        texts = {
            turn.id: turn.text
            for turn in (await session.execute(select(ControlTurn))).scalars().all()
        }
        assert len((await session.execute(select(LlmCall))).scalars().all()) == 1
        assert len((await session.execute(select(AuditLogEntry))).scalars().all()) == 1
        assert len((await session.execute(select(AgentRun))).scalars().all()) == 1
    assert texts == {old_turn.id: None, new_turn.id: "Synthetic new"}


# ------------------------------------------------- retention: managed content
async def _seed_message(
    factory: async_sessionmaker[AsyncSession],
    account_id: int,
    *,
    chat_id: int,
    sent_at: datetime,
    message_text: str,
    name: str,
) -> int:
    async with transaction(factory) as uow:
        contact = Contact(
            account_id=account_id,
            telegram_user_id=chat_id,
            display_name=name,
            updated_at=sent_at,
        )
        uow.session.add(contact)
        await uow.session.flush()
        conversation = Conversation(
            account_id=account_id, business_chat_id=chat_id, contact_id=contact.id
        )
        uow.session.add(conversation)
        await uow.session.flush()
        uow.session.add(
            Message(
                account_id=account_id,
                conversation_id=conversation.id,
                telegram_message_id=1,
                direction="contact",
                sent_at=sent_at,
                content_type="text",
                text=message_text,
                search_key=message_text,
                content_hash="synthetic-hash",
            )
        )
        return conversation.id


async def test_message_content_is_purged_after_ninety_days_and_the_gap_recorded(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    """§10.4: the retention period is a hard ceiling, summary or not; the
    conversation remembers that content expired unsummarised."""
    account_id = await owner(factory, clock)
    old = await _seed_message(
        factory,
        account_id,
        chat_id=5001,
        sent_at=NOW - timedelta(days=91),
        message_text="synthetic old",
        name="Old Synthetic",
    )
    fresh = await _seed_message(
        factory,
        account_id,
        chat_id=5002,
        sent_at=NOW - timedelta(days=10),
        message_text="synthetic fresh",
        name="Fresh Synthetic",
    )

    report = await sweep_all(factory, clock)

    assert report.message_contents == 1
    assert report.contact_names == 1
    async with factory() as session:
        purged = (
            await session.execute(select(Message).where(Message.conversation_id == old))
        ).scalar_one()
        kept = (
            await session.execute(select(Message).where(Message.conversation_id == fresh))
        ).scalar_one()
        old_conv = await session.get(Conversation, old)
        fresh_conv = await session.get(Conversation, fresh)
        names = (await session.execute(select(Contact.display_name))).scalars().all()
    assert purged.text is None
    assert purged.search_key is None
    assert purged.content_hash is None
    assert purged.purged_at == NOW
    assert purged.deleted_at is None  # expired, not deleted: the tombstone says which
    assert kept.text == "synthetic fresh"
    assert old_conv is not None
    assert old_conv.summary_gap_before == NOW - timedelta(days=91)
    assert fresh_conv is not None
    assert fresh_conv.summary_gap_before is None
    assert set(names) == {None, "Fresh Synthetic"}


# ------------------------------------------------------------ backup check
async def test_backup_check_is_disabled_without_a_status_path(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    channel = FakeChannel()
    alerts = OwnerAlerts(factory, channel, clock)
    assert await check_backup(clock, alerts, status_path=None) is None
    assert channel.sends == []


async def test_a_fresh_backup_marker_raises_nothing(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, tmp_path: Path
) -> None:
    await owner(factory, clock)
    marker = tmp_path / "last_success"
    marker.write_text((NOW - timedelta(hours=3)).isoformat(), encoding="utf-8")
    channel = FakeChannel()
    report = await check_backup(clock, OwnerAlerts(factory, channel, clock), status_path=marker)
    assert report is not None
    assert report.stale is False
    assert channel.sends == []


@pytest.mark.parametrize("state", ["missing", "stale", "garbage"])
async def test_a_missing_or_stale_backup_alerts_the_owner_once_a_day(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, tmp_path: Path, state: str
) -> None:
    await owner(factory, clock)
    marker = tmp_path / "last_success"
    if state == "stale":
        marker.write_text((NOW - timedelta(hours=30)).isoformat(), encoding="utf-8")
    elif state == "garbage":
        marker.write_text("not a timestamp", encoding="utf-8")
    channel = FakeChannel()
    alerts = OwnerAlerts(factory, channel, clock)

    first = await check_backup(clock, alerts, status_path=marker)
    second = await check_backup(clock, alerts, status_path=marker)
    clock.advance(hours=24)
    third = await check_backup(clock, alerts, status_path=marker)

    assert first is not None
    assert (first.stale, first.alerted) == (True, True)
    assert second is not None
    assert (second.stale, second.alerted) == (True, False)  # cooldown
    assert third is not None
    assert third.alerted is True
    assert len(channel.sends) == 2
    assert "No successful database backup" in channel.sends[0]
    if state == "stale":
        assert "for 30 hours" in channel.sends[0]
    else:
        assert "for unknown hours" in channel.sends[0]
