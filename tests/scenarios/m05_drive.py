"""P2 journeys through production wiring, the durable inbox and registered jobs.

Only the clock, Telegram transport and model are fake. No worker, poller,
provider client or external API is started. Rows read here contain synthetic
test data in assistant_test, never the owner's databases.
"""

from __future__ import annotations

import functools
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from procrastinate import App
from sqlalchemy import select, text

from assistant.accounts.models import Account
from assistant.bootstrap import (
    _PhaseWiring,
    _wire_m04,
    _wire_m05,
    build_confirmation_applier,
    build_owner_runtime,
    build_summarizer,
)
from assistant.config import Settings
from assistant.db.uow import transaction
from assistant.inbox.notify import OwnerAlerts
from assistant.inbox.processor import InboxProcessor
from assistant.jobs.app import build_app
from assistant.jobs.scheduler import ProcrastinateScheduler
from assistant.jobs.tasks import JobHandlers, register_tasks
from assistant.privacy.requests import RequestedDeletions
from assistant.telegram.business import BusinessPipeline
from assistant.telegram.connections import ConnectionRegistry
from assistant.telegram.control.agent_channel import GatewayControlChannel
from assistant.telegram.control.dispatcher import ControlDispatcher
from assistant.telegram.control.inbox_handlers import ControlInboxHandlers, RefusalLimiter
from assistant.telegram.gateway import OutboundGateway
from assistant.telegram.ingress.writer import write_state
from assistant.telegram.routing import route_update
from tests.fakes.clock import FakeSleep
from tests.fakes.llm import FakeLLM, text_response, tool_response
from tests.integration.agent_env import READ_FLAGS, args, ask, gated
from tests.integration.business_env import (
    OWNER,
    Env,
    business_message,
    callback,
    connect,
    conversation,
    deliver,
    environment,
    grant_managed,
)

FLAGS = READ_FLAGS | {
    "tool.create_monitor",
    "tool.list_monitors",
    "tool.stop_monitor",
    "tool.propose_reply",
    "tool.set_conversation_mode",
    "assist.drafts",
    "tool.delete_conversation_data",
    "triage.run",
    "triage.assist_trigger",
    "notifications.deliver",
    "digest.daily",
    "monitors.run",
}
PROCESS_RUN = "synthetic-qa-s5-process"


@dataclass
class M05Scenario:
    env: Env
    fake: FakeLLM
    app: App
    handlers: JobHandlers
    wiring: _PhaseWiring
    account_id: int
    conversation_id: int
    contact_id: int
    settings: Settings

    @property
    def date(self) -> int:
        return int(self.env.clock.now().timestamp())

    async def rows(self, model: type[Any]) -> list[Any]:
        async with self.env.factory() as session:
            scope = (
                model.id == self.account_id
                if model is Account
                else model.account_id == self.account_id
            )
            return list(await session.scalars(select(model).where(scope).order_by(model.id)))

    def require(self, task: str) -> None:
        assert task in self.app.tasks, f"awaiting feature registration: {task}"

    async def job(self, task: str, **payload: Any) -> None:
        self.require(task)
        assert self.env.scheduler is not None
        await self.env.scheduler.defer(task, args=payload)
        assert await run_queued(self, task) >= 1

    async def owner(self, tool: str, *, forwarded: bool = False, **values: Any) -> None:
        if forwarded:
            self.fake.queue(
                tool_response(
                    (
                        "qa-read",
                        "get_conversation",
                        args(
                            conversation_id=self.conversation_id,
                            contact_id=None,
                            cursor=None,
                            limit=10,
                        ),
                    )
                )
            )
        self.fake.queue(
            tool_response(("qa-call", tool, args(**values))), text_response("Synthetic completion.")
        )
        await ask(self.env, "Synthetic P2 request.")
        assert await run_queued(self, "agent.owner_request") == 1

    async def message(
        self,
        number: int,
        text: str = "Synthetic contact request.",
        *,
        owner: bool = False,
        chat: int | None = None,
    ) -> int:
        from tests.integration.business_env import CONTACT, OWNER, message

        chat_id = CONTACT if chat is None else chat
        await deliver(
            self.env,
            business_message(
                message_id=number,
                date=self.date,
                message_text=text,
                from_id=OWNER if owner else chat_id,
                chat_id=chat_id,
            ),
        )
        row = await message(self.env, number, chat_id)
        assert row is not None
        return row.id

    async def preferences(self, **values: Any) -> None:
        async with transaction(self.env.factory) as uow:
            account = await uow.session.get(Account, self.account_id)
            assert account is not None
            if "timezone" in values:
                account.timezone = values.pop("timezone")
            account.settings = {**account.settings, **values}

    async def revoke(self) -> None:
        from tests.integration.business_env import private_message

        await deliver(self.env, private_message(OWNER, "/privacy revoke"))

    async def monitor(
        self, kind: str, *, confirm: bool = True, forwarded: bool = False, **condition: Any
    ) -> Any:
        from assistant.automation.models import Automation
        from tests.integration.business_env import live_card

        before = len(await self.rows(Automation))
        await self.owner(
            "create_monitor",
            forwarded=forwarded,
            scope={
                "contact_ids": [],
                "conversation_ids": [self.conversation_id],
                "all_observed": False,
            },
            condition={
                "kind": kind,
                "contact_id": self.contact_id if kind == "contact_replied" else None,
                "terms": [],
                "description": None,
                "media_kinds": [],
                **condition,
            },
            action="alert",
            lifecycle="one_shot" if kind == "contact_replied" else "standing",
            expires_at=None,
            bypass_quiet_hours=False,
        )
        from assistant.llm.port import ToolResultItem

        outputs = [
            json.loads(i.output)
            for r in self.fake.requests
            for i in r.items
            if isinstance(i, ToolResultItem)
        ]
        assert any(o.get("status") == "awaiting_confirmation" for o in outputs), (
            "monitor tool must produce an owner confirmation"
        )
        card = await live_card(self.env, "tool_confirm")
        assert len(await self.rows(Automation)) == before
        if not confirm:
            return card
        await deliver(
            self.env,
            callback(f"tool_confirm:{card.token}:confirm", message_id=card.prompt_message_id),
        )
        assert await run_queued(self, "agent.apply_confirmation") == 1
        rows = await self.rows(Automation)
        assert len(rows) == before + 1
        assert rows[-1].status == "active"
        return rows[-1]

    async def alert(
        self,
        *,
        key: str = "qa-alert",
        kind: str = "triage_decision",
        priority: str = "high",
        alert_class: str = "standard",
        conversation_id: int | None = None,
        action_ids: tuple[int, ...] = (),
    ) -> int | None:
        from assistant.inbox.notification_types import (
            AlertClass,
            NotificationKind,
            NotificationRefs,
            Priority,
        )
        from assistant.inbox.notifications import enqueue

        self.require("notify.deliver")
        conv = self.conversation_id if conversation_id is None else conversation_id
        async with transaction(self.env.factory) as uow:
            from assistant.inbox.models import ConversationTriage
            from tests.integration.p2_factories import seed_triage

            if (
                await uow.session.scalar(
                    select(ConversationTriage.id).where(
                        ConversationTriage.account_id == self.account_id,
                        ConversationTriage.conversation_id == conv,
                    )
                )
                is None
            ):
                await seed_triage(uow.session, account_id=self.account_id, conversation_id=conv)
            return await enqueue(
                uow.session,
                account_id=self.account_id,
                kind=NotificationKind(kind),
                priority=Priority(priority),
                alert_class=AlertClass(alert_class),
                dedupe_key=key,
                refs=NotificationRefs(
                    conversation_ids=(conv,),
                    contact_ids=(self.contact_id,),
                    action_ids=action_ids,
                    alert_kind="delivery_uncertain" if kind == "system" else None,
                ),
                clock=self.env.clock,
                conversation_id=conv,
                contact_id=self.contact_id,
                scheduler=self.env.scheduler,
            )

    def output(self, start: int = 0) -> str:
        return "\n".join(
            s["text"]
            for s in self.env.sender.sends[start:]
            if s.get("business_connection_id") is None
        )


def triage_result(**changes: Any) -> dict[str, Any]:
    return {
        "language": "en",
        "needs_reply": False,
        "contains_question_to_owner": False,
        "requests_decision": False,
        "urgency": "normal",
        "deadline": None,
        "commitments": [],
        "open_questions": [],
        "resolved_task_ids": [],
        "escalation_category": "none",
        "reasons": [],
        "script": "latin",
        **changes,
    }


async def triage(s: M05Scenario, result: dict[str, Any]) -> None:
    s.require("inbox.triage")
    s.fake.parsed.append(result)
    await advance(s, s.settings.triage_debounce_seconds + 1)
    assert await run_queued(s, "inbox.triage") == 1


@asynccontextmanager
async def m05_environment(url: str, *, flags: frozenset[str] = FLAGS) -> AsyncIterator[M05Scenario]:
    async with environment(url) as env:
        fake = FakeLLM()
        app = build_app(url)
        scheduler = ProcrastinateScheduler(app)
        gateway = OutboundGateway(env.sender, env.clock, sleep=FakeSleep())
        channel = GatewayControlChannel(gateway)
        settings = Settings(
            _env_file=None,
            database_url=url,
            bot_token=None,
            openai_api_key=None,
            owner_telegram_ids=(OWNER,),
            feature_flags=flags,
        )
        wiring = _PhaseWiring()
        client = gated(env, fake)
        _wire_m04(
            wiring,
            settings=settings,
            session_factory=env.factory,
            gateway=gateway,
            channel=channel,
            clock=env.clock,
            scheduler=scheduler,
            process_run_id=PROCESS_RUN,
            llm=client,
            roles=frozenset({"poller", "inbox", "worker", "system"}),
        )
        _wire_m05(
            wiring,
            settings=settings,
            session_factory=env.factory,
            gateway=gateway,
            channel=channel,
            clock=env.clock,
            scheduler=scheduler,
            llm=client,
            roles=frozenset({"poller", "inbox", "worker", "system"}),
        )
        shared = {
            "notifications_enabled": wiring.notifications_enabled,
            "drafter": wiring.drafter,
            "approval_cards": wiring.approval_cards,
            "actions_config": wiring.actions_config,
        }
        summaries = build_summarizer(settings, env.factory, env.clock, client)
        handlers = JobHandlers(
            session_factory=env.factory,
            clock=env.clock,
            scheduler=scheduler,
            owner_runtime=build_owner_runtime(
                settings, env.factory, channel, env.clock, client, summaries, **shared
            ),
            confirmation_applier=build_confirmation_applier(
                settings, env.factory, channel, env.clock, **shared
            ),
            summary_refresher=summaries,
            alerts=OwnerAlerts(env.factory, channel, env.clock),
            monthly_budget_usd=None,
            heartbeat_url=None,
            data_deleter=RequestedDeletions(env.factory, env.clock, channel),
            actions=wiring.actions,
            inbox=wiring.inbox,
            automation=wiring.automation,
            reconcilers=tuple(wiring.reconcilers),
        )
        register_tasks(app, handlers)
        owners = frozenset({OWNER})
        registry = ConnectionRegistry(
            env.factory, env.clock, env.lookup, scheduler, owner_ids=owners
        )
        pipeline = BusinessPipeline(
            registry, env.clock, bot_id=env.bot_id, listeners=wiring.listeners
        )
        env.dispatcher = ControlDispatcher(gateway, env.clock, scheduler, routes=wiring.routes)
        control = ControlInboxHandlers(env.dispatcher, gateway, RefusalLimiter(env.clock))
        # Same assembly as bootstrap, using the transport/database supplied by the fixture.
        from assistant.core.routing import Route

        env.processor = InboxProcessor(
            env.factory,
            env.clock,
            owner_ids=owners,
            bot_id=env.bot_id,
            router=functools.partial(route_update, owner_ids=owners, bot_id=env.bot_id),
            handlers={
                Route.CONTROL: control.control,
                Route.REFUSED: control.refused,
                Route.CONNECTION: registry.connection,
                Route.BUSINESS: pipeline.handle,
                Route.IGNORED: pipeline.on_ignored,
            },
            preparers={Route.BUSINESS: registry.prepare},
            dead_letter_hooks={Route.BUSINESS: pipeline.on_dead_letter},
        )
        env.scheduler = scheduler
        async with app.open_async():
            await connect(env)
            await grant_managed(env)
            await deliver(
                env, business_message(message_id=101, date=int(env.clock.now().timestamp()))
            )
            conv = await conversation(env)
            assert conv is not None
            async with transaction(env.factory) as uow:
                account_id = await uow.session.scalar(
                    select(Account.id).where(Account.telegram_user_id == OWNER)
                )
                await write_state(
                    uow.session,
                    "ingestion_barrier",
                    {
                        "process_run_id": PROCESS_RUN,
                        "caught_up_at": env.clock.now().isoformat(),
                    },
                )
            assert account_id is not None
            env.sender.sends.clear()
            env.sender.edits.clear()
            yield M05Scenario(
                env, fake, app, handlers, wiring, account_id, conv.id, conv.contact_id, settings
            )


async def run_queued(s: M05Scenario, task_name: str, *, limit: int | None = None) -> int:
    """Drain due jobs of one task in id order, invoking its registered body.

    Mark doing before invocation to release the todo-only queueing lock; a
    handler may legitimately defer its next debounce job. New jobs are picked
    up on the next iteration. A finite guard catches accidental endless defers.
    """
    completed = 0
    while limit is None or completed < limit:
        async with transaction(s.env.factory) as uow:
            row = (
                (
                    await uow.session.execute(
                        text(
                            "SELECT id, args FROM procrastinate.procrastinate_jobs "
                            "WHERE task_name = :task AND status = 'todo' "
                            "AND (scheduled_at IS NULL OR scheduled_at <= :now) "
                            "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
                        ),
                        {"task": task_name, "now": s.env.clock.now()},
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                break
            job_id, payload = row["id"], row["args"]
            await uow.session.execute(
                text("UPDATE procrastinate.procrastinate_jobs SET status = 'doing' WHERE id = :id"),
                {"id": job_id},
            )
        if task_name not in s.app.tasks:
            raise RuntimeError(f"queued task has no production registration: {task_name}")
        try:
            await s.app.tasks[task_name].func(**payload)
        except BaseException:
            async with transaction(s.env.factory) as uow:
                await uow.session.execute(
                    text(
                        "UPDATE procrastinate.procrastinate_jobs SET status = 'failed' "
                        "WHERE id = :id"
                    ),
                    {"id": job_id},
                )
            raise
        async with transaction(s.env.factory) as uow:
            await uow.session.execute(
                text(
                    "UPDATE procrastinate.procrastinate_jobs SET status = 'succeeded' "
                    "WHERE id = :id"
                ),
                {"id": job_id},
            )
        completed += 1
        if completed >= 100:
            raise RuntimeError(f"unbounded job drain: {task_name}")
    return completed


async def advance(s: M05Scenario, seconds: float, *, periodic: tuple[str, ...] = ()) -> None:
    s.env.clock.advance(seconds=seconds)
    for task in periodic:
        assert task in s.app.tasks, f"periodic task not registered: {task}"
        await s.app.tasks[task].func(timestamp=s.date)
