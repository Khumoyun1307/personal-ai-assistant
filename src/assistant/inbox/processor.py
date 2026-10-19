"""The inbox processor (ARCHITECTURE §6.2).

Claims `pending` rows in insertion order, one transaction per row. The two
things it must get right:

**Transient vs deterministic.** A network blip or a deadlock leaves the row
pending and does NOT count as an attempt — otherwise a Telegram outage would
dead-letter the backlog. Only deterministic errors (parse, validation, a bug)
count, and after three the row is dead-lettered so it cannot block the queue
behind it.

**Payload hygiene.** Rows routed to refusal or ignored get `payload = NULL` in
the same transaction that marks them — we have no basis to keep content from
someone who is not the owner (§6 invariant 12). A handler can reach the same
verdict later, once it has looked at the database (`Disposition`).

**No network inside the transaction.** A route may have a *preparer* that runs
before the processing transaction opens — the `getBusinessConnection` bootstrap
is one (§6.2, §6.4). It does its own short write; a `TransientError` from it
leaves the row pending without counting an attempt.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.core.clock import Clock
from assistant.core.errors import TransientError
from assistant.core.routing import (
    CLEAR_PAYLOAD_DISPOSITIONS,
    Disposition,
    Route,
    RouteDecision,
    Router,
)
from assistant.inbox.models import InboundUpdate
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

MAX_ATTEMPTS = 3

#: Backoff per attempt, in seconds (§6.2). Applied by the caller's scheduler.
RETRY_BACKOFF = (1, 5, 30, 120)


class DeterministicError(Exception):
    """A row that will never succeed as-is: parse failure, bad shape, a bug."""


@dataclass(slots=True)
class ProcessResult:
    """What one pass over the inbox did."""

    claimed: int = 0
    processed: int = 0
    ignored: int = 0
    rejected: int = 0
    deferred: int = 0
    dead_lettered: int = 0


#: A handler for a routed update. Returns a `Disposition` to override the
#: route's status (None = as routed); raises to signal failure.
Handler = Callable[[AsyncSession, InboundUpdate, RouteDecision], Awaitable[Disposition | None]]

#: Runs before the processing transaction, for work that needs the network.
#: Gets the raw update and the routing decision; writes in its own transaction.
Preparer = Callable[[dict[str, Any], RouteDecision], Awaitable[None]]


async def _noop_handler(
    _session: AsyncSession, _row: InboundUpdate, _decision: RouteDecision
) -> Disposition | None:
    """Default handler: record the routing and do nothing else.

    M01 only needs updates captured and classified. Control-chat handling
    arrives in S01.3 and the business pipeline in M02; until then a correctly
    routed update that does nothing is the right behaviour, not a gap.
    """
    return None


class InboxProcessor:
    """Drains the durable inbox. Singleton — see `db.advisory_lock`."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        *,
        owner_ids: frozenset[int],
        bot_id: int,
        router: Router,
        handlers: dict[Route, Handler] | None = None,
        preparers: dict[Route, Preparer] | None = None,
        batch_size: int = 20,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._owner_ids = owner_ids
        self._bot_id = bot_id
        # Injected by bootstrap: the domain must not import the transport
        # adapter that knows what a Telegram update looks like (§5.2).
        self._router = router
        self._handlers = handlers or {}
        self._preparers = preparers or {}
        self._batch_size = batch_size

    async def _claim(self, session: AsyncSession) -> list[InboundUpdate]:
        """Claim pending rows in order, skipping any another worker holds."""
        statement = (
            select(InboundUpdate)
            .where(InboundUpdate.status == "pending")
            .order_by(InboundUpdate.id)
            .limit(self._batch_size)
            .with_for_update(skip_locked=True)
        )
        result = await session.execute(statement)
        return list(result.scalars().all())

    async def process_batch(self) -> ProcessResult:
        """Process up to `batch_size` rows, each in its own transaction."""
        result = ProcessResult()

        async with self._session_factory() as session, session.begin():
            rows = await self._claim(session)
            row_ids = [row.id for row in rows]
        result.claimed = len(row_ids)

        for row_id in row_ids:
            outcome = await self._process_one(row_id)
            match outcome:
                case "processed":
                    result.processed += 1
                case "ignored":
                    result.ignored += 1
                case "rejected":
                    result.rejected += 1
                case "failed":
                    result.dead_lettered += 1
                case "deferred":
                    result.deferred += 1
        return result

    async def _prepare(self, row_id: int) -> None:
        """Run the route's preparer, if any, outside the processing transaction."""
        if not self._preparers:
            return
        async with self._session_factory() as session:
            row = await session.get(InboundUpdate, row_id)
            if row is None or row.status != "pending":
                return
            payload = row.payload or {}
        decision = self._router(payload)
        preparer = self._preparers.get(decision.route)
        if preparer is not None:
            await preparer(payload, decision)

    async def _process_one(self, row_id: int) -> str:
        try:
            await self._prepare(row_id)
        except (OperationalError, DBAPIError, TransientError) as error:
            logger.warning(
                "inbox_transient_error", update_row_id=row_id, exc_type=type(error).__name__
            )
            raise
        except Exception as error:
            async with self._session_factory() as session, session.begin():
                row = await session.get(InboundUpdate, row_id, with_for_update=True)
                if row is None or row.status != "pending":
                    return "deferred"
                return await self._record_failure(session, row, type(error).__name__)

        async with self._session_factory() as session, session.begin():
            row = await session.get(InboundUpdate, row_id, with_for_update=True)
            if row is None or row.status != "pending":
                return "deferred"

            payload = row.payload or {}
            try:
                decision = self._router(payload)
                handler = self._handlers.get(decision.route, _noop_handler)
                disposition = await handler(session, row, decision)
            except (OperationalError, DBAPIError, TransientError) as error:
                # Transient by definition: deadlock, serialization failure,
                # connection loss. Leave pending, do NOT burn an attempt —
                # otherwise an outage dead-letters the whole backlog.
                logger.warning(
                    "inbox_transient_error",
                    update_row_id=row_id,
                    exc_type=type(error).__name__,
                )
                raise
            except DeterministicError as error:
                return await self._record_failure(session, row, type(error).__name__)
            except Exception as error:
                return await self._record_failure(session, row, type(error).__name__)

            status = (
                disposition.value if disposition is not None else self._status_for(decision.route)
            )
            row.status = status
            row.processed_at = self._clock.now()
            if decision.clears_payload or disposition in CLEAR_PAYLOAD_DISPOSITIONS:
                # Same transaction as the status change: a crash between the
                # two would leave content we have no basis to hold.
                row.payload = None
            return status

    @staticmethod
    def _status_for(route: Route) -> str:
        match route:
            case Route.REFUSED:
                return "rejected"
            case Route.IGNORED:
                return "ignored"
            case _:
                return "processed"

    async def _record_failure(
        self, session: AsyncSession, row: InboundUpdate, error_code: str
    ) -> str:
        """Count a deterministic failure; dead-letter on the third."""
        row.attempts += 1
        row.error_code = error_code
        if row.attempts >= MAX_ATTEMPTS:
            row.status = "failed"
            row.processed_at = self._clock.now()
            logger.error(
                "inbox_dead_letter",
                update_row_id=row.id,
                attempts=row.attempts,
                error_code=error_code,
                detail="row dead-lettered; processing continues",
            )
            return "failed"
        logger.warning(
            "inbox_attempt_failed",
            update_row_id=row.id,
            attempts=row.attempts,
            error_code=error_code,
        )
        return "deferred"


async def requeue_failed(
    session_factory: async_sessionmaker[AsyncSession], *, row_id: int | None = None
) -> int:
    """Reset dead-lettered rows to pending (`inbox requeue`, §6.2).

    The runbook is: fix the bug, deploy, requeue. Returns rows reset.
    """
    async with session_factory() as session, session.begin():
        statement = (
            update(InboundUpdate)
            .where(InboundUpdate.status == "failed")
            .values(status="pending", attempts=0, error_code=None, processed_at=None)
        )
        if row_id is not None:
            statement = statement.where(InboundUpdate.id == row_id)
        result = await session.execute(statement)
        # CursorResult carries rowcount; the base Result type does not expose it.
        return int(getattr(result, "rowcount", 0) or 0)


async def inbox_backlog(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """Counts per status, for `/status` and the ingestion watchdog."""
    from sqlalchemy import func

    async with session_factory() as session:
        result = await session.execute(
            select(InboundUpdate.status, func.count()).group_by(InboundUpdate.status)
        )
        return dict(result.all())
