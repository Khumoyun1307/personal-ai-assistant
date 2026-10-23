"""Connection-change notices (FR-ONB-04, FR-ONB-07, J1 step 5).

The `connections.notify_owner` job body: it reloads the event, tells the owner
the current capability tier, asks for managed-conversation consent when a
connection goes live without it, and pages once per event.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import service
from assistant.accounts.models import BusinessConnection, BusinessConnectionEvent, PendingInput
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
    assert "Let Alviro's AI agent work with these chats?" in notice
    assert "The text is processed by OpenAI and isn't used to train their models" in notice
    assert "When it's off, Alviro still tracks which chats are connected" in notice
    assert [b.label for b in channel.buttons[0]] == ["Turn on", "Not now"]
    card = await _consent_card(factory, account_id, clock)
    assert card is not None
    assert card.prompt_message_id == result.message_id
    assert all(f":{card.token}:" in b.callback_data for b in channel.buttons[0])


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
    assert "Let Alviro's AI agent work with these chats?" not in notice
    assert channel.buttons == [()]


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


# ------------------------------------------------ the managed-consent card (S02.6)
async def _consent_card(
    factory: async_sessionmaker[AsyncSession], account_id: int, clock: FakeClock
) -> PendingInput | None:
    async with factory() as session:
        return await service.live_card(
            session, account_id=account_id, kind=service.MANAGED_CONSENT_CARD, clock=clock
        )


async def _event(factory: async_sessionmaker[AsyncSession], account_id: int, change: str) -> int:
    async with transaction(factory) as uow:
        event = BusinessConnectionEvent(
            account_id=account_id,
            connection_id="synthetic-conn-a",
            payload_digest="synthetic",
            rights=["can_reply"],
            is_enabled=True,
            change=change,
            source="update",
        )
        uow.session.add(event)
        await uow.session.flush()
        return event.id


async def test_the_consent_card_is_opened_only_after_the_notice_arrived(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A failed send must leave no open question behind (S02.6)."""
    clock = FakeClock(NOW)
    channel = FakeChannel(deliver=False)
    account_id, event_id = await seed(factory, clock, change="connected")

    result = await notify_connection_change(
        factory, clock, OwnerAlerts(factory, channel, clock), event_id
    )

    assert not result.sent
    assert await _consent_card(factory, account_id, clock) is None
    async with factory() as session:
        assert await service.get_pending_input(session, account_id=account_id, clock=clock) is None


async def test_a_reconnect_does_not_reopen_an_open_question(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClock(NOW)
    channel = FakeChannel()
    alerts = OwnerAlerts(factory, channel, clock)
    account_id, event_id = await seed(factory, clock, change="connected")
    await notify_connection_change(factory, clock, alerts, event_id)
    first = await _consent_card(factory, account_id, clock)

    clock.advance(hours=2)
    again = await _event(factory, account_id, "reconnected")
    await notify_connection_change(factory, clock, alerts, again)

    assert len(channel.sends) == 2
    assert "The question above about letting Alviro's AI agent" in channel.sends[-1]
    assert "is still open" in channel.sends[-1]
    assert channel.buttons[-1] == ()
    async with factory() as session:
        cards = (
            (
                await session.execute(
                    select(PendingInput).where(PendingInput.kind == service.MANAGED_CONSENT_CARD)
                )
            )
            .scalars()
            .all()
        )
    assert [card.token for card in cards] == [first.token if first else None]


async def test_a_reconnect_soon_after_a_decline_does_not_ask_again(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClock(NOW)
    channel = FakeChannel()
    alerts = OwnerAlerts(factory, channel, clock)
    account_id, _ = await seed(factory, clock, change="connected")
    async with transaction(factory) as uow:
        await service.record_managed_consent_declined(
            uow.session, account_id=account_id, clock=clock
        )

    clock.advance(hours=3)
    await notify_connection_change(
        factory, clock, alerts, await _event(factory, account_id, "reconnected")
    )
    assert "You chose <b>Not now</b>" in channel.sends[-1]
    assert channel.buttons[-1] == ()
    assert await _consent_card(factory, account_id, clock) is None

    # A day later, a new connection may ask again.
    clock.advance(hours=24)
    await notify_connection_change(
        factory, clock, alerts, await _event(factory, account_id, "reconnected")
    )
    assert "Let Alviro's AI agent work with these chats?" in channel.sends[-1]
    assert await _consent_card(factory, account_id, clock) is not None
