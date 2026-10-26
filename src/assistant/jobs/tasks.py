"""Task catalogue (ARCHITECTURE §12.2).

Task bodies are methods of `JobHandlers`, plain async code that tests can call
directly; `register_tasks` binds them to the procrastinate app with their
queue, lock and schedule. Job arguments are ids only (AGENTS §13).

| Task | Queue | Lock / schedule |
|---|---|---|
| `agent.owner_request(turn_id)` | llm | lock `acct:{id}:control` (set at defer) |
| `connections.notify_owner(event_id)` | system | deferred with the registry change (S02.1) |
| `agent.apply_confirmation(confirmation_id)` | llm | lock `acct:{id}:control` (at defer) |
| `memory.refresh_summary(conversation_id)` | llm | qlock `sum:{conv}`, `schedule_at`; no lock |
| `system.ingestion_watchdog` | system | every minute |
| `system.reconcile_pending` | system | every minute |
| `system.retention_sweep` | system | hourly |
| `system.retry_stalled_jobs` | system | every 10 minutes |
| `system.remove_old_jobs` | system | daily |
| `system.heartbeat_ping` | system | every 5 minutes |
| `system.backup_check` | system | daily |

Periodic tasks carry a queueing lock so a slow run never piles up copies.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from procrastinate import App, RetryStrategy
from procrastinate.exceptions import UniqueViolation
from procrastinate.jobs import Status
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.agent.budget import check_budget_thresholds
from assistant.agent.confirmations import ApplyOutcome
from assistant.agent.runtime import RunOutcome
from assistant.agent.summarize import RefreshOutcome
from assistant.core.clock import Clock
from assistant.core.jobs import (
    APPLY_CONFIRMATION_TASK,
    CONNECTION_NOTIFY_TASK,
    OWNER_REQUEST_TASK,
    REFRESH_SUMMARY_TASK,
    JobScheduler,
    owner_control_lock,
)
from assistant.db.uow import transaction
from assistant.inbox.backup_check import BackupReport, check_backup, deliver_restore_notice
from assistant.inbox.connection_alerts import notify_connection_change
from assistant.inbox.notify import AlertKind, AlertResult, OwnerAlerts
from assistant.inbox.policy_notice import deliver_policy_update_notices
from assistant.inbox.watchdog import STALL_AFTER, check_ingestion, poller_heartbeat_at
from assistant.jobs.app import QUEUE_LLM, QUEUE_SYSTEM, STALLED_WORKER_TIMEOUT
from assistant.jobs.reconcile import (
    fail_interrupted_confirmations,
    unapplied_confirmations,
    unqueued_owner_turns,
    unqueued_summary_refreshes,
)
from assistant.memory import summaries
from assistant.observability import heartbeat
from assistant.observability.logging import get_logger
from assistant.privacy.retention import SweepReport, sweep_all

logger = get_logger(__name__)

#: Finished jobs are kept this long for inspection, then deleted. Failed and
#: aborted jobs are kept: they are evidence.
JOB_HISTORY_HOURS = 72

#: Bounded retries for infrastructure hiccups only (AGENTS §13: never
#: `retry=True`). Provider errors are handled inside the run, not retried here,
#: and a retry of an already-answered turn is a no-op (see the runtime).
OWNER_REQUEST_RETRY = RetryStrategy(
    max_attempts=3, exponential_wait=2, retry_exceptions={OperationalError, DBAPIError}
)


#: Database hiccups only. A Telegram failure is reported by the channel as an
#: undelivered result, not raised, and is never retried blindly.
NOTIFY_RETRY = RetryStrategy(
    max_attempts=3, exponential_wait=2, retry_exceptions={OperationalError, DBAPIError}
)


class OwnerRuntime(Protocol):
    async def run(self, turn_id: int) -> RunOutcome: ...


class Applier(Protocol):
    async def apply(self, confirmation_id: int) -> ApplyOutcome: ...


class SummaryRefresher(Protocol):
    async def refresh(self, conversation_id: int) -> RefreshOutcome: ...


@dataclass(frozen=True, slots=True)
class JobHandlers:
    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    scheduler: JobScheduler
    owner_runtime: OwnerRuntime
    alerts: OwnerAlerts
    monthly_budget_usd: Decimal | None
    heartbeat_url: str | None
    #: Runs held tool calls the owner confirmed (S03.1). Required: a worker
    #: that could not apply an owner's confirmed change must not start.
    confirmation_applier: Applier
    #: Refreshes rolling summaries (S03.2).
    summary_refresher: SummaryRefresher
    #: Release flag `summary.refresh` (M03 D61): background refreshes run
    #: only when it is on; the read tools still summarize on demand.
    summaries_enabled: bool = False
    summary_debounce: timedelta = summaries.DEFAULT_REFRESH_DEBOUNCE
    summary_max_wait: timedelta = summaries.DEFAULT_REFRESH_MAX_WAIT
    backup_status_path: Path | None = None
    #: An off-site remote is configured: the upload marker is checked too.
    backup_upload_required: bool = False
    #: Linked from the policy-update notice (AGENTS §16).
    privacy_policy_url: str | None = None

    async def apply_confirmation(self, confirmation_id: int) -> ApplyOutcome:
        return await self.confirmation_applier.apply(confirmation_id)

    async def owner_request(self, turn_id: int) -> RunOutcome:
        outcome = await self.owner_runtime.run(turn_id)
        if outcome.run_id is not None and outcome.account_id is not None:
            await check_budget_thresholds(
                self.session_factory,
                self.clock,
                self.alerts,
                account_id=outcome.account_id,
                monthly_budget_usd=self.monthly_budget_usd,
            )
        return outcome

    async def refresh_summary(self, conversation_id: int) -> RefreshOutcome | None:
        """`memory.refresh_summary`: a trailing debounce, then one refresh.

        Deferred with `schedule_at` and a queueing lock only — never a `lock`
        (AGENTS §13). While the chat is still active the job re-defers itself
        (its own row is `doing`, so the queueing lock is free) instead of
        holding anything; consent and budget are re-checked by the refresher
        when it runs, so a revocation after the defer stops it.
        """
        if not self.summaries_enabled:
            return None
        async with transaction(self.session_factory) as uow:
            ref = await summaries.conversation_ref(uow.session, conversation_id)
            if ref is None:
                return None
            timing = await summaries.refresh_timing(
                uow.session,
                ref,
                now=self.clock.now(),
                debounce=self.summary_debounce,
                max_wait=self.summary_max_wait,
            )
        if not timing.due:
            if timing.later is not None:
                await summaries.defer_refresh(self.scheduler, conversation_id, at=timing.later)
            return None
        outcome = await self.summary_refresher.refresh(conversation_id)
        if outcome.more_pending:
            await summaries.defer_refresh(self.scheduler, conversation_id, at=None)
        logger.info(
            "summary_job_finished",
            conversation_id=conversation_id,
            status=outcome.status.value,
            calls=outcome.calls,
        )
        if outcome.calls and outcome.account_id is not None:
            await check_budget_thresholds(
                self.session_factory,
                self.clock,
                self.alerts,
                account_id=outcome.account_id,
                monthly_budget_usd=self.monthly_budget_usd,
            )
        return outcome

    async def connection_notify(self, event_id: int) -> AlertResult:
        return await notify_connection_change(
            self.session_factory, self.clock, self.alerts, event_id
        )

    async def ingestion_watchdog(self) -> None:
        report = await check_ingestion(self.session_factory, self.clock, self.alerts)
        if report.stalled:
            logger.warning("ingestion_stalled", heartbeat_age_s=_seconds(report.heartbeat_age))
        # A restore notice left by `post-restore` goes out with the first run.
        if await deliver_restore_notice(self.session_factory, self.alerts):
            logger.warning("restore_notice_delivered")
        # A policy bump that widens processing is announced once (AGENTS §16).
        if await deliver_policy_update_notices(
            self.session_factory, self.clock, self.alerts, policy_url=self.privacy_policy_url
        ):
            logger.info("policy_update_notice_delivered")

    async def backup_check(self) -> BackupReport | None:
        """Daily: alert when the dump (or the off-site upload) is older than 26 h."""
        return await check_backup(
            self.clock,
            self.alerts,
            status_path=self.backup_status_path,
            upload_required=self.backup_upload_required,
        )

    async def reconcile_pending(self) -> int:
        async with transaction(self.session_factory) as uow:
            lost = await unqueued_owner_turns(uow.session, self.clock.now())
            for turn_id, account_id in lost:
                await self.scheduler.defer(
                    OWNER_REQUEST_TASK,
                    args={"turn_id": turn_id},
                    lock=owner_control_lock(account_id),
                    connection=await uow.raw_connection(),
                )
            unapplied = await unapplied_confirmations(uow.session, self.clock.now())
            for confirmation_id, account_id in unapplied:
                await self.scheduler.defer(
                    APPLY_CONFIRMATION_TASK,
                    args={"confirmation_id": confirmation_id},
                    lock=owner_control_lock(account_id),
                    connection=await uow.raw_connection(),
                )
            interrupted = await fail_interrupted_confirmations(uow.session, self.clock.now())
            stale_summaries = (
                await unqueued_summary_refreshes(uow.session, self.clock.now())
                if self.summaries_enabled
                else []
            )
        # After the commit, without a connection: a queueing-lock conflict
        # inside the transaction would abort it. Scheduled one debounce out,
        # so a chat whose refresh keeps failing is retried at that pace.
        resummarize_at = self.clock.now() + self.summary_debounce
        for conversation_id in stale_summaries:
            await summaries.defer_refresh(self.scheduler, conversation_id, at=resummarize_at)
        if stale_summaries:
            logger.warning("summary_refreshes_redeferred", count=len(stale_summaries))
        if lost:
            logger.warning("owner_turns_redeferred", count=len(lost))
        if unapplied:
            logger.warning("confirmations_redeferred", count=len(unapplied))
        for confirmation_id, account_id, tool_name in interrupted:
            # After the commit: the row is `failed` whether or not this arrives.
            logger.warning("confirmation_interrupted", confirmation_id=confirmation_id)
            await self.alerts.notify_owner(
                account_id,
                AlertKind.CONFIRMATION_INTERRUPTED,
                dedupe_key=f"confirmation:{confirmation_id}:interrupted",
                tool=html.escape(tool_name),
            )
        return len(lost) + len(unapplied) + len(interrupted) + len(stale_summaries)

    async def retention_sweep(self) -> SweepReport:
        report = await sweep_all(
            self.session_factory,
            self.clock,
            scheduler=self.scheduler,
            # The rollup is a background summary: same flag (M03 D61, D72).
            rollups_enabled=self.summaries_enabled,
        )
        if report.total:
            logger.info(
                "retention_swept",
                inbound_payloads=report.inbound_payloads,
                control_turn_texts=report.control_turn_texts,
                llm_calls=report.llm_calls,
                agent_runs=report.agent_runs,
                audit_entries=report.audit_entries,
                connection_events=report.connection_events,
                foreign_connections=report.foreign_connections,
                message_contents=report.message_contents,
                contact_names=report.contact_names,
                expired_inputs=report.expired_inputs,
                tool_confirmations=report.tool_confirmations,
                period_summaries=report.period_summaries,
                forgotten_memories=report.forgotten_memories,
                rollups_scheduled=report.rollups_scheduled,
            )
        return report

    async def heartbeat_ping(self) -> bool:
        """Ping only while ingestion is healthy; silence is the alarm."""
        if self.heartbeat_url is None:
            return False
        async with transaction(self.session_factory) as uow:
            beat = await poller_heartbeat_at(uow.session)
        if beat is None or self.clock.now() - beat > STALL_AFTER:
            logger.warning("heartbeat_ping_skipped", detail="ingestion not healthy")
            return False
        return await heartbeat.ping(self.heartbeat_url)


def _seconds(value: object) -> int | None:
    total = getattr(value, "total_seconds", None)
    return None if total is None else int(total())


def register_tasks(app: App, handlers: JobHandlers) -> None:
    """Bind the catalogue to the app. Called once, by bootstrap."""

    @app.task(name=OWNER_REQUEST_TASK, queue=QUEUE_LLM, retry=OWNER_REQUEST_RETRY)
    async def owner_request(turn_id: int) -> None:
        await handlers.owner_request(turn_id)

    @app.task(name=APPLY_CONFIRMATION_TASK, queue=QUEUE_LLM, retry=OWNER_REQUEST_RETRY)
    async def apply_confirmation(confirmation_id: int) -> None:
        # Lock `acct:{id}:control` is set at defer; a retry or redelivery is
        # a no-op once the row has left `confirmed`.
        await handlers.apply_confirmation(confirmation_id)

    @app.task(name=REFRESH_SUMMARY_TASK, queue=QUEUE_LLM)
    async def refresh_summary(conversation_id: int) -> None:
        # Queueing lock `sum:{conversation_id}` and `schedule_at` are set at
        # defer; no `lock` on a future-scheduled job (AGENTS §13). No
        # procrastinate retry: a retry puts the row back to `todo` with its
        # queueing lock and collides with a newer queued refresh. A failed
        # job leaves the flag set; `reconcile_pending` re-creates it.
        await handlers.refresh_summary(conversation_id)

    @app.task(name=CONNECTION_NOTIFY_TASK, queue=QUEUE_SYSTEM, retry=NOTIFY_RETRY)
    async def connection_notify(event_id: int) -> None:
        await handlers.connection_notify(event_id)

    @app.periodic(cron="* * * * *")
    @app.task(
        name="system.ingestion_watchdog",
        queue=QUEUE_SYSTEM,
        queueing_lock="system.ingestion_watchdog",
    )
    async def ingestion_watchdog(timestamp: int) -> None:
        await handlers.ingestion_watchdog()

    @app.periodic(cron="* * * * *")
    @app.task(
        name="system.reconcile_pending",
        queue=QUEUE_SYSTEM,
        queueing_lock="system.reconcile_pending",
    )
    async def reconcile_pending(timestamp: int) -> None:
        await handlers.reconcile_pending()

    @app.periodic(cron="7 * * * *")
    @app.task(
        name="system.retention_sweep", queue=QUEUE_SYSTEM, queueing_lock="system.retention_sweep"
    )
    async def retention_sweep(timestamp: int) -> None:
        await handlers.retention_sweep()

    @app.periodic(cron="*/10 * * * *")
    @app.task(
        name="system.retry_stalled_jobs",
        queue=QUEUE_SYSTEM,
        queueing_lock="system.retry_stalled_jobs",
    )
    async def retry_stalled_jobs(timestamp: int) -> None:
        # Required (TF §8): a worker that died mid-job leaves it `doing`
        # forever otherwise. Only workers silent past the timeout count.
        stalled = await app.job_manager.get_stalled_jobs(
            seconds_since_heartbeat=STALLED_WORKER_TIMEOUT
        )
        for job in stalled:
            if job.queueing_lock is not None and list(
                await app.job_manager.list_jobs_async(
                    queueing_lock=job.queueing_lock, status="todo"
                )
            ):
                # A retry would put the row back to `todo` with its queueing
                # lock, which a newer queued job already holds (unique index
                # on `todo` rows): fail it instead — the queued job does the
                # same work (a summary refresh, the next period's task).
                await app.job_manager.finish_job(job, status=Status.FAILED, delete_job=False)
                logger.warning("stalled_job_failed", job_id=job.id, task=job.task_name)
                continue
            try:
                await app.job_manager.retry_job(job)
            except UniqueViolation:
                # The same collision, with a job queued since the check.
                await app.job_manager.finish_job(job, status=Status.FAILED, delete_job=False)
                logger.warning("stalled_job_failed", job_id=job.id, task=job.task_name)
                continue
            logger.warning("stalled_job_retried", job_id=job.id, task=job.task_name)

    @app.periodic(cron="17 3 * * *")
    @app.task(
        name="system.remove_old_jobs", queue=QUEUE_SYSTEM, queueing_lock="system.remove_old_jobs"
    )
    async def remove_old_jobs(timestamp: int) -> None:
        await app.job_manager.delete_old_jobs(nb_hours=JOB_HISTORY_HOURS)

    @app.periodic(cron="*/5 * * * *")
    @app.task(
        name="system.heartbeat_ping", queue=QUEUE_SYSTEM, queueing_lock="system.heartbeat_ping"
    )
    async def heartbeat_ping(timestamp: int) -> None:
        await handlers.heartbeat_ping()

    @app.periodic(cron="23 4 * * *")
    @app.task(name="system.backup_check", queue=QUEUE_SYSTEM, queueing_lock="system.backup_check")
    async def backup_check(timestamp: int) -> None:
        await handlers.backup_check()
