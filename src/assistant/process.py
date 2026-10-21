"""The running process: roles and their liveness (ARCHITECTURE §4.1, §4.3).

`python -m assistant run` runs these roles under `core.supervisor.Supervisor`:

- **poller** — `getUpdates` → durable inbox. Healthy while a poll succeeded in
  the last 3 minutes. Network/5xx errors back off and retry; a 409 conflict or
  a lost advisory lock ends the role (and so the process).
- **inbox** — routes stored updates and runs fast handlers. Healthy while it
  has made progress (or found nothing to do) in the last 2 minutes.
- **worker:<queue>** — one procrastinate worker per queue. Healthy while
  procrastinate's worker heartbeat is fresh.

Objects are built by `bootstrap.py`; this module only runs them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.core.clock import Clock
from assistant.core.errors import TransientError
from assistant.core.supervisor import Role
from assistant.db.advisory_lock import AdvisoryLock
from assistant.inbox.processor import InboxProcessor
from assistant.observability.logging import get_logger
from assistant.telegram.ingress.poller import Poller
from assistant.telegram.ingress.ports import TelegramTransientError

logger = get_logger(__name__)

POLLER_STALE_AFTER = timedelta(minutes=3)
INBOX_STALE_AFTER = timedelta(minutes=2)
WORKER_HEARTBEAT_STALE_SECONDS = 60
INBOX_IDLE_SLEEP = 0.5
BACKOFF_SECONDS = (1, 2, 5, 10, 30)


class _Cancellable:
    """Runs a coroutine as an inner task so `stop()` can cancel it cleanly."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None

    async def run_inner(self, coro: Awaitable[None]) -> None:
        self._task = asyncio.ensure_future(coro)
        try:
            await self._task
        except asyncio.CancelledError:
            if self._task.cancelled():
                return  # stopped on request: a clean return
            raise

    def cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()


class PollerRole(_Cancellable):
    """Long-polls under the poller advisory lock. Started (`startup()`) by bootstrap."""

    def __init__(self, poller: Poller, lock: AdvisoryLock, clock: Clock) -> None:
        super().__init__()
        self._poller = poller
        self._lock = lock
        self._clock = clock
        self.last_success_at: datetime = clock.now()

    async def run(self) -> None:
        await self.run_inner(self._loop())

    async def _loop(self) -> None:
        failures = 0
        while True:
            await self._lock.verify()  # AdvisoryLockLostError ends the role
            try:
                await self._poller.poll_once()
            except TelegramTransientError as error:
                failures += 1
                delay = BACKOFF_SECONDS[min(failures, len(BACKOFF_SECONDS)) - 1]
                logger.warning("poller_transient", exc_type=str(error), retry_in_s=delay)
                await asyncio.sleep(delay)
                continue
            failures = 0
            self.last_success_at = self._clock.now()

    def stop(self) -> None:
        # Cancelling mid-poll is safe: the offset only advances after commit.
        self.cancel()

    async def liveness(self) -> str | None:
        if self._clock.now() - self.last_success_at > POLLER_STALE_AFTER:
            return "no successful getUpdates for 3 minutes"
        return None

    def role(self) -> Role:
        return Role("poller", self.run, self.stop, self.liveness, grace_seconds=5)


class InboxRole:
    """Drains the durable inbox under the inbox advisory lock."""

    def __init__(self, processor: InboxProcessor, lock: AdvisoryLock, clock: Clock) -> None:
        self._processor = processor
        self._lock = lock
        self._clock = clock
        self._stopping = asyncio.Event()
        self.last_progress_at: datetime = clock.now()

    async def run(self) -> None:
        failures = 0
        while not self._stopping.is_set():
            await self._lock.verify()
            try:
                result = await self._processor.process_batch()
            except (OperationalError, DBAPIError, TransientError) as error:
                failures += 1
                delay = BACKOFF_SECONDS[min(failures, len(BACKOFF_SECONDS)) - 1]
                logger.warning("inbox_transient", exc_type=type(error).__name__, retry_in_s=delay)
                await self._sleep(delay)
                continue
            failures = 0
            moved = result.processed + result.ignored + result.rejected + result.dead_lettered
            if result.claimed == 0 or moved > 0:
                self.last_progress_at = self._clock.now()
            if result.claimed == 0:
                await self._sleep(INBOX_IDLE_SLEEP)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    def stop(self) -> None:
        # Finish the row in hand, then return. Pending rows wait in Postgres.
        self._stopping.set()

    async def liveness(self) -> str | None:
        if self._clock.now() - self.last_progress_at > INBOX_STALE_AFTER:
            return "no inbox progress for 2 minutes"
        return None

    def role(self) -> Role:
        return Role("inbox", self.run, self.stop, self.liveness, grace_seconds=30)


class WorkerRole(_Cancellable):
    """One procrastinate worker. Cancelling it triggers procrastinate's graceful stop."""

    def __init__(
        self,
        name: str,
        start: Callable[[], Awaitable[None]],
        heartbeat_probe: Callable[[], Awaitable[str | None]],
        grace_seconds: float,
    ) -> None:
        super().__init__()
        self._name = name
        self._start = start
        self._probe = heartbeat_probe
        self._grace = grace_seconds

    async def run(self) -> None:
        await self.run_inner(self._start())

    def stop(self) -> None:
        self.cancel()

    def role(self) -> Role:
        # A few seconds beyond procrastinate's own graceful timeout.
        return Role(
            f"worker:{self._name}", self.run, self.stop, self._probe, grace_seconds=self._grace + 5
        )


def worker_heartbeat_probe(
    session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[], Awaitable[str | None]]:
    """Fresh procrastinate worker heartbeat, measured by the database's own clock."""

    async def probe() -> str | None:
        async with session_factory() as session:
            age = (
                await session.execute(
                    text(
                        "SELECT EXTRACT(EPOCH FROM now() - max(last_heartbeat)) "
                        "FROM procrastinate.procrastinate_workers"
                    )
                )
            ).scalar_one_or_none()
        if age is None:
            return None  # not registered yet: the role task itself is the signal
        if float(age) > WORKER_HEARTBEAT_STALE_SECONDS:
            return f"worker heartbeat stale ({int(age)} s)"
        return None

    return probe
