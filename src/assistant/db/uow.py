"""Unit of work: explicit transaction boundaries (AGENTS §10).

Every write happens inside one, and the boundary is visible in the code rather
than implied by autocommit. Transactions stay short — never held across an LLM
or Telegram call (AGENTS §13).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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
        try:
            if exc_type is None:
                await session.commit()
            else:
                await session.rollback()
        finally:
            await session.close()
            self._session = None

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
