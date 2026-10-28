"""Automation jobs (F0-interfaces §6.1): registered only for the handler
groups bootstrap wires; an omitted group registers nothing.

S05.4: `monitors.evaluate(conversation_id)` (`llm` queue, queueing lock
`mon:{id}`, scheduled, no lock — AGENTS §13) and `system.expire_monitors`
(hourly, `system` queue).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from procrastinate import App
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.agent.budget import check_budget_thresholds
from assistant.agent.monitor_eval import MonitorEvalOutcome, MonitorEvalStatus, MonitorEvaluator
from assistant.automation import monitor_trigger, monitors
from assistant.conversations.models import Conversation
from assistant.core.clock import Clock
from assistant.core.jobs import MONITOR_TASK, JobScheduler
from assistant.db.uow import transaction
from assistant.inbox.notify import OwnerAlerts
from assistant.jobs.app import QUEUE_LLM, QUEUE_SYSTEM
from assistant.memory import summaries
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

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

    #: S05.4: `monitors.evaluate` and `system.expire_monitors`.
    monitors: MonitorJobs | None = None


def register_automation_tasks(app: App, handlers: AutomationJobHandlers | None) -> None:
    if handlers is None:
        return
    jobs = handlers.monitors
    if jobs is not None:
        if jobs.evaluator is not None:

            @app.task(name=MONITOR_TASK, queue=QUEUE_LLM)
            async def monitors_evaluate(conversation_id: int) -> None:
                # No procrastinate retry: a retry puts the row back to `todo`
                # with its queueing lock and collides with a newer queued
                # evaluation; the flag and the reconciler recover a failure.
                await jobs.evaluate(conversation_id)

        @app.periodic(cron="37 * * * *")
        @app.task(name=EXPIRE_MONITORS, queue=QUEUE_SYSTEM, queueing_lock=EXPIRE_MONITORS)
        async def expire_monitors(timestamp: int) -> None:
            await jobs.expire()
