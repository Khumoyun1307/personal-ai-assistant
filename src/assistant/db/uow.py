"""Unit of work: explicit transaction boundaries (AGENTS §10).

Every write happens inside one, and the boundary is visible in the code rather
than implied by autocommit. Transactions stay short — never held across an LLM
or Telegram call (AGENTS §13).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.observability.logging import get_logger

logger = get_logger(__name__)

#: Work that must run only once the transaction has committed — typically a
#: Telegram call that reports a state change (AGENTS §13: never hold a row
#: lock across a network call; a rolled-back transaction must not have told
#: anyone anything). It gets a session factory for its own short follow-up
#: transactions.
AfterCommit = Callable[[async_sessionmaker[AsyncSession]], Awaitable[None]]

_AFTER_COMMIT_KEY = "assistant.after_commit"


def after_commit(session: AsyncSession, action: AfterCommit) -> None:
    """Queue `action` to run after `session`'s transaction commits.

    Discarded on rollback. Only transactions run by `UnitOfWork` or the inbox
    processor execute these; anywhere else they would silently never run, so
    callers must be on one of those paths.
    """
    session.info.setdefault(_AFTER_COMMIT_KEY, []).append(action)


def discard_after_commit(session: AsyncSession) -> None:
    """Drop queued actions (the transaction, or the part that queued them,
    was rolled back)."""
    session.info.pop(_AFTER_COMMIT_KEY, None)


async def run_after_commit(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Run the actions queued on `session`, in order, after its commit.

    Each runs at most once: a failure is logged (type only, §14.4) and the
    rest still run; nothing is retried, because the committed state must not
    be re-announced twice (§6 invariant 6 for control-chat cards).
    """
    actions: list[AfterCommit] = session.info.pop(_AFTER_COMMIT_KEY, [])
    for action in actions:
        try:
            await action(session_factory)
        except Exception as error:
            logger.error("after_commit_failed", exc_type=type(error).__name__)


class UnitOfWork:
    """One transaction. Commits on clean exit, rolls back on any exception."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork used outside its context manager")
        return self._session

    async def __aenter__(self) -> Self:
        self._session = self._session_factory()
        await self._session.begin()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self.session
        committed = False
        try:
            if exc_type is None:
                await session.commit()
                committed = True
            else:
                await session.rollback()
        finally:
            await session.close()
            self._session = None
            if not committed:
                discard_after_commit(session)
        if committed:
            await run_after_commit(session, self._session_factory)

    async def flush(self) -> None:
        """Send pending changes so server-generated ids become available."""
        await self.session.flush()

    async def raw_connection(self) -> Any:
        """The underlying psycopg connection, for transactional job defer.

        This is what lets a job be enqueued inside the same transaction as the
        domain write, so a rollback discards both (verified in M00's jobs
        spike, TQ-01). Used by the job scheduler adapter in S01.5.
        """
        connection = await self.session.connection()
        return (await connection.get_raw_connection()).driver_connection


@asynccontextmanager
async def transaction(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[UnitOfWork]:
    """Convenience wrapper: `async with transaction(factory) as uow:`."""
    async with UnitOfWork(session_factory) as uow:
        yield uow
