"""Singleton enforcement (ARCHITECTURE §6.1).

Two pollers on one token both get HTTP 409 and neither makes progress; two
inbox processors break global ordering. The lock is what prevents a second
process from starting at all.
"""

from __future__ import annotations

import pytest

from assistant.db.advisory_lock import (
    NAMESPACE_INBOX,
    NAMESPACE_POLLER,
    AdvisoryLock,
    AdvisoryLockLostError,
)
from assistant.db.engine import build_engine

pytestmark = pytest.mark.integration

BOT_ID = 8123456789


@pytest.fixture
async def engine(migrated_url: str):  # type: ignore[no-untyped-def]
    engine = build_engine(migrated_url)
    yield engine
    await engine.dispose()


async def test_a_second_holder_cannot_acquire(engine) -> None:  # type: ignore[no-untyped-def]
    first = AdvisoryLock(engine, NAMESPACE_POLLER, BOT_ID)
    second = AdvisoryLock(engine, NAMESPACE_POLLER, BOT_ID)
    try:
        assert await first.acquire() is True
        assert await second.acquire() is False
        assert second.held is False
    finally:
        await first.release()


async def test_the_lock_is_reusable_after_release(engine) -> None:  # type: ignore[no-untyped-def]
    first = AdvisoryLock(engine, NAMESPACE_POLLER, BOT_ID)
    assert await first.acquire() is True
    await first.release()

    second = AdvisoryLock(engine, NAMESPACE_POLLER, BOT_ID)
    try:
        assert await second.acquire() is True
    finally:
        await second.release()


async def test_poller_and_inbox_locks_do_not_collide(engine) -> None:  # type: ignore[no-untyped-def]
    """Different roles, same bot id — both must be able to run."""
    poller = AdvisoryLock(engine, NAMESPACE_POLLER, BOT_ID)
    inbox = AdvisoryLock(engine, NAMESPACE_INBOX, BOT_ID)
    try:
        assert await poller.acquire() is True
        assert await inbox.acquire() is True
    finally:
        await poller.release()
        await inbox.release()


async def test_verify_raises_once_the_lock_is_gone(engine) -> None:  # type: ignore[no-untyped-def]
    """Losing the connection must stop the work, not continue unguarded."""
    lock = AdvisoryLock(engine, NAMESPACE_POLLER, BOT_ID)
    await lock.acquire()
    await lock.verify()

    await lock.release()
    with pytest.raises(AdvisoryLockLostError):
        await lock.verify()


async def test_context_manager_refuses_to_start_when_held(engine) -> None:  # type: ignore[no-untyped-def]
    holder = AdvisoryLock(engine, NAMESPACE_INBOX, BOT_ID)
    await holder.acquire()
    try:
        with pytest.raises(AdvisoryLockLostError):
            async with AdvisoryLock(engine, NAMESPACE_INBOX, BOT_ID):
                pass
    finally:
        await holder.release()
