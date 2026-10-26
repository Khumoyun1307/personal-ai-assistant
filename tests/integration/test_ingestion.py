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
from datetime import UTC, datetime, timedelta

import psycopg.errors
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.conversations.models import CoverageGap
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
        await conn.exec_driver_sql("TRUNCATE app.inbound_updates, app.system_state CASCADE")
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

    assert (await requeue_failed(factory)).reset == 1
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


# ------------------------------------------- transient vs deterministic (fix)
# A unique or NOT NULL violation is a DBAPIError, and an earlier version treated
# every DBAPIError as transient: the row never burned an attempt and was retried
# forever, blocking the queue behind it. Only deadlocks, serialization failures
# and connection loss are transient (ARCHITECTURE §6.2).


async def _insert_state(session: AsyncSession, key: str) -> None:
    await session.execute(
        text("INSERT INTO app.system_state (key, value) VALUES (:k, '{}'::jsonb)"), {"k": key}
    )


async def _state_exists(factory: async_sessionmaker[AsyncSession], key: str) -> bool:
    async with factory() as session:
        found = await session.execute(
            text("SELECT 1 FROM app.system_state WHERE key = :k"), {"k": key}
        )
        return found.first() is not None


async def test_an_integrity_error_counts_as_an_attempt_and_dead_letters(factory) -> None:  # type: ignore[no-untyped-def]
    async def duplicate_key(session, _row, _decision) -> None:  # type: ignore[no-untyped-def]
        await _insert_state(session, "dup")
        await _insert_state(session, "dup")  # IntegrityError (unique violation)

    async with factory() as session, session.begin():
        await write_batch(session, bot_id=BOT_ID, updates=[make_update(80, chat_id=OWNER)])

    proc = processor(factory, handlers={Route.CONTROL: duplicate_key})
    for _ in range(3):
        await proc.process_batch()

    row = await fetch(factory, 80)
    assert row.status == "failed"
    assert row.attempts == 3
    assert row.error_code == "IntegrityError"
    # Nothing the handler wrote before failing survives.
    assert not await _state_exists(factory, "dup")


async def test_a_failed_handler_leaves_no_partial_writes(factory) -> None:  # type: ignore[no-untyped-def]
    async def write_then_explode(session, _row, _decision) -> None:  # type: ignore[no-untyped-def]
        await _insert_state(session, "partial")
        raise DeterministicError("after writing")

    async with factory() as session, session.begin():
        await write_batch(session, bot_id=BOT_ID, updates=[make_update(81, chat_id=OWNER)])

    await processor(factory, handlers={Route.CONTROL: write_then_explode}).process_batch()

    row = await fetch(factory, 81)
    assert row.status == "pending"
    assert row.attempts == 1
    assert not await _state_exists(factory, "partial")


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("stmt", {}, psycopg.errors.DeadlockDetected("deadlock detected")),
        DBAPIError("stmt", {}, psycopg.errors.SerializationFailure("could not serialize")),
    ],
    ids=["deadlock", "serialization_failure"],
)
async def test_a_deadlock_or_serialization_failure_is_still_transient(factory, error) -> None:  # type: ignore[no-untyped-def]
    async def contended(_session, _row, _decision) -> None:  # type: ignore[no-untyped-def]
        raise error

    async with factory() as session, session.begin():
        await write_batch(session, bot_id=BOT_ID, updates=[make_update(82, chat_id=OWNER)])

    with pytest.raises(type(error)):
        await processor(factory, handlers={Route.CONTROL: contended}).process_batch()

    row = await fetch(factory, 82)
    assert row.status == "pending"
    assert row.attempts == 0


async def test_the_dead_letter_hook_runs_once_when_the_row_fails(factory) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[int, Route]] = []

    async def explode(_session, _row, _decision) -> None:  # type: ignore[no-untyped-def]
        raise DeterministicError("poison")

    async def hook(session, row, decision) -> None:  # type: ignore[no-untyped-def]
        calls.append((row.update_id, decision.route))
        await _insert_state(session, "hooked")

    async with factory() as session, session.begin():
        await write_batch(session, bot_id=BOT_ID, updates=[make_update(83, chat_id=OWNER)])

    proc = processor(
        factory, handlers={Route.CONTROL: explode}, dead_letter_hooks={Route.CONTROL: hook}
    )
    for _ in range(4):
        await proc.process_batch()

    assert calls == [(83, Route.CONTROL)]
    assert (await fetch(factory, 83)).status == "failed"
    assert await _state_exists(factory, "hooked")


async def test_a_failing_dead_letter_hook_does_not_prevent_dead_lettering(factory) -> None:  # type: ignore[no-untyped-def]
    async def explode(_session, _row, _decision) -> None:  # type: ignore[no-untyped-def]
        raise DeterministicError("poison")

    async def bad_hook(session, _row, _decision) -> None:  # type: ignore[no-untyped-def]
        await _insert_state(session, "bad-hook")
        raise RuntimeError("hook bug")

    async with factory() as session, session.begin():
        await write_batch(session, bot_id=BOT_ID, updates=[make_update(84, chat_id=OWNER)])

    proc = processor(
        factory, handlers={Route.CONTROL: explode}, dead_letter_hooks={Route.CONTROL: bad_hook}
    )
    for _ in range(3):
        await proc.process_batch()

    row = await fetch(factory, 84)
    assert row.status == "failed"
    assert row.attempts == 3
    assert not await _state_exists(factory, "bad-hook")


# ------------------------------------------------------- outage coverage gaps
async def _active_account(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session, session.begin():
        await session.execute(text("TRUNCATE app.accounts CASCADE"))
        await session.execute(
            text(
                "INSERT INTO app.accounts (telegram_user_id, status, timezone) "
                "VALUES (7000000001, 'active', 'UTC')"
            )
        )


async def _gaps(factory: async_sessionmaker[AsyncSession]) -> list[CoverageGap]:
    async with factory() as session:
        return list((await session.execute(select(CoverageGap))).scalars().all())


async def test_an_outage_over_24h_records_a_possible_gap(factory) -> None:  # type: ignore[no-untyped-def]
    """§6.6: Telegram keeps updates ~24 h. A longer silence between two
    completed polls may have lost messages dated in [T0, T1 - 24 h + 10 min]."""
    await _active_account(factory)
    clock = FakeClock(datetime(2026, 10, 19, 6, 0, tzinfo=UTC))
    first = Poller(FakeUpdateSource(), factory, clock)
    await first.startup()
    await first.poll_once()
    t0 = clock.now()
    clock.advance(hours=30)

    second = Poller(FakeUpdateSource(), factory, clock)
    await second.startup()  # the startup heartbeat must not count as a poll
    await second.poll_once()
    await second.poll_once()  # only the first completed poll records the gap

    [gap] = await _gaps(factory)
    assert (gap.kind, gap.certainty) == ("outage", "possible")
    assert gap.at_risk_from == t0
    assert gap.at_risk_to == clock.now() - timedelta(hours=24) + timedelta(minutes=10)
    assert gap.conversation_id is None


async def test_a_short_outage_records_a_delayed_gap(factory) -> None:  # type: ignore[no-untyped-def]
    await _active_account(factory)
    clock = FakeClock(datetime(2026, 10, 19, 6, 0, tzinfo=UTC))
    first = Poller(FakeUpdateSource(), factory, clock)
    await first.startup()
    await first.poll_once()
    clock.advance(hours=2)

    second = Poller(FakeUpdateSource(), factory, clock)
    await second.startup()
    await second.poll_once()

    [gap] = await _gaps(factory)
    assert gap.certainty == "delayed"
    assert gap.at_risk_to == clock.now()


async def test_a_first_ever_poll_records_no_gap(factory) -> None:  # type: ignore[no-untyped-def]
    await _active_account(factory)
    poller = Poller(FakeUpdateSource(), factory, FakeClock())
    await poller.startup()
    await poller.poll_once()
    assert await _gaps(factory) == []
