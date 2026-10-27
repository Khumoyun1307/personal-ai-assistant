"""The inbox drives the control chat end to end (S01.5 wiring of S01.3).

A stored update goes through the real `InboxProcessor` with the real
`route_update` and `ControlInboxHandlers`:

- an owner command gets its reply through the gateway, never with a
  `business_connection_id`;
- owner free text becomes a control turn plus an `agent.owner_request` job;
- a non-allowlisted sender gets the fixed refusal once per window, no account
  or turn is created, and the payload is cleared (§6 invariant 12);
- a button press is always answered;
- a business message never reaches the control handlers (§6 invariant 5).
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, ControlTurn, PendingInput
from assistant.core.routing import Route
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.uow import transaction
from assistant.inbox.models import InboundUpdate
from assistant.inbox.processor import InboxProcessor
from assistant.jobs.app import build_app
from assistant.jobs.scheduler import ProcrastinateScheduler
from assistant.telegram.control.dispatcher import ControlDispatcher
from assistant.telegram.control.handlers import REFUSAL_TEXT
from assistant.telegram.control.inbox_handlers import (
    CALLBACK_EXPIRED,
    ControlInboxHandlers,
    RefusalLimiter,
)
from assistant.telegram.gateway import OutboundGateway
from assistant.telegram.ingress.writer import write_batch
from assistant.telegram.routing import route_update
from tests.fakes.clock import FakeClock, FakeSleep
from tests.fakes.sender import FakeTelegramSender

pytestmark = pytest.mark.integration

OWNER = 7000000001
STRANGER = 7000000009
BOT_ID = 8845134387
RESET = (
    "TRUNCATE app.accounts, app.inbound_updates, app.system_state CASCADE; "
    "TRUNCATE procrastinate.procrastinate_jobs CASCADE"
)


@pytest.fixture
async def setup(
    migrated_url: str,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], InboxProcessor, FakeTelegramSender]]:
    engine = build_engine(migrated_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(RESET)
    factory = build_session_factory(engine)
    clock = FakeClock(datetime.now(UTC))
    sender = FakeTelegramSender()
    gateway = OutboundGateway(sender, clock, sleep=FakeSleep())
    app = build_app(migrated_url)

    @app.task(name="agent.owner_request", queue="llm")
    async def owner_request(turn_id: int) -> None:  # never run here; only deferred
        return None

    handlers = ControlInboxHandlers(
        ControlDispatcher(
            gateway,
            clock,
            ProcrastinateScheduler(app),
            policy_url="https://telegra.ph/synthetic-policy",
        ),
        gateway,
        RefusalLimiter(clock),
    )
    processor = InboxProcessor(
        factory,
        clock,
        owner_ids=frozenset({OWNER}),
        bot_id=BOT_ID,
        router=functools.partial(route_update, owner_ids=frozenset({OWNER}), bot_id=BOT_ID),
        handlers={Route.CONTROL: handlers.control, Route.REFUSED: handlers.refused},
    )
    async with app.open_async():
        yield factory, processor, sender
    async with engine.begin() as conn:
        await conn.exec_driver_sql(RESET)
    await engine.dispose()


_next_update = iter(range(900_000, 999_999))


def private_message(sender_id: int, message_text: str) -> dict[str, Any]:
    update_id = next(_next_update)
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id % 1000,
            "date": 1792354000,
            "chat": {"id": sender_id, "type": "private"},
            "from": {"id": sender_id, "is_bot": False, "first_name": "Synthetic"},
            "text": message_text,
        },
    }


async def deliver(
    factory: async_sessionmaker[AsyncSession], processor: InboxProcessor, *updates: dict[str, Any]
) -> None:
    async with transaction(factory) as uow:
        await write_batch(uow.session, bot_id=BOT_ID, updates=list(updates))
    await processor.process_batch()


async def choose_language(
    factory: async_sessionmaker[AsyncSession], processor: InboxProcessor, op: str = "en"
) -> None:
    """Tap `op` on the onboarding language card (M03 fix round C4)."""
    async with factory() as session:
        card = (
            await session.execute(
                select(PendingInput)
                .where(PendingInput.kind == "onb_lang")
                .where(PendingInput.token.is_not(None))
            )
        ).scalar_one()
    await deliver(
        factory,
        processor,
        {
            "update_id": next(_next_update),
            "callback_query": {
                "id": "cbq-synthetic-language",
                "from": {"id": OWNER, "is_bot": False, "first_name": "Synthetic"},
                "chat_instance": "1",
                "data": f"onb_lang:{card.token}:{op}",
                "message": {
                    "message_id": card.prompt_message_id,
                    "date": 1792354000,
                    "chat": {"id": OWNER, "type": "private"},
                },
            },
        },
    )


async def test_an_owner_command_is_answered_in_the_control_chat(setup: Any) -> None:
    factory, processor, sender = setup

    await deliver(factory, processor, private_message(OWNER, "/start"))
    await deliver(factory, processor, private_message(OWNER, "/help"))

    assert len(sender.sends) == 2
    assert "What I can do" in sender.sends[1]["text"]
    assert all(send["business_connection_id"] is None for send in sender.sends)
    assert all(send["chat_id"] == OWNER for send in sender.sends)


async def test_owner_free_text_becomes_a_turn_and_a_queued_job(setup: Any) -> None:
    factory, processor, sender = setup
    await deliver(factory, processor, private_message(OWNER, "/start"))
    await choose_language(factory, processor)
    # The next plain message answers the consent question (S01.3 precedence).
    await deliver(factory, processor, private_message(OWNER, "yes"))
    sends_before = len(sender.sends)

    await deliver(factory, processor, private_message(OWNER, "What is on today?"))

    async with factory() as session:
        turn = (await session.execute(select(ControlTurn))).scalar_one()
        job_args = (
            await session.execute(
                text(
                    "SELECT args FROM procrastinate.procrastinate_jobs "
                    "WHERE task_name = 'agent.owner_request'"
                )
            )
        ).scalar_one()
    assert turn.text == "What is on today?"
    assert job_args == {"turn_id": turn.id}
    # Nothing is sent for the free text: the agent answers later, from the job.
    assert len(sender.sends) == sends_before


async def test_a_stranger_gets_one_refusal_and_leaves_no_trace(setup: Any) -> None:
    factory, processor, sender = setup

    await deliver(
        factory,
        processor,
        private_message(STRANGER, "Synthetic hello"),
        private_message(STRANGER, "Synthetic hello again"),
    )

    assert [send["text"] for send in sender.sends] == [REFUSAL_TEXT]
    async with factory() as session:
        assert (await session.execute(select(Account))).first() is None
        assert (await session.execute(select(ControlTurn))).first() is None
        rows = (await session.execute(select(InboundUpdate))).scalars().all()
    assert [(row.status, row.payload) for row in rows] == [("rejected", None)] * 2
    # Regression (found in the live refusal check): Python None round-trips
    # from a JSON `null` too, so check SQL NULL in SQL — "no content" must mean
    # NULL, which `IS NULL` queries such as the retention sweep rely on.
    async with factory() as session:
        sql_nulls = (
            await session.execute(
                text("SELECT count(*) FROM app.inbound_updates WHERE payload IS NULL")
            )
        ).scalar_one()
    assert sql_nulls == 2


async def test_a_button_press_is_always_answered(setup: Any) -> None:
    factory, processor, sender = setup
    update_id = next(_next_update)

    await deliver(
        factory,
        processor,
        {
            "update_id": update_id,
            "callback_query": {
                "id": "cbq-synthetic-1",
                "from": {"id": OWNER, "is_bot": False, "first_name": "Synthetic"},
                "chat_instance": "1",
                "data": "act:abcdefghij:approve",
            },
        },
    )

    assert sender.answered_callbacks[0]["id"] == "cbq-synthetic-1"
    assert sender.answered_callbacks[0]["text"] == CALLBACK_EXPIRED


async def test_a_business_message_from_the_owner_never_reaches_the_control_chat(
    setup: Any,
) -> None:
    factory, processor, sender = setup
    update_id = next(_next_update)

    await deliver(
        factory,
        processor,
        {
            "update_id": update_id,
            "business_message": {
                "message_id": 1,
                "date": 1792354000,
                "business_connection_id": "conn-synthetic",
                "chat": {"id": 7000000002, "type": "private"},
                "from": {"id": OWNER, "is_bot": False, "first_name": "Synthetic"},
                "text": "/help",
            },
        },
    )

    assert sender.sends == []
    async with factory() as session:
        assert (await session.execute(select(ControlTurn))).first() is None


async def test_privacy_revoke_withdraws_consent_at_once(setup: Any) -> None:
    """FR-PRV-02: consent is revocable, and revocation is immediate."""
    factory, processor, sender = setup
    await deliver(factory, processor, private_message(OWNER, "/start"))
    await choose_language(factory, processor)
    await deliver(factory, processor, private_message(OWNER, "yes"))

    await deliver(factory, processor, private_message(OWNER, "/privacy"))
    await deliver(factory, processor, private_message(OWNER, "/privacy revoke"))

    privacy_text, revoke_text = sender.sends[-2]["text"], sender.sends[-1]["text"]
    assert "/privacy revoke" in privacy_text
    assert "https://telegra.ph/synthetic-policy" in privacy_text
    assert "AI consent revoked" in revoke_text
    async with factory() as session:
        account = (await session.execute(select(Account))).scalar_one()
        from assistant.accounts.service import active_consent

        assert (await active_consent(session, account.id, kind="ai_control")).granted is False
