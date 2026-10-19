"""Durable ingestion against a real PostgreSQL (ARCHITECTURE §6.1, §6.2).

Named regression suite. The claims being pinned:

- an update is never lost when the process dies mid-batch;
- a redelivered update is a no-op, not a duplicate;
- the offset advances only after the batch commits;
- a transient failure does not burn a retry attempt;
- a poison row is dead-lettered and does not block the queue behind it;
- payloads for non-owners are cleared in the same transaction.
"""

from __future__ import annotations

import functools

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.core.routing import Route
from assistant.db.engine import build_engine, build_session_factory
from assistant.inbox.models import InboundUpdate
from assistant.inbox.processor import (
    DeterministicError,
    InboxProcessor,
    inbox_backlog,
    requeue_failed,
)
from assistant.telegram.ingress.poller import Poller
from assistant.telegram.ingress.writer import write_batch
from assistant.telegram.routing import route_update
from tests.fakes.clock import FakeClock
from tests.fakes.telegram import BOT_ID, FakeUpdateSource, make_update

pytestmark = pytest.mark.integration

OWNER = 584113992
STRANGER = 999000111
OWNERS = frozenset({OWNER})


@pytest.fixture
async def factory(migrated_url: str):  # type: ignore[no-untyped-def]
    engine = build_engine(migrated_url)
    session_factory = build_session_factory(engine)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("TRUNCATE app.inbound_updates, app.system_state")
    yield session_factory
    await engine.dispose()


async def count_rows(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return int(
            (await session.execute(select(func.count()).select_from(InboundUpdate))).scalar_one()
        )


async def fetch(factory: async_sessionmaker[AsyncSession], update_id: int) -> InboundUpdate:
    async with factory() as session:
        row = await session.execute(
            select(InboundUpdate).where(InboundUpdate.update_id == update_id)
        )
        return row.scalar_one()


# --------------------------------------------------------------- the writer
async def test_a_redelivered_update_is_a_no_op(factory) -> None:  # type: ignore[no-untyped-def]
    """Telegram replays anything unconfirmed; duplicates must be harmless."""
    batch = [make_update(1, chat_id=OWNER), make_update(2, chat_id=OWNER)]

    async with factory() as session, session.begin():
        assert await write_batch(session, bot_id=BOT_ID, updates=batch) == 2
    async with factory() as session, session.begin():
        assert await write_batch(session, bot_id=BOT_ID, updates=batch) == 0

    assert await count_rows(factory) == 2


async def test_the_same_update_id_from_a_different_bot_is_kept(factory) -> None:  # type: ignore[no-untyped-def]
    """Dev and prod bots number updates independently."""
    async with factory() as session, session.begin():
        await write_batch(session, bot_id=BOT_ID, updates=[make_update(1, chat_id=OWNER)])
    async with factory() as session, session.begin():
        await write_batch(session, bot_id=BOT_ID + 1, updates=[make_update(1, chat_id=OWNER)])
    assert await count_rows(factory) == 2


# --------------------------------------------------------------- the poller
async def test_offset_advances_only_after_the_batch_commits(factory) -> None:  # type: ignore[no-untyped-def]
    source = FakeUpdateSource([[make_update(10, chat_id=OWNER), make_update(11, chat_id=OWNER)]])
    poller = Poller(source, factory, FakeClock())
    await poller.startup()

    # First call must omit the offset so Telegram replays its oldest unconfirmed.
    assert await poller.poll_once() == 2
    assert source.calls[0]["offset"] is None
    assert poller.state.offset == 12

    await poller.poll_once()
    assert source.calls[1]["offset"] == 12


async def test_a_crash_before_commit_loses_nothing(factory) -> None:  # type: ignore[no-untyped-def]
    """The durability claim, exercised rather than asserted.

    The batch is written and the transaction rolled back, standing in for a
    crash. Because the offset was never advanced, Telegram would resend — and
    the replay must land everything exactly once.
    """
    batch = [make_update(20, chat_id=OWNER), make_update(21, chat_id=OWNER)]

    async with factory() as session:
        await session.begin()
        await write_batch(session, bot_id=BOT_ID, updates=batch)
        await session.rollback()  # the "crash"

    assert await count_rows(factory) == 0

    source = FakeUpdateSource([batch])
    poller = Poller(source, factory, FakeClock())
    await poller.startup()
    assert await poller.poll_once() == 2
    assert await count_rows(factory) == 2


async def test_startup_refuses_a_database_belonging_to_another_bot(factory) -> None:  # type: ignore[no-untyped-def]
    """A dev token on the prod database would cross tenants."""
    from assistant.telegram.ingress.poller import BotIdentityMismatchError

    await Poller(FakeUpdateSource(bot_id=BOT_ID), factory, FakeClock()).startup()

    other = Poller(FakeUpdateSource(bot_id=BOT_ID + 99), factory, FakeClock())
    with pytest.raises(BotIdentityMismatchError):
        await other.startup()


async def test_startup_refreshes_the_heartbeat_so_a_restart_is_not_a_stall(factory) -> None:  # type: ignore[no-untyped-def]
    """Regression (found in the S01.5 live run): the watchdog read the previous
    run's heartbeat while the first long poll was still open and alerted the
    owner about a stall that was really a restart."""
    from datetime import timedelta

    from assistant.inbox.watchdog import STALL_AFTER, poller_heartbeat_at

    clock = FakeClock()
    await Poller(FakeUpdateSource(), factory, clock).startup()
    clock.advance(minutes=10)  # the process was down for ten minutes

    await Poller(FakeUpdateSource(), factory, clock).startup()

    async with factory() as session:
        beat = await poller_heartbeat_at(session)
    assert beat is not None
    assert clock.now() - beat < STALL_AFTER
    assert clock.now() - beat == timedelta(0)


async def test_startup_never_drops_pending_updates(factory) -> None:  # type: ignore[no-untyped-def]
    """drop_pending_updates=True would discard up to 24 h of messages."""
    source = FakeUpdateSource()
    await Poller(source, factory, FakeClock()).startup()
    assert source.delete_webhook_calls == 1


async def test_allowed_updates_is_sent_explicitly_every_call(factory) -> None:  # type: ignore[no-untyped-def]
    """The setting is sticky server-side; omitting it silently loses kinds."""
    source = FakeUpdateSource([[make_update(30, chat_id=OWNER)]])
    poller = Poller(source, factory, FakeClock())
    await poller.startup()
    await poller.poll_once()
    await poller.poll_once()
    for call in source.calls:
        assert "business_message" in call["allowed"]
        assert "deleted_business_messages" in call["allowed"]


async def test_conflict_stops_polling_rather_than_crash_looping(factory) -> None:  # type: ignore[no-untyped-def]
    from assistant.telegram.ingress.ports import TelegramConflictError

    source = FakeUpdateSource()
    source.raise_conflict_on = 0
    poller = Poller(source, factory, FakeClock())
    await poller.startup()
    with pytest.raises(TelegramConflictError):
        await poller.run_forever(stop_after_batches=5)
    assert len(source.calls) == 1


# ------------------------------------------------------------ the processor
def processor(factory, **kwargs):  # type: ignore[no-untyped-def]
    # The domain takes the router as a dependency; the Telegram parser is
    # bound here the way bootstrap will bind it (§5.2).
    router = functools.partial(route_update, owner_ids=OWNERS, bot_id=BOT_ID)
    return InboxProcessor(
        factory, FakeClock(), owner_ids=OWNERS, bot_id=BOT_ID, router=router, **kwargs
    )


async def test_owner_updates_are_processed_and_keep_their_payload(factory) -> None:  # type: ignore[no-untyped-def]
    async with factory() as session, session.begin():
        await write_batch(session, bot_id=BOT_ID, updates=[make_update(40, chat_id=OWNER)])

    result = await processor(factory).process_batch()
    assert result.processed == 1

    row = await fetch(factory, 40)
    assert row.status == "processed"
    assert row.payload is not None


async def test_non_owner_payload_is_cleared_when_rejected(factory) -> None:  # type: ignore[no-untyped-def]
    """§6 invariant 12: no domain rows, no retained content."""
    async with factory() as session, session.begin():
        await write_batch(session, bot_id=BOT_ID, updates=[make_update(41, chat_id=STRANGER)])

    result = await processor(factory).process_batch()
    assert result.rejected == 1

    row = await fetch(factory, 41)
    assert row.status == "rejected"
    assert row.payload is None


async def test_group_payload_is_cleared_when_ignored(factory) -> None:  # type: ignore[no-untyped-def]
    update = make_update(42, chat_id=-100999, chat_type="supergroup", from_id=OWNER)
    async with factory() as session, session.begin():
        await write_batch(session, bot_id=BOT_ID, updates=[update])

    assert (await processor(factory).process_batch()).ignored == 1
    assert (await fetch(factory, 42)).payload is None


async def test_business_updates_are_captured_but_not_routed_to_control(factory) -> None:  # type: ignore[no-untyped-def]
    """M01 stores business traffic; M02 interprets it. It is never control."""
    seen: list[Route] = []

    async def record(_session, _row, decision) -> None:  # type: ignore[no-untyped-def]
        seen.append(decision.route)

    update = make_update(43, kind="business_message", chat_id=STRANGER, from_id=OWNER)
    async with factory() as session, session.begin():
        await write_batch(session, bot_id=BOT_ID, updates=[update])

    handlers = dict.fromkeys(Route, record)
    await processor(factory, handlers=handlers).process_batch()

    assert seen == [Route.BUSINESS]
    assert (await fetch(factory, 43)).payload is not None


async def test_a_poison_row_is_dead_lettered_after_three_attempts(factory) -> None:  # type: ignore[no-untyped-def]
    async def explode(_session, _row, _decision) -> None:  # type: ignore[no-untyped-def]
        raise DeterministicError("unparseable")

    async with factory() as session, session.begin():
        await write_batch(session, bot_id=BOT_ID, updates=[make_update(50, chat_id=OWNER)])

    proc = processor(factory, handlers={Route.CONTROL: explode})
    for _ in range(3):
        await proc.process_batch()

    row = await fetch(factory, 50)
    assert row.status == "failed"
    assert row.attempts == 3
    assert row.error_code == "DeterministicError"


async def test_a_poison_row_does_not_block_the_queue_behind_it(factory) -> None:  # type: ignore[no-untyped-def]
    """Head-of-line blocking here would stall every later message."""

    async def explode_on_first(_session, row, _decision) -> None:  # type: ignore[no-untyped-def]
        if row.update_id == 60:
            raise DeterministicError("poison")

    updates = [make_update(60, chat_id=OWNER), make_update(61, chat_id=OWNER)]
    async with factory() as session, session.begin():
        await write_batch(session, bot_id=BOT_ID, updates=updates)

    proc = processor(factory, handlers={Route.CONTROL: explode_on_first})
    for _ in range(3):
        await proc.process_batch()

    assert (await fetch(factory, 60)).status == "failed"
    assert (await fetch(factory, 61)).status == "processed"


async def test_requeue_resets_dead_lettered_rows(factory) -> None:  # type: ignore[no-untyped-def]
    async def explode(_session, _row, _decision) -> None:  # type: ignore[no-untyped-def]
        raise DeterministicError("poison")

    async with factory() as session, session.begin():
        await write_batch(session, bot_id=BOT_ID, updates=[make_update(70, chat_id=OWNER)])

    proc = processor(factory, handlers={Route.CONTROL: explode})
    for _ in range(3):
        await proc.process_batch()
    assert (await fetch(factory, 70)).status == "failed"

    assert await requeue_failed(factory) == 1
    row = await fetch(factory, 70)
    assert row.status == "pending"
    assert row.attempts == 0

    # ...and after the "fix", it goes through.
    assert (await processor(factory).process_batch()).processed == 1


async def test_backlog_counts_by_status(factory) -> None:  # type: ignore[no-untyped-def]
    async with factory() as session, session.begin():
        await write_batch(
            session,
            bot_id=BOT_ID,
            updates=[make_update(80, chat_id=OWNER), make_update(81, chat_id=STRANGER)],
        )
    assert (await inbox_backlog(factory))["pending"] == 2
    await processor(factory).process_batch()

    backlog = await inbox_backlog(factory)
    assert backlog.get("processed") == 1
    assert backlog.get("rejected") == 1
    assert "pending" not in backlog
