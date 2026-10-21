"""Connection-change notices (FR-ONB-04, FR-ONB-07, J1 step 5).

The `connections.notify_owner` job body: it reloads the event, tells the owner
the current capability tier, asks for managed-conversation consent when a
connection goes live without it, and pages once per event.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import service
from assistant.accounts.models import BusinessConnection, BusinessConnectionEvent
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.uow import transaction
from assistant.inbox.connection_alerts import notify_connection_change
from assistant.inbox.notify import OwnerAlerts
from tests.fakes.channel import FakeChannel
from tests.fakes.clock import FakeClock

pytestmark = pytest.mark.integration

OWNER = 7000000001
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
RESET = "TRUNCATE app.accounts, app.inbound_updates, app.system_state CASCADE"


@pytest.fixture
async def factory(migrated_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(RESET)
    yield build_session_factory(engine)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(RESET)
    await engine.dispose()


async def seed(
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
    *,
    change: str,
    tier: str = "can_reply",
    rights: tuple[str, ...] = ("can_reply",),
    reachable: bool = True,
    managed_consent: bool = False,
) -> tuple[int, int]:
    async with transaction(factory) as uow:
        account, _ = await service.get_or_create_account(
            uow.session, telegram_user_id=OWNER, clock=clock
        )
        account.status = "active"
        if reachable:
            await service.set_control_chat(uow.session, account, OWNER)
        if managed_consent:
            await service.grant_consent(
                uow.session,
                account,
                kind=service.CONSENT_AI_MANAGED,
                policy_version="v2",
                processors=["OpenAI"],
                clock=clock,
            )
        uow.session.add(
            BusinessConnection(
                account_id=account.id,
                connection_id="synthetic-conn-a",
                is_enabled=tier != "none",
                rights=list(rights),
                capability_tier=tier,
                user_chat_id=OWNER,
                status="disabled" if tier == "none" else "active",
                connected_at=NOW,
            )
        )
        event = BusinessConnectionEvent(
            account_id=account.id,
            connection_id="synthetic-conn-a",
            payload_digest="synthetic",
            rights=list(rights),
            is_enabled=tier != "none",
            change=change,
            source="update",
        )
        uow.session.add(event)
        await uow.session.flush()
        return account.id, event.id


async def test_a_new_connection_states_the_tier_and_asks_for_managed_consent(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClock(NOW)
    channel = FakeChannel()
    account_id, event_id = await seed(factory, clock, change="connected")

    result = await notify_connection_change(
        factory, clock, OwnerAlerts(factory, channel, clock), event_id
    )

    assert result.sent
    [notice] = channel.sends
    assert "Connected to your account" in notice
    assert "Can reply" in notice
    assert "cannot see older history" in notice
    assert "OpenAI" in notice
    assert "Reply <b>yes</b>" in notice
    async with factory() as session:
        pending = await service.get_pending_input(session, account_id=account_id, clock=clock)
    assert pending is not None
    assert pending.kind == service.PENDING_MANAGED_CONSENT


async def test_no_consent_question_once_managed_consent_is_given(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClock(NOW)
    channel = FakeChannel()
    _, event_id = await seed(factory, clock, change="reconnected", managed_consent=True)

    await notify_connection_change(factory, clock, OwnerAlerts(factory, channel, clock), event_id)

    [notice] = channel.sends
    assert "Reconnected" in notice
    assert "never reached me" in notice
    assert "Reply <b>yes</b>" not in notice


async def test_a_disconnect_says_processing_stopped(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClock(NOW)
    channel = FakeChannel()
    _, event_id = await seed(factory, clock, change="disabled", tier="none", rights=())

    await notify_connection_change(factory, clock, OwnerAlerts(factory, channel, clock), event_id)

    [notice] = channel.sends
    assert "disconnected" in notice
    assert "stopped processing" in notice


async def test_unused_rights_are_named_so_the_owner_can_turn_them_off(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClock(NOW)
    channel = FakeChannel()
    _, event_id = await seed(
        factory,
        clock,
        change="rights_changed",
        tier="extended",
        rights=("can_reply", "can_delete_all_messages", "can_edit_bio", "can_transfer_stars"),
    )

    await notify_connection_change(factory, clock, OwnerAlerts(factory, channel, clock), event_id)

    [notice] = channel.sends
    for name in ("can_delete_all_messages", "can_edit_bio", "can_transfer_stars"):
        assert f"<code>{name}</code>" in notice
    assert "<code>can_reply</code>" not in notice


async def test_each_event_pages_once(factory: async_sessionmaker[AsyncSession]) -> None:
    clock = FakeClock(NOW)
    channel = FakeChannel()
    alerts = OwnerAlerts(factory, channel, clock)
    _, event_id = await seed(factory, clock, change="connected", managed_consent=True)

    await notify_connection_change(factory, clock, alerts, event_id)
    second = await notify_connection_change(factory, clock, alerts, event_id)

    assert len(channel.sends) == 1
    assert (second.sent, second.reason) == (False, "duplicate")


async def test_an_owner_who_never_pressed_start_is_not_messaged(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClock(NOW)
    channel = FakeChannel()
    _, event_id = await seed(factory, clock, change="connected", reachable=False)

    result = await notify_connection_change(
        factory, clock, OwnerAlerts(factory, channel, clock), event_id
    )

    assert (result.sent, result.reason) == (False, "unreachable")
    assert channel.sends == []
