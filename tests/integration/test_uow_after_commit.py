"""`UnitOfWork` after-commit actions (ARCHITECTURE §6.7, AGENTS §13).

A queued action is a Telegram call that reports a state change: it must run
only once the change is committed, never for a rolled-back transaction, and
one failing action must neither stop the others nor be retried.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.db import uow as uow_module
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.models import SystemState
from assistant.db.uow import after_commit, transaction

pytestmark = pytest.mark.integration

KEY = "test.uow_after_commit"


@pytest.fixture
async def factory(migrated_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_url)
    built = build_session_factory(engine)
    try:
        yield built
    finally:
        async with built() as session, session.begin():
            await session.execute(delete(SystemState).where(SystemState.key == KEY))
        await engine.dispose()


async def _committed(factory: async_sessionmaker[AsyncSession]) -> bool:
    async with factory() as session:
        return (await session.get(SystemState, KEY)) is not None


class _RecordingLogger:
    def __init__(self) -> None:
        self.errors: list[tuple[str, dict[str, Any]]] = []

    def error(self, event: str, **fields: Any) -> None:
        self.errors.append((event, fields))


async def test_actions_run_only_after_the_commit(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    seen: list[bool] = []

    async def action(follow_up: async_sessionmaker[AsyncSession]) -> None:
        # The write is visible to another session: the commit has happened.
        seen.append(await _committed(follow_up))

    async with transaction(factory) as uow:
        uow.session.add(SystemState(key=KEY, value={}))
        after_commit(uow.session, action)
        await uow.flush()
        assert seen == []  # queued, not run, while the transaction is open

    assert seen == [True]


async def test_a_rollback_discards_queued_actions(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ran: list[str] = []

    async def action(_: async_sessionmaker[AsyncSession]) -> None:
        ran.append("ran")

    async def failing_transaction() -> None:
        async with transaction(factory) as uow:
            uow.session.add(SystemState(key=KEY, value={}))
            after_commit(uow.session, action)
            raise RuntimeError("synthetic failure")

    with pytest.raises(RuntimeError):
        await failing_transaction()

    assert ran == []
    assert not await _committed(factory)

    # The next transaction on the same factory starts with an empty queue.
    async with transaction(factory) as uow:
        await uow.session.execute(select(SystemState.key).limit(1))
    assert ran == []


async def test_a_failing_action_is_logged_by_type_and_later_actions_still_run(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _RecordingLogger()
    monkeypatch.setattr(uow_module, "logger", recorder)
    calls: list[str] = []

    async def first(_: async_sessionmaker[AsyncSession]) -> None:
        calls.append("first")
        raise ValueError("synthetic text that must not be logged")

    async def second(_: async_sessionmaker[AsyncSession]) -> None:
        calls.append("second")

    async with transaction(factory) as uow:
        uow.session.add(SystemState(key=KEY, value={}))
        after_commit(uow.session, first)
        after_commit(uow.session, second)

    assert calls == ["first", "second"]  # each once, in order; nothing retried
    assert recorder.errors == [("after_commit_failed", {"exc_type": "ValueError"})]
    assert await _committed(factory)  # the commit stands
