"""Composition root.

The only module that instantiates adapters and wires them to ports
(AGENTS §5, ARCHITECTURE §5.2). The running process (S01.5) calls these
factories; nothing else constructs an SDK client.
"""

from __future__ import annotations

import contextlib
import functools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from aiogram import Bot
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.actions.config import ActionsConfig
from assistant.actions.drafts import assist_trigger
from assistant.actions.ports import ApprovalCardPresenter
from assistant.agent.confirmations import ConfirmationApplier
from assistant.agent.persistence import DbCallRecorder, DbConsentReader
from assistant.agent.ports import ControlChannel
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime
from assistant.agent.summarize import SummarizeConfig, Summarizer
from assistant.agent.tools.registry import ToolServices, default_registry
from assistant.config import ConfigError, Settings
from assistant.conversations.listeners import IngestionListener
from assistant.core.clock import Clock, SystemClock
from assistant.core.routing import Route
from assistant.core.supervisor import Role, Supervisor
from assistant.db.advisory_lock import NAMESPACE_INBOX, NAMESPACE_POLLER, AdvisoryLock
from assistant.db.engine import build_engine, build_session_factory
from assistant.inbox.notify import OwnerAlerts
from assistant.inbox.processor import InboxProcessor
from assistant.jobs.app import QUEUE_SYSTEM, QUEUES, build_app, run_queue_worker
from assistant.jobs.reconcile import Reconciler
from assistant.jobs.scheduler import ProcrastinateScheduler
from assistant.jobs.tasks import JobHandlers, register_tasks
from assistant.jobs.tasks_actions import ActionsJobHandlers
from assistant.jobs.tasks_automation import AutomationJobHandlers
from assistant.jobs.tasks_inbox import InboxJobHandlers
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.llm.metering import MeteredLLMClient
from assistant.llm.openai_adapter import OpenAIClient
from assistant.llm.port import LLMClient
from assistant.memory.summaries import SummaryRefreshTrigger
from assistant.privacy.post_restore import assert_not_restore_pending
from assistant.privacy.requests import RequestedDeletions
from assistant.process import InboxRole, PollerRole, WorkerRole, worker_heartbeat_probe
from assistant.telegram.business import BusinessPipeline
from assistant.telegram.connections import AiogramConnectionLookup, ConnectionRegistry
from assistant.telegram.control.agent_channel import GatewayControlChannel
from assistant.telegram.control.dispatcher import ControlDispatcher
from assistant.telegram.control.inbox_handlers import ControlInboxHandlers, RefusalLimiter
from assistant.telegram.control.routes import ControlRoutes
from assistant.telegram.gateway import AiogramSender, OutboundGateway
from assistant.telegram.ingress.aiogram_source import AiogramUpdateSource
from assistant.telegram.ingress.poller import Poller
from assistant.telegram.ingress.writer import write_state
from assistant.telegram.routing import route_update

if TYPE_CHECKING:
    from assistant.agent.drafting import Drafter  # type: ignore[import-untyped, unused-ignore]

#: SDK-level retries for connection errors, 429 and 5xx. Safe for a model call
#: (it changes nothing in the world); never applied to Telegram sends.
OPENAI_MAX_RETRIES = 2


def build_llm_client(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> LLMClient:
    """OpenAI behind the consent gate and metering, in that order.

    Consent is outermost so that a call without consent never reaches even the
    metering layer, let alone the provider (§6 invariant 9).
    """
    if settings.openai_api_key is None:
        raise ConfigError("OPENAI_API_KEY is not set")
    sdk = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(), max_retries=OPENAI_MAX_RETRIES
    )
    metered = MeteredLLMClient(OpenAIClient(sdk), DbCallRecorder(session_factory, clock))
    return ConsentGatedLLMClient(metered, DbConsentReader(session_factory))


def agent_config(settings: Settings) -> AgentConfig:
    return AgentConfig(
        model=settings.model_routes.model_for("owner_request"),
        effort=settings.llm_owner_request_effort,
        monthly_budget_usd=settings.llm_monthly_budget_usd,
        enabled_flags=settings.feature_flags,
    )


def build_confirmation_applier(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    channel: ControlChannel,
    clock: Clock,
    *,
    drafter: Drafter | None = None,
    approval_cards: ApprovalCardPresenter | None = None,
    actions_config: ActionsConfig | None = None,
    notifications_enabled: bool = False,
) -> ConfirmationApplier:
    """Runs held tool calls the owner confirmed (S03.1), with the same
    registry and flags as the owner runtime."""
    return ConfirmationApplier(
        session_factory=session_factory,
        clock=clock,
        channel=channel,
        registry=default_registry(),
        services=ToolServices(
            session_factory=session_factory,
            clock=clock,
            monthly_budget_usd=settings.llm_monthly_budget_usd,
            channel=channel,
            drafter=drafter,
            approval_cards=approval_cards,
            actions_config=actions_config,
            notifications_enabled=notifications_enabled,
        ),
        enabled_flags=settings.feature_flags,
    )


def summarize_config(settings: Settings) -> SummarizeConfig:
    return SummarizeConfig(
        model=settings.model_routes.model_for("summarize"),
        effort=settings.llm_summarize_effort,
        monthly_budget_usd=settings.llm_monthly_budget_usd,
    )


def build_summarizer(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    llm: LLMClient,
) -> Summarizer:
    """The `summarize` run type (S03.2), on the same consent-gated client."""
    return Summarizer(
        session_factory=session_factory, llm=llm, clock=clock, config=summarize_config(settings)
    )


def summary_timing(settings: Settings) -> tuple[timedelta, timedelta]:
    """(debounce, max wait) of the background refresh (M03 D60)."""
    return (
        timedelta(minutes=settings.summary_refresh_debounce_minutes),
        timedelta(hours=settings.summary_refresh_max_wait_hours),
    )


def build_owner_runtime(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    channel: ControlChannel,
    clock: Clock,
    llm: LLMClient | None = None,
    summarizer: Summarizer | None = None,
    *,
    drafter: Drafter | None = None,
    approval_cards: ApprovalCardPresenter | None = None,
    actions_config: ActionsConfig | None = None,
    notifications_enabled: bool = False,
) -> OwnerRequestRuntime:
    client = llm or build_llm_client(settings, session_factory, clock)
    return OwnerRequestRuntime(
        session_factory=session_factory,
        llm=client,
        channel=channel,
        registry=default_registry(),
        clock=clock,
        config=agent_config(settings),
        summarizer=summarizer or build_summarizer(settings, session_factory, clock, client),
        drafter=drafter,
        approval_cards=approval_cards,
        actions_config=actions_config,
        notifications_enabled=notifications_enabled,
    )


# --------------------------------------------------------------- the process
ROLE_NAMES = ("poller", "inbox", "worker", "system")


@dataclass
class _PhaseWiring:
    """Shared extension points; only the per-milestone blocks populate them."""

    listeners: list[IngestionListener] = field(default_factory=list)
    routes: ControlRoutes = field(default_factory=ControlRoutes)
    actions: ActionsJobHandlers | None = None
    inbox: InboxJobHandlers | None = None
    automation: AutomationJobHandlers | None = None
    reconcilers: list[Reconciler] = field(default_factory=list)
    actions_config: ActionsConfig | None = None
    drafter: Drafter | None = None
    approval_cards: ApprovalCardPresenter | None = None
    notifications_enabled: bool = False


def _wire_m04(
    wiring: _PhaseWiring,
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: OutboundGateway,
    channel: ControlChannel,
    clock: Clock,
    scheduler: ProcrastinateScheduler,
    process_run_id: str,
) -> None:
    """M04 units add their adapters here; F0 enables no feature behaviour."""
    wiring.actions_config = ActionsConfig(
        approval_max_age=timedelta(hours=settings.approval_max_age_hours),
        draft_debounce=timedelta(seconds=settings.draft_debounce_seconds),
        mark_uncertain_after=timedelta(seconds=settings.mark_uncertain_after_seconds),
        connection_stale_alert_after=timedelta(minutes=settings.connection_stale_alert_minutes),
        sending_enabled="actions.send" in settings.feature_flags,
        process_run_id=process_run_id,
        assist_draft_trigger=assist_trigger(settings.feature_flags),
        draft_route_model=settings.model_routes.model_for("draft_reply"),
    )


def _wire_m05(
    wiring: _PhaseWiring,
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    gateway: OutboundGateway,
    channel: ControlChannel,
    clock: Clock,
    scheduler: ProcrastinateScheduler,
) -> None:
    """M05 units add their adapters and listeners here after F0."""


class InstanceAlreadyRunningError(RuntimeError):
    """Another process holds the poller or inbox advisory lock."""


@asynccontextmanager
async def running_process(
    settings: Settings, roles: frozenset[str] = frozenset(ROLE_NAMES)
) -> AsyncIterator[Supervisor]:
    """Build and open everything `python -m assistant run` needs (ARCHITECTURE §4.1).

    Yields a supervisor ready to run; on exit, releases advisory locks and
    closes the job app, the Telegram session and the database engine.
    """
    unknown = roles - set(ROLE_NAMES)
    if unknown:
        raise ConfigError(f"unknown role(s): {', '.join(sorted(unknown))}")
    if settings.bot_token is None:
        raise ConfigError("BOT_TOKEN is not set")

    clock = SystemClock()
    database_url = settings.database_url.get_secret_value()
    engine = build_engine(database_url)
    session_factory = build_session_factory(engine)
    bot = Bot(token=settings.bot_token.get_secret_value())
    gateway = OutboundGateway(AiogramSender(bot), clock)
    channel = GatewayControlChannel(gateway)
    jobs_app = build_app(database_url)
    scheduler = ProcrastinateScheduler(jobs_app)
    locks: list[AdvisoryLock] = []
    process_run_id = uuid4().hex

    try:
        # §15.6: a restored database must go through post-restore before any
        # role runs — otherwise actions and schedules from before the dump
        # would come back as executable.
        async with session_factory() as session, session.begin():
            await assert_not_restore_pending(session)
            # A caught-up value from another process must never permit a send.
            # Commit the reset before constructing or starting any role (R1).
            await write_state(
                session,
                "ingestion_barrier",
                {"process_run_id": process_run_id, "caught_up_at": None},
            )
        wiring = _PhaseWiring()
        _wire_m04(
            wiring,
            settings=settings,
            session_factory=session_factory,
            gateway=gateway,
            channel=channel,
            clock=clock,
            scheduler=scheduler,
            process_run_id=process_run_id,
        )
        _wire_m05(
            wiring,
            settings=settings,
            session_factory=session_factory,
            gateway=gateway,
            channel=channel,
            clock=clock,
            scheduler=scheduler,
        )
        debounce, max_wait = summary_timing(settings)
        if roles & {"worker", "system"}:
            alerts = OwnerAlerts(session_factory, channel, clock)
            llm = build_llm_client(settings, session_factory, clock)
            summarizer = build_summarizer(settings, session_factory, clock, llm)
            register_tasks(
                jobs_app,
                JobHandlers(
                    session_factory=session_factory,
                    clock=clock,
                    scheduler=scheduler,
                    owner_runtime=build_owner_runtime(
                        settings,
                        session_factory,
                        channel,
                        clock,
                        llm,
                        summarizer,
                        drafter=wiring.drafter,
                        approval_cards=wiring.approval_cards,
                        actions_config=wiring.actions_config,
                        notifications_enabled=wiring.notifications_enabled,
                    ),
                    summary_refresher=summarizer,
                    summaries_enabled=settings.summaries_enabled,
                    summary_debounce=debounce,
                    summary_max_wait=max_wait,
                    alerts=alerts,
                    monthly_budget_usd=settings.llm_monthly_budget_usd,
                    heartbeat_url=(
                        None
                        if settings.heartbeat_url is None
                        else settings.heartbeat_url.get_secret_value()
                    ),
                    backup_status_path=settings.backup_status_path,
                    backup_upload_required=settings.backup_remote is not None,
                    privacy_policy_url=settings.privacy_policy_url,
                    confirmation_applier=build_confirmation_applier(
                        settings,
                        session_factory,
                        channel,
                        clock,
                        drafter=wiring.drafter,
                        approval_cards=wiring.approval_cards,
                        actions_config=wiring.actions_config,
                        notifications_enabled=wiring.notifications_enabled,
                    ),
                    data_deleter=RequestedDeletions(session_factory, clock, channel),
                    actions=wiring.actions,
                    inbox=wiring.inbox,
                    automation=wiring.automation,
                    reconcilers=tuple(wiring.reconcilers),
                ),
            )
        async with jobs_app.open_async():
            supervisor_roles: list[Role] = []
            if "poller" in roles:
                lock = AdvisoryLock(engine, NAMESPACE_POLLER, bot.id)
                if not await lock.acquire():
                    raise InstanceAlreadyRunningError("another process is polling this bot")
                locks.append(lock)
                poller = Poller(
                    AiogramUpdateSource(bot), session_factory, clock, process_run_id=process_run_id
                )
                await poller.startup()
                supervisor_roles.append(PollerRole(poller, lock, clock).role())
            if "inbox" in roles:
                lock = AdvisoryLock(engine, NAMESPACE_INBOX, bot.id)
                if not await lock.acquire():
                    raise InstanceAlreadyRunningError("another inbox processor is running")
                locks.append(lock)
                control = ControlInboxHandlers(
                    ControlDispatcher(
                        gateway,
                        clock,
                        scheduler,
                        policy_url=settings.privacy_policy_url,
                        monthly_budget_usd=settings.llm_monthly_budget_usd,
                        routes=wiring.routes,
                    ),
                    gateway,
                    RefusalLimiter(clock),
                )
                owner_ids = frozenset(settings.owner_telegram_ids)
                registry = ConnectionRegistry(
                    session_factory,
                    clock,
                    AiogramConnectionLookup(bot),
                    scheduler,
                    owner_ids=owner_ids,
                )
                business = BusinessPipeline(
                    registry,
                    clock,
                    bot_id=bot.id,
                    listeners=wiring.listeners,
                    summaries=(
                        SummaryRefreshTrigger(scheduler, clock, debounce)
                        if settings.summaries_enabled
                        else None
                    ),
                )
                processor = InboxProcessor(
                    session_factory,
                    clock,
                    owner_ids=owner_ids,
                    bot_id=bot.id,
                    router=functools.partial(route_update, owner_ids=owner_ids, bot_id=bot.id),
                    handlers={
                        Route.CONTROL: control.control,
                        Route.REFUSED: control.refused,
                        Route.CONNECTION: registry.connection,
                        Route.BUSINESS: business.handle,
                        Route.IGNORED: business.on_ignored,
                    },
                    preparers={Route.BUSINESS: registry.prepare},
                    dead_letter_hooks={Route.BUSINESS: business.on_dead_letter},
                )
                supervisor_roles.append(InboxRole(processor, lock, clock).role())
            probe = worker_heartbeat_probe(session_factory)
            for spec in QUEUES:
                wanted = "system" if spec.name == QUEUE_SYSTEM else "worker"
                if wanted in roles:
                    supervisor_roles.append(
                        WorkerRole(
                            spec.name,
                            functools.partial(run_queue_worker, jobs_app, spec),
                            probe,
                            spec.shutdown_grace_seconds,
                        ).role()
                    )
            yield Supervisor(supervisor_roles)
    finally:
        for lock in locks:
            with contextlib.suppress(Exception):
                await lock.release()
        await bot.session.close()
        await engine.dispose()
