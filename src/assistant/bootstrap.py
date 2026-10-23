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

from aiogram import Bot
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.agent.persistence import DbCallRecorder, DbConsentReader
from assistant.agent.ports import ControlChannel
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime
from assistant.agent.tools.registry import default_registry
from assistant.config import ConfigError, Settings
from assistant.core.clock import Clock, SystemClock
from assistant.core.routing import Route
from assistant.core.supervisor import Role, Supervisor
from assistant.db.advisory_lock import NAMESPACE_INBOX, NAMESPACE_POLLER, AdvisoryLock
from assistant.db.engine import build_engine, build_session_factory
from assistant.inbox.notify import OwnerAlerts
from assistant.inbox.processor import InboxProcessor
from assistant.jobs.app import QUEUE_SYSTEM, QUEUES, build_app, run_queue_worker
from assistant.jobs.scheduler import ProcrastinateScheduler
from assistant.jobs.tasks import JobHandlers, register_tasks
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.llm.metering import MeteredLLMClient
from assistant.llm.openai_adapter import OpenAIClient
from assistant.llm.port import LLMClient
from assistant.privacy.post_restore import assert_not_restore_pending
from assistant.process import InboxRole, PollerRole, WorkerRole, worker_heartbeat_probe
from assistant.telegram.business import BusinessPipeline
from assistant.telegram.connections import AiogramConnectionLookup, ConnectionRegistry
from assistant.telegram.control.agent_channel import GatewayControlChannel
from assistant.telegram.control.dispatcher import ControlDispatcher
from assistant.telegram.control.inbox_handlers import ControlInboxHandlers, RefusalLimiter
from assistant.telegram.gateway import AiogramSender, OutboundGateway
from assistant.telegram.ingress.aiogram_source import AiogramUpdateSource
from assistant.telegram.ingress.poller import Poller
from assistant.telegram.routing import route_update

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
        model=settings.llm_default_model,
        effort=settings.llm_owner_request_effort,
        monthly_budget_usd=settings.llm_monthly_budget_usd,
        enabled_flags=settings.feature_flags,
    )


def build_owner_runtime(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    channel: ControlChannel,
    clock: Clock,
    llm: LLMClient | None = None,
) -> OwnerRequestRuntime:
    return OwnerRequestRuntime(
        session_factory=session_factory,
        llm=llm or build_llm_client(settings, session_factory, clock),
        channel=channel,
        registry=default_registry(),
        clock=clock,
        config=agent_config(settings),
    )


# --------------------------------------------------------------- the process
ROLE_NAMES = ("poller", "inbox", "worker", "system")


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

    try:
        # §15.6: a restored database must go through post-restore before any
        # role runs — otherwise actions and schedules from before the dump
        # would come back as executable.
        async with session_factory() as session:
            await assert_not_restore_pending(session)
        if roles & {"worker", "system"}:
            alerts = OwnerAlerts(session_factory, channel, clock)
            register_tasks(
                jobs_app,
                JobHandlers(
                    session_factory=session_factory,
                    clock=clock,
                    scheduler=scheduler,
                    owner_runtime=build_owner_runtime(settings, session_factory, channel, clock),
                    alerts=alerts,
                    monthly_budget_usd=settings.llm_monthly_budget_usd,
                    heartbeat_url=(
                        None
                        if settings.heartbeat_url is None
                        else settings.heartbeat_url.get_secret_value()
                    ),
                    backup_status_path=settings.backup_status_path,
                    backup_upload_required=settings.backup_remote is not None,
                ),
            )
        async with jobs_app.open_async():
            supervisor_roles: list[Role] = []
            if "poller" in roles:
                lock = AdvisoryLock(engine, NAMESPACE_POLLER, bot.id)
                if not await lock.acquire():
                    raise InstanceAlreadyRunningError("another process is polling this bot")
                locks.append(lock)
                poller = Poller(AiogramUpdateSource(bot), session_factory, clock)
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
                business = BusinessPipeline(registry, clock, bot_id=bot.id)
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
