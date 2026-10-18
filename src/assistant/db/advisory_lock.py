"""Singleton enforcement via PostgreSQL advisory locks (ARCHITECTURE §6.1).

Exactly one poller may call `getUpdates` for a token — a second one gets HTTP
409 and both make no progress. Exactly one inbox processor may run, because
global ordering depends on it.

The lock is held on a **dedicated, non-pooled connection**. A pooled connection
could be returned to the pool and handed to someone else, silently releasing
the lock while the holder believes it still has it. The holder re-checks on
every loop: losing the connection must stop the work immediately, not continue
unguarded.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

#: Distinct namespaces, so the poller and the processor never collide.
NAMESPACE_POLLER = 1
NAMESPACE_INBOX = 2


class AdvisoryLockLostError(RuntimeError):
    """The lock connection died. The caller must stop working at once."""


class AdvisoryLock:
    """A session-scoped advisory lock on its own connection."""

    def __init__(self, engine: AsyncEngine, namespace: int, key: int) -> None:
        self._engine = engine
        self._namespace = namespace
        self._key = key
        # The two-argument form takes int4, and a Telegram bot id does not fit
        # in int4. Fold the namespace and key into one bigint instead.
        self._lock_key = (namespace << 48) ^ (key & 0xFFFF_FFFF_FFFF)
        self._connection: AsyncConnection | None = None
        self._held = False

    @property
    def held(self) -> bool:
        return self._held

    async def acquire(self) -> bool:
        """Try to take the lock. False means someone else holds it."""
        connection = await self._engine.connect()
        # Autocommit: a session-level advisory lock must not be tied to a
        # transaction that might roll back and release it.
        connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
        result = await connection.execute(
            text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))"),
            {"key": self._lock_key},
        )
        acquired = bool(result.scalar_one())
        if not acquired:
            await connection.close()
            return False
        self._connection = connection
        self._held = True
        return True

    async def verify(self) -> None:
        """Confirm the lock connection is still alive.

        Called every loop. Raises `AdvisoryLockLostError` so the caller stops rather
        than continuing to poll or process without the singleton guarantee.
        """
        if not self._held or self._connection is None:
            raise AdvisoryLockLostError("advisory lock is not held")
        try:
            await self._connection.execute(text("SELECT 1"))
        except Exception as error:
            self._held = False
            raise AdvisoryLockLostError("advisory lock connection lost") from error

    async def release(self) -> None:
        if self._connection is not None:
            try:
                await self._connection.execute(
                    text("SELECT pg_advisory_unlock(CAST(:key AS bigint))"),
                    {"key": self._lock_key},
                )
            finally:
                await self._connection.close()
                self._connection = None
        self._held = False

    async def __aenter__(self) -> Self:
        if not await self.acquire():
            raise AdvisoryLockLostError(
                f"another process holds lock ({self._namespace}, {self._key})"
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.release()
