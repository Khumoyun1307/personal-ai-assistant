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

from sqlalchemy import func, select, update
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
from assistant.db.uow import discard_after_commit, run_after_commit
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

#: Runs in the dead-lettering transaction when a row goes `failed`, so the
#: domain can record the consequence (a coverage gap, a paused conversation —
#: §6.2). It gets its own savepoint: a hook bug must not keep a poison row
#: pending.
DeadLetterHook = Callable[[AsyncSession, InboundUpdate, RouteDecision], Awaitable[None]]

#: SQLSTATE classes that mean "try again later", not "this row is bad":
#: 08 connection exception, 40001 serialization failure, 40P01 deadlock,
#: 57P0x server shutdown / cannot connect now.
_TRANSIENT_SQLSTATES = frozenset({"40001", "40P01"})
_TRANSIENT_SQLSTATE_PREFIXES = ("08", "57P")


def _is_transient(error: BaseException) -> bool:
    """Transient = leave the row pending without counting an attempt.

    Only deadlocks, serialization failures and connection loss qualify
    (ARCHITECTURE §6.2). Every other database error — a unique or NOT NULL
    violation, a bad cast, a syntax error — is deterministic: it will recur on
    every retry, so it must burn an attempt and eventually dead-letter, or the
    row blocks the whole queue behind it.
    """
    if isinstance(error, TransientError | OperationalError):
        return True
    if isinstance(error, DBAPIError):
        if error.connection_invalidated:
            return True
        sqlstate = getattr(error.orig, "sqlstate", None)
        if isinstance(sqlstate, str):
            return sqlstate in _TRANSIENT_SQLSTATES or sqlstate.startswith(
                _TRANSIENT_SQLSTATE_PREFIXES
            )
    return False


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
        dead_letter_hooks: dict[Route, DeadLetterHook] | None = None,
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
        self._dead_letter_hooks = dead_letter_hooks or {}
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
        except Exception as error:
            if _is_transient(error):
                logger.warning(
                    "inbox_transient_error", update_row_id=row_id, exc_type=type(error).__name__
                )
                raise
            async with self._session_factory() as session, session.begin():
                row = await session.get(InboundUpdate, row_id, with_for_update=True)
                if row is None or row.status != "pending":
                    return "deferred"
                decision = self._router(row.payload or {})
                return await self._record_failure(session, row, decision, type(error).__name__)

        async with self._session_factory() as session:
            try:
                async with session.begin():
                    status = await self._process_locked(session, row_id)
            except BaseException:
                discard_after_commit(session)
                raise
            # Telegram calls a handler queued with `after_commit` (e.g. a card
            # press's edits) run only now: never under the row locks the
            # transaction held, and never for a rolled-back attempt that the
            # retry would announce a second time (AGENTS §13).
            await run_after_commit(session, self._session_factory)
            return status

    async def _process_locked(self, session: AsyncSession, row_id: int) -> str:
        row = await session.get(InboundUpdate, row_id, with_for_update=True)
        if row is None or row.status != "pending":
            return "deferred"

        payload = row.payload or {}
        decision = self._router(payload)
        handler = self._handlers.get(decision.route, _noop_handler)
        try:
            # A savepoint: whatever a failing handler wrote is rolled back
            # before the failure is counted, so a half-written row never
            # commits, and a SQL error does not abort the transaction the
            # attempt counter needs.
            async with session.begin_nested():
                disposition = await handler(session, row, decision)
        except Exception as error:
            # Whatever the failed attempt queued to announce is void.
            discard_after_commit(session)
            if _is_transient(error):
                # Leave pending, do NOT burn an attempt — otherwise an
                # outage dead-letters the whole backlog.
                logger.warning(
                    "inbox_transient_error",
                    update_row_id=row_id,
                    exc_type=type(error).__name__,
                )
                raise
            # The rollback expired `row`; reload it under the lock we hold
            # (an expired attribute cannot lazy-load in async code).
            await session.refresh(row)
            return await self._record_failure(session, row, decision, type(error).__name__)

        status = disposition.value if disposition is not None else self._status_for(decision.route)
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
        self, session: AsyncSession, row: InboundUpdate, decision: RouteDecision, error_code: str
    ) -> str:
        """Count a deterministic failure; dead-letter on the third."""
        row.attempts += 1
        row.error_code = error_code
        if row.attempts >= MAX_ATTEMPTS:
            await self._run_dead_letter_hook(session, row, decision)
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

    async def _run_dead_letter_hook(
        self, session: AsyncSession, row: InboundUpdate, decision: RouteDecision
    ) -> None:
        """Let the domain record the consequence of a dead letter (§6.2).

        Runs before the row is marked, in its own savepoint: if the hook
        itself fails, its writes are rolled back, the failure is logged, and
        the row is dead-lettered anyway — a hook bug must never resurrect a
        poison row.
        """
        hook = self._dead_letter_hooks.get(decision.route)
        if hook is None:
            return
        try:
            async with session.begin_nested():
                await hook(session, row, decision)
        except Exception as error:
            logger.error(
                "inbox_dead_letter_hook_failed",
                update_row_id=row.id,
                route=decision.route.value,
                exc_type=type(error).__name__,
            )
            await session.refresh(row)


@dataclass(frozen=True, slots=True)
class Requeued:
    """What `inbox requeue` did: rows reset to pending, and dead letters left
    failed because their payload is gone (swept, cleared by an owner's
    deletion, M03 D89, or failed in an Ignore chat, C3-2) — processing them
    would handle an empty update."""

    reset: int
    cleared: int


async def requeue_failed(
    session_factory: async_sessionmaker[AsyncSession], *, row_id: int | None = None
) -> Requeued:
    """Reset dead-lettered rows that still hold their payload to pending
    (`inbox requeue`, §6.2). The runbook is: fix the bug, deploy, requeue.
    """
    async with session_factory() as session, session.begin():
        statement = (
            update(InboundUpdate)
            .where(InboundUpdate.status == "failed")
            .where(InboundUpdate.payload.is_not(None))
            .values(status="pending", attempts=0, error_code=None, processed_at=None)
        )
        cleared = (
            select(func.count())
            .select_from(InboundUpdate)
            .where(InboundUpdate.status == "failed")
            .where(InboundUpdate.payload.is_(None))
        )
        if row_id is not None:
            statement = statement.where(InboundUpdate.id == row_id)
            cleared = cleared.where(InboundUpdate.id == row_id)
        result = await session.execute(statement)
        left = int((await session.execute(cleared)).scalar_one())
        # CursorResult carries rowcount; the base Result type does not expose it.
        reset = int(getattr(result, "rowcount", 0) or 0)
    if left:
        logger.warning("inbox_requeue_skipped_cleared", count=left)
    return Requeued(reset, left)


async def inbox_backlog(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """Counts per status, for `/status` and the ingestion watchdog."""
    async with session_factory() as session:
        result = await session.execute(
            select(InboundUpdate.status, func.count()).group_by(InboundUpdate.status)
        )
        return dict(result.all())


async def inbox_backlog_for(session: AsyncSession, account_id: int) -> dict[str, int]:
    """Counts per status for one account's rows plus the unrouted pending ones
    (a pending row has no account yet, and may be this owner's)."""
    from sqlalchemy import or_

    result = await session.execute(
        select(InboundUpdate.status, func.count())
        .where(
            or_(
                InboundUpdate.account_id == account_id,
                (InboundUpdate.account_id.is_(None)) & (InboundUpdate.status == "pending"),
            )
        )
        .group_by(InboundUpdate.status)
    )
    return {str(status): int(count) for status, count in result.all()}
