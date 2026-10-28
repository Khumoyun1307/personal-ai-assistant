"""Automation jobs (F0-interfaces §6.1; ARCHITECTURE §12.2): registered only
for the handler groups bootstrap wires; an omitted group registers nothing.

S05.3b — schedules and the digest:

| Task | Queue | Lock / schedule |
|---|---|---|
| `system.reconcile_schedules` | system | every minute; qlock = name |
| `automation.fire(automation_id, occurrence_at)` | outbound | qlock `auto:{id}:{epoch}` |
| `digest.build(account_id, period_end)` | llm | qlock `digest:{acct}:{epoch}`; no retry |

No job carries a procrastinate `lock`. Arguments are ids and epoch seconds
only (AGENTS §13). Every handler reloads its rows and is safe to run twice:
the occurrence ledger and the `digests` row are the dedupe, the queueing
locks only an optimization.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from procrastinate import App, RetryStrategy
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.automation import schedules
from assistant.automation.schedules import DueOccurrence, FireAction, FirePlan
from assistant.core.clock import Clock
from assistant.core.jobs import (
    DIGEST_TASK,
    FIRE_TASK,
    JobScheduler,
    digest_queueing_lock,
    occurrence_queueing_lock,
)
from assistant.db.uow import transaction
from assistant.inbox.digest import DigestDelivery, DigestResult
from assistant.jobs.app import QUEUE_LLM, QUEUE_OUTBOUND, QUEUE_SYSTEM
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

RECONCILE_SCHEDULES = "system.reconcile_schedules"

#: Database hiccups only (F0-interfaces §6.1: bounded, transient). A re-run
#: is harmless: a fired or skipped occurrence is a no-op, and a second
#: `digest.build` finds its `digests` row.
FIRE_RETRY = RetryStrategy(
    max_attempts=3, exponential_wait=2, retry_exceptions={OperationalError, DBAPIError}
)

#: A claimed occurrence with no `automation.fire` job after this lost its
#: defer (a crash between the reconciler's commit and the defer).
FIRE_GRACE = timedelta(minutes=2)
#: Older claims are left alone: firing them now would be later than any
#: digest tolerates (they stay `claimed` in the ledger, visible).
FIRE_WINDOW = timedelta(hours=1)
LOST_FIRE_BATCH = 100

_LOST_FIRES = text(
    """
    SELECT o.automation_id, o.account_id, o.occurrence_at
    FROM app.automation_occurrences AS o
    WHERE o.status = 'claimed'
      AND o.created_at < :older_than
      AND o.created_at >= :newer_than
      AND NOT EXISTS (
          -- By queueing lock (`auto:{id}:{epoch}`, core/jobs.py).
          SELECT 1 FROM procrastinate.procrastinate_jobs AS j
          WHERE j.queueing_lock = 'auto:' || o.automation_id::text || ':'
                || floor(extract(epoch FROM o.occurrence_at))::bigint::text
            AND j.task_name = :task
            AND j.status IN ('todo', 'doing')
      )
    ORDER BY o.occurrence_at, o.id
    LIMIT :limit
    """
)


async def lost_fires(session: AsyncSession, now: datetime) -> list[DueOccurrence]:
    """Claimed occurrences whose `automation.fire` job was lost."""
    result = await session.execute(
        _LOST_FIRES,
        {
            "older_than": now - FIRE_GRACE,
            "newer_than": now - FIRE_WINDOW,
            "task": FIRE_TASK,
            "limit": LOST_FIRE_BATCH,
        },
    )
    return [
        DueOccurrence(int(automation_id), int(account_id), occurrence_at)
        for automation_id, account_id, occurrence_at in result.all()
    ]


@dataclass(frozen=True, slots=True)
class ScheduleJobs:
    """S05.3b's job bodies, callable directly in tests."""

    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    scheduler: JobScheduler
    digests: DigestDelivery
    #: Flag `digest.daily`: digest automations follow the preference and
    #: fire. `/digest` works without it.
    digest_enabled: bool = False

    async def reconcile_schedules(self) -> int:
        """Claim due occurrences in one transaction, then defer their fire
        jobs after the commit (a queueing-lock conflict means one is
        queued). Lost defers from earlier runs are re-deferred too."""
        now = self.clock.now()
        async with transaction(self.session_factory) as uow:
            report = await schedules.reconcile_due(
                uow.session, now, digest_enabled=self.digest_enabled
            )
            lost = await lost_fires(uow.session, now)
        due = list(dict.fromkeys((*report.claimed, *lost)))
        for occurrence in due:
            await self.scheduler.defer(
                FIRE_TASK,
                args={"automation_id": occurrence.automation_id, "occurrence_at": occurrence.epoch},
                queueing_lock=occurrence_queueing_lock(occurrence.automation_id, occurrence.epoch),
            )
        if due or report.skipped or report.invalid or report.rezoned or report.digests:
            logger.info(
                "schedules_reconciled",
                claimed=len(report.claimed),
                redeferred=len(lost),
                skipped=report.skipped,
                completed=report.completed,
                expired=report.expired,
                invalid=report.invalid,
                rezoned=report.rezoned,
                digests=report.digests,
            )
        return len(due)

    async def fire(self, automation_id: int, occurrence_at: int) -> FirePlan:
        """`automation.fire` (S05.3b §3.3): re-check, act, then mark the
        occurrence fired. A crash after the digest was sent and before the
        mark re-runs this: the new `digest.build` finds its row and sends
        nothing, and the occurrence is marked fired."""
        moment = schedules.from_epoch(occurrence_at)
        async with transaction(self.session_factory) as uow:
            plan = await schedules.plan_fire(
                uow.session,
                automation_id=automation_id,
                occurrence_at=moment,
                digest_enabled=self.digest_enabled,
            )
        if plan.action is FireAction.DIGEST and plan.account_id is not None:
            await self.scheduler.defer(
                DIGEST_TASK,
                args={"account_id": plan.account_id, "period_end": occurrence_at},
                queueing_lock=digest_queueing_lock(plan.account_id, occurrence_at),
            )
            async with transaction(self.session_factory) as uow:
                await schedules.mark_fired(
                    uow.session,
                    automation_id=automation_id,
                    occurrence_at=moment,
                    now=self.clock.now(),
                )
        logger.info(
            "automation_fired",
            automation_id=automation_id,
            account_id=plan.account_id,
            action=plan.action.value,
            note=None if plan.note is None else plan.note.value,
        )
        return plan

    async def build_digest(self, account_id: int, period_end: int) -> DigestResult:
        return await self.digests.build(account_id, schedules.from_epoch(period_end))


@dataclass(frozen=True, slots=True)
class AutomationJobHandlers:
    """Automation handlers, populated by their owning units."""

    #: S05.3b: `system.reconcile_schedules`, `automation.fire`, `digest.build`.
    schedules: ScheduleJobs | None = None


def register_automation_tasks(app: App, handlers: AutomationJobHandlers | None) -> None:
    """Omitted groups register nothing."""
    if handlers is None:
        return
    jobs = handlers.schedules
    if jobs is not None:

        @app.periodic(cron="* * * * *")
        @app.task(name=RECONCILE_SCHEDULES, queue=QUEUE_SYSTEM, queueing_lock=RECONCILE_SCHEDULES)
        async def reconcile_schedules(timestamp: int) -> None:
            await jobs.reconcile_schedules()

        @app.task(name=FIRE_TASK, queue=QUEUE_OUTBOUND, retry=FIRE_RETRY)
        async def automation_fire(automation_id: int, occurrence_at: int) -> None:
            # Queueing lock `auto:{id}:{epoch}` is set at defer; no `lock`.
            await jobs.fire(automation_id, occurrence_at)

        @app.task(name=DIGEST_TASK, queue=QUEUE_LLM)
        async def digest_build(account_id: int, period_end: int) -> None:
            # Queueing lock `digest:{acct}:{epoch}` is set at defer; no `lock`
            # and no retry: the `digests` row dedupes, and a claimed digest
            # is never sent twice.
            await jobs.build_digest(account_id, period_end)
