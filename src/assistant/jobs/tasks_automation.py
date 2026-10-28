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

S05.4: `monitors.evaluate(conversation_id)` (`llm` queue, queueing lock
`mon:{id}`, scheduled, no lock — AGENTS §13) and `system.expire_monitors`
(hourly, `system` queue).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from procrastinate import App, RetryStrategy
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.agent.budget import check_budget_thresholds
from assistant.agent.monitor_eval import MonitorEvalOutcome, MonitorEvalStatus, MonitorEvaluator
from assistant.automation import monitor_trigger, monitors, schedules
from assistant.automation.schedules import FireAction, FirePlan, Recovery
from assistant.conversations.models import Conversation
from assistant.core.clock import Clock
from assistant.core.jobs import (
    DIGEST_TASK,
    FIRE_TASK,
    MONITOR_TASK,
    JobScheduler,
    digest_queueing_lock,
    occurrence_queueing_lock,
)
from assistant.db.uow import transaction
from assistant.inbox.digest import DigestDelivery, DigestResult
from assistant.inbox.notify import OwnerAlerts
from assistant.jobs.app import QUEUE_LLM, QUEUE_OUTBOUND, QUEUE_SYSTEM
from assistant.memory import summaries
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
#: defer (a crash between the reconciler's commit and the defer). How long
#: it may still be recovered is its rule's deadline, not a fixed window
#: (`schedules.recover_claims`).
FIRE_GRACE = timedelta(minutes=2)
LOST_FIRE_BATCH = 100

_LOST_FIRES = text(
    """
    SELECT o.id
    FROM app.automation_occurrences AS o
    WHERE o.status = 'claimed'
      AND o.created_at < :older_than
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


async def lost_fires(session: AsyncSession, now: datetime) -> Recovery:
    """Claimed occurrences whose `automation.fire` job was lost: re-deferred
    while their rule still allows them, settled once it does not."""
    result = await session.execute(
        _LOST_FIRES,
        {"older_than": now - FIRE_GRACE, "task": FIRE_TASK, "limit": LOST_FIRE_BATCH},
    )
    return await schedules.recover_claims(session, [int(row_id) for (row_id,) in result.all()], now)


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
        due = list(dict.fromkeys((*report.claimed, *lost.due)))
        for occurrence in due:
            await self.scheduler.defer(
                FIRE_TASK,
                args={"automation_id": occurrence.automation_id, "occurrence_at": occurrence.epoch},
                queueing_lock=occurrence_queueing_lock(occurrence.automation_id, occurrence.epoch),
            )
        changed = report.skipped or report.invalid or report.rezoned or report.digests
        if due or changed or lost.settled:
            logger.info(
                "schedules_reconciled",
                claimed=len(report.claimed),
                redeferred=len(lost.due),
                settled=lost.settled,
                skipped=report.skipped,
                completed=report.completed,
                expired=report.expired,
                invalid=report.invalid,
                rezoned=report.rezoned,
                digests=report.digests,
            )
        return len(due)

    async def fire(self, automation_id: int, occurrence_at: int) -> FirePlan:
        """`automation.fire` (S05.3b §3.3): re-check (deadlines included: the
        job may have waited in its queue), act, then mark the occurrence
        fired. A crash after the digest was sent and before the mark re-runs
        this: the new `digest.build` finds its row and sends nothing, and the
        occurrence is marked fired."""
        moment = schedules.from_epoch(occurrence_at)
        async with transaction(self.session_factory) as uow:
            plan = await schedules.plan_fire(
                uow.session,
                automation_id=automation_id,
                occurrence_at=moment,
                digest_enabled=self.digest_enabled,
                now=self.clock.now(),
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


EXPIRE_MONITORS = "system.expire_monitors"


@dataclass(frozen=True, slots=True)
class MonitorJobs:
    """`monitors.evaluate` (S05.4 §3.6): a trailing debounce, then one
    `evaluate_monitor` run for the chat's semantic monitors; and the hourly
    expiry. `evaluator` is None while flag `monitors.run` is off: then only
    the expiry runs.

    Deferred with `schedule_at` and the `mon:{id}` queueing lock only, never
    a `lock` (AGENTS §13). While contact messages keep arriving, the job
    re-defers itself (its own row is `doing`, so the queueing lock is free).
    Mode, consent, budget and the monitors themselves are re-checked by the
    evaluator when it runs. No procrastinate retry: a failed run leaves the
    flag set and the reconciler re-creates the job.
    """

    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    scheduler: JobScheduler
    debounce: timedelta
    evaluator: MonitorEvaluator | None = None
    alerts: OwnerAlerts | None = None
    monthly_budget_usd: Decimal | None = None

    async def evaluate(self, conversation_id: int) -> MonitorEvalOutcome | None:
        if self.evaluator is None:
            return None  # flag `monitors.run` off: nothing is evaluated
        now = self.clock.now()
        async with transaction(self.session_factory) as uow:
            ref = await summaries.conversation_ref(uow.session, conversation_id)
            if ref is None:
                return None
            later = await monitor_trigger.quiet_until(
                uow.session,
                account_id=ref.account_id,
                conversation_id=conversation_id,
                now=now,
                debounce=self.debounce,
            )
        if later is not None:
            await monitor_trigger.defer_evaluation(self.scheduler, conversation_id, at=later)
            return None
        outcome = await self.evaluator.evaluate(conversation_id)
        if outcome.status is MonitorEvalStatus.EVALUATED:
            # More arrived while the model ran: the run moved the flag to it.
            async with transaction(self.session_factory) as uow:
                pending = await uow.session.scalar(
                    select(Conversation.pending_monitor_at).where(
                        Conversation.id == conversation_id
                    )
                )
            if pending is not None:
                await monitor_trigger.defer_evaluation(
                    self.scheduler, conversation_id, at=max(now, pending) + self.debounce
                )
        logger.info(
            "monitor_job_finished",
            conversation_id=conversation_id,
            status=outcome.status.value,
            calls=outcome.calls,
            alerts=outcome.alerts,
        )
        if outcome.calls and outcome.account_id is not None and self.alerts is not None:
            await check_budget_thresholds(
                self.session_factory,
                self.clock,
                self.alerts,
                account_id=outcome.account_id,
                monthly_budget_usd=self.monthly_budget_usd,
            )
        return outcome

    async def expire(self) -> int:
        """`system.expire_monitors` (hourly): monitors past their expiry end.
        Matching already ignores them, so an hour's delay never fires one."""
        async with transaction(self.session_factory) as uow:
            expired = await monitors.expire_due(uow.session, now=self.clock.now())
        if expired:
            logger.info("monitors_expired", count=expired)
        return expired


@dataclass(frozen=True, slots=True)
class AutomationJobHandlers:
    """Automation handlers, populated by their owning units."""

    #: S05.3b: `system.reconcile_schedules`, `automation.fire`, `digest.build`.
    schedules: ScheduleJobs | None = None
    #: S05.4: `monitors.evaluate` and `system.expire_monitors`.
    monitors: MonitorJobs | None = None


def register_automation_tasks(app: App, handlers: AutomationJobHandlers | None) -> None:
    """Omitted groups register nothing."""
    if handlers is None:
        return
    schedule_jobs = handlers.schedules
    if schedule_jobs is not None:

        @app.periodic(cron="* * * * *")
        @app.task(name=RECONCILE_SCHEDULES, queue=QUEUE_SYSTEM, queueing_lock=RECONCILE_SCHEDULES)
        async def reconcile_schedules(timestamp: int) -> None:
            await schedule_jobs.reconcile_schedules()

        @app.task(name=FIRE_TASK, queue=QUEUE_OUTBOUND, retry=FIRE_RETRY)
        async def automation_fire(automation_id: int, occurrence_at: int) -> None:
            # Queueing lock `auto:{id}:{epoch}` is set at defer; no `lock`.
            await schedule_jobs.fire(automation_id, occurrence_at)

        @app.task(name=DIGEST_TASK, queue=QUEUE_LLM)
        async def digest_build(account_id: int, period_end: int) -> None:
            # Queueing lock `digest:{acct}:{epoch}` is set at defer; no `lock`
            # and no retry: the `digests` row dedupes, and a claimed digest
            # is never sent twice.
            await schedule_jobs.build_digest(account_id, period_end)

    monitor_jobs = handlers.monitors
    if monitor_jobs is not None:
        if monitor_jobs.evaluator is not None:

            @app.task(name=MONITOR_TASK, queue=QUEUE_LLM)
            async def monitors_evaluate(conversation_id: int) -> None:
                # No procrastinate retry: a retry puts the row back to `todo`
                # with its queueing lock and collides with a newer queued
                # evaluation; the flag and the reconciler recover a failure.
                await monitor_jobs.evaluate(conversation_id)

        @app.periodic(cron="37 * * * *")
        @app.task(name=EXPIRE_MONITORS, queue=QUEUE_SYSTEM, queueing_lock=EXPIRE_MONITORS)
        async def expire_monitors(timestamp: int) -> None:
            await monitor_jobs.expire()
