"""M05 inbox jobs (F0-interfaces §6.1): registered only for the handler
groups bootstrap wires; an omitted group registers nothing."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from procrastinate import App, RetryStrategy
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.agent.budget import check_budget_thresholds
from assistant.agent.triage import TriageOutcome, Triager, TriageStatus
from assistant.conversations.models import Conversation
from assistant.core.clock import Clock
from assistant.core.jobs import NOTIFY_TASK, TRIAGE_TASK, JobScheduler
from assistant.db.uow import transaction
from assistant.inbox import tasks, triage_trigger
from assistant.inbox.notifications import NotificationDelivery
from assistant.inbox.notify import OwnerAlerts
from assistant.jobs.app import QUEUE_LLM, QUEUE_OUTBOUND, QUEUE_SYSTEM
from assistant.memory import summaries
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

#: Database errors, as `OWNER_REQUEST_RETRY`: `DBAPIError` also covers
#: integrity and data errors, not only transient ones, which is harmless — a
#: re-run reloads the row and decides again, at most three times. Never a
#: resend: the row is claimed (`uncertain`) before the send, so a re-run after
#: it finds nothing queued. Telegram errors are not exceptions here.
NOTIFY_DELIVER_RETRY = RetryStrategy(
    max_attempts=3, exponential_wait=2, retry_exceptions={OperationalError, DBAPIError}
)
EXPIRE_TASKS = "system.expire_tasks"


@dataclass(frozen=True, slots=True)
class TriageJob:
    """`inbox.triage` (S05.1 §3.8): a trailing debounce, then one triage run.

    Deferred with `schedule_at` and the `triage:{id}` queueing lock only,
    never a `lock` (AGENTS §13). While contact messages keep arriving, the
    job re-defers itself (its own row is `doing`, so the queueing lock is
    free). Consent, mode and budget are re-checked by the `Triager` when it
    runs. No procrastinate retry: a failed run leaves the flag set and the
    reconciler re-creates the job.
    """

    session_factory: async_sessionmaker[AsyncSession]
    triager: Triager
    scheduler: JobScheduler
    clock: Clock
    debounce: timedelta
    alerts: OwnerAlerts | None = None
    monthly_budget_usd: Decimal | None = None

    async def triage(self, conversation_id: int) -> TriageOutcome | None:
        now = self.clock.now()
        async with transaction(self.session_factory) as uow:
            ref = await summaries.conversation_ref(uow.session, conversation_id)
            if ref is None:
                return None
            later = await triage_trigger.quiet_until(
                uow.session,
                account_id=ref.account_id,
                conversation_id=conversation_id,
                now=now,
                debounce=self.debounce,
            )
        if later is not None:
            await triage_trigger.defer_triage(self.scheduler, conversation_id, at=later)
            return None
        outcome = await self.triager.triage(conversation_id)
        if outcome.status is TriageStatus.TRIAGED:
            # More arrived while the model ran: the store moved the flag to it.
            async with transaction(self.session_factory) as uow:
                pending = await uow.session.scalar(
                    select(Conversation.pending_triage_at).where(Conversation.id == conversation_id)
                )
            if pending is not None:
                await triage_trigger.defer_triage(
                    self.scheduler, conversation_id, at=max(now, pending) + self.debounce
                )
        logger.info(
            "triage_job_finished",
            conversation_id=conversation_id,
            status=outcome.status.value,
            calls=outcome.calls,
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

    async def expire_tasks(self) -> int:
        """`system.expire_tasks` (hourly): silent expiry of stale open tasks."""
        async with transaction(self.session_factory) as uow:
            expired = await tasks.expire_due(uow.session, clock=self.clock)
        if expired:
            logger.info("tasks_expired", count=expired)
        return expired


@dataclass(frozen=True, slots=True)
class InboxJobHandlers:
    """M05 inbox handlers, populated by their owning units."""

    #: S05.2: `notify.deliver`.
    notifications: NotificationDelivery | None = None
    #: S05.1: `inbox.triage` and `system.expire_tasks`.
    triage: TriageJob | None = None


def register_inbox_tasks(app: App, handlers: InboxJobHandlers | None) -> None:
    if handlers is None:
        return
    delivery = handlers.notifications
    if delivery is not None:

        @app.task(name=NOTIFY_TASK, queue=QUEUE_OUTBOUND, retry=NOTIFY_DELIVER_RETRY)
        async def notify_deliver(notification_id: int) -> None:
            await delivery.deliver(notification_id)

    triage = handlers.triage
    if triage is not None:

        @app.task(name=TRIAGE_TASK, queue=QUEUE_LLM)
        async def inbox_triage(conversation_id: int) -> None:
            await triage.triage(conversation_id)

        @app.periodic(cron="7 * * * *")
        @app.task(name=EXPIRE_TASKS, queue=QUEUE_SYSTEM, queueing_lock=EXPIRE_TASKS)
        async def expire_tasks(timestamp: int) -> None:
            await triage.expire_tasks()
