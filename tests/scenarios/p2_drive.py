"""P2 journeys through production wiring, the durable inbox and registered jobs.

Only the clock, Telegram transport and model are fake. No worker, poller,
provider client or external API is started. Rows read here contain synthetic
test data in assistant_test, never the owner's databases.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from procrastinate import App
from sqlalchemy import select, text

from assistant.accounts.models import Account, AuditLogEntry
from assistant.actions.models import Action
from assistant.bootstrap import (
    _PhaseWiring,
    _wire_m04,
    build_confirmation_applier,
    build_owner_runtime,
    build_summarizer,
)
from assistant.config import Settings
from assistant.conversations.models import Message
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
from tests.integration.p2_factories import seed_action

FLAGS = READ_FLAGS | {
    "actions.send",
    "assist.drafts",
    "tool.propose_reply",
    "tool.propose_message",
    "tool.pause_autonomy",
    "tool.resume_autonomy",
    "tool.set_conversation_mode",
}
PROCESS_RUN = "synthetic-qa-s4-process"
DRAFT = "P2 synthetic reply for approval."
REPLACEMENT = "P2 exact replacement — retain punctuation!"


def draft_result(**changes: Any) -> dict[str, Any]:
    return {
        "language": "en",
        "needs_reply": True,
        "skip_reason": None,
        "revision_kind": "new",
        "text": DRAFT,
        "confidence": "medium",
        "uses_only_permitted_information": True,
        "sensitive_topic": False,
        "makes_commitment": False,
        "should_escalate": False,
        "escalation_reason": None,
        **changes,
    }


def owner_draft_result(**changes: Any) -> dict[str, Any]:
    """An owner-requested draft (`draft_owner`, PF-10): no veto fields."""
    return {
        "language": "en",
        "answers_message_id": None,
        "text": DRAFT,
        "confidence": "medium",
        "sensitive_topic": False,
        "makes_commitment": False,
        **changes,
    }


def revision_result(**changes: Any) -> dict[str, Any]:
    """A card revision (`draft_owner` mode `revise`, PF-13): the Edit button
    and `revise_draft` alike (A18)."""
    return owner_draft_result(**({"revision_kind": "instruction"} | changes))


@dataclass
class Scenario:
    env: Env
    fake: FakeLLM
    app: App
    handlers: JobHandlers
    wiring: _PhaseWiring
    account_id: int
    conversation_id: int

    async def rows(self) -> list[Action]:
        async with self.env.factory() as session:
            return list(
                (
                    await session.scalars(
                        select(Action)
                        .where(
                            Action.account_id == self.account_id,
                            Action.conversation_id == self.conversation_id,
                        )
                        .order_by(Action.id)
                    )
                ).all()
            )

    async def action(self, action_id: int | None = None) -> Action:
        rows = await self.rows()
        matches = rows if action_id is None else [a for a in rows if a.id == action_id]
        assert matches, "the journey must create an action"
        return matches[-1]

    async def events(self, action_id: int) -> list[str]:
        async with self.env.factory() as session:
            return list(
                (
                    await session.scalars(
                        select(AuditLogEntry.event)
                        .where(
                            AuditLogEntry.account_id == self.account_id,
                            AuditLogEntry.subject_type == "action",
                            AuditLogEntry.subject_id == action_id,
                        )
                        .order_by(AuditLogEntry.id)
                    )
                ).all()
            )

    async def seed(self, *, state: str = "awaiting_approval", **fields: Any) -> Action:
        """F0 seed for a state the UI cannot reach yet; never an execution fake."""
        async with transaction(self.env.factory) as uow:
            base = await uow.session.scalar(
                select(Message.id)
                .where(
                    Message.account_id == self.account_id,
                    Message.conversation_id == self.conversation_id,
                    Message.direction == "contact",
                )
                .order_by(Message.id.desc())
                .limit(1)
            )
            row = await seed_action(
                uow.session,
                account_id=self.account_id,
                conversation_id=self.conversation_id,
                state=state,
                **{
                    "shown_through_message_id": base,
                    "created_at": self.env.clock.now(),
                    "updated_at": self.env.clock.now(),
                    "expires_at": self.env.clock.now() + timedelta(hours=1),
                    "approval_token": "QAseed1234" if state == "awaiting_approval" else None,
                    "approved_text_hash": hashlib.sha256(
                        str(fields.get("text", DRAFT)).encode()
                    ).hexdigest()
                    if state == "approved"
                    else None,
                    "text": DRAFT,
                    "base_message_id": base,
                    **fields,
                },
            )
            if state == "approved":
                uow.session.add(
                    AuditLogEntry(
                        account_id=self.account_id,
                        at=self.env.clock.now(),
                        actor="owner",
                        event="action_approved",
                        subject_type="action",
                        subject_id=row.id,
                        details={"version": row.version},
                    )
                )
            if self.wiring.approval_cards is not None:
                self.wiring.approval_cards.present_after_commit(uow.session, row.id)
        return await self.action(row.id)

    async def owner(
        self,
        tool: str,
        *,
        forwarded: bool = False,
        said: str = "P2 synthetic owner request.",
        **values: Any,
    ) -> None:
        self.fake.queue(
            tool_response(("qa-call", tool, args(**values))),
            text_response("P2 synthetic owner-run completion."),
        )
        extra = (
            {
                "forward_origin": {
                    "type": "hidden_user",
                    "date": self.date,
                    "sender_user_name": "Synthetic source",
                }
            }
            if forwarded
            else {}
        )
        await ask(self.env, said, **extra)
        assert await run_queued(self, "agent.owner_request") == 1

    async def propose(self, *, dictated: bool = False) -> Action:
        if dictated:
            # The owner's own words carry the text (PF-12 provenance check).
            await self.owner(
                "propose_message",
                said=f"P2 synthetic owner request: {REPLACEMENT}",
                conversation_id=self.conversation_id,
                contact_id=None,
                text=REPLACEMENT,
                answered_message_id=None,
            )
            return await self.action()
        self.fake.parsed.append(owner_draft_result())
        await self.owner(
            "propose_reply",
            conversation_id=self.conversation_id,
            contact_id=None,
            mode="reply",
            answered_message_id=None,
            guidance=None,
            min_words=None,
            max_words=None,
        )
        return await self.action()

    @property
    def date(self) -> int:
        return int(self.env.clock.now().timestamp())

    def business_sends(self) -> list[dict[str, Any]]:
        # Include every transport recording, including future plain send/edit
        # methods. A new adapter method must not make negative assertions blind.
        return [
            call
            for recording in vars(self.env.sender).values()
            if isinstance(recording, list)
            for call in recording
            if isinstance(call, dict) and call.get("business_connection_id") is not None
        ]

    def final_card(self, row: Action) -> dict[str, Any]:
        edits = [e for e in self.env.sender.edits if e["message_id"] == row.card_message_id]
        assert edits, "the transition must update the displayed approval card"
        return edits[-1]

    def card_buttons(self, row: Action) -> list[dict[str, Any]]:
        markup = self.final_card(row)["reply_markup"]
        if hasattr(markup, "model_dump"):
            markup = markup.model_dump()
        assert isinstance(markup, dict), "the final card must have buttons"
        return [button for line in markup["inline_keyboard"] for button in line]

    def assert_copy_ready(self, row: Action) -> None:
        assert any(
            (b.get("copy_text") or {}).get("text") == row.text for b in self.card_buttons(row)
        )

    def control_text(self) -> str:
        return "\n".join(
            str(s["text"])
            for s in [*self.env.sender.sends, *self.env.sender.edits]
            if s.get("business_connection_id") is None
        )

    async def execute(self, action_id: int) -> None:
        assert "actions.execute" in self.app.tasks, "actions.execute not registered: awaiting S04.4"
        assert self.env.scheduler is not None
        await self.env.scheduler.defer("actions.execute", args={"action_id": action_id})
        await run_queued(self, "actions.execute")


@asynccontextmanager
async def m04_environment(url: str, *, flags: frozenset[str] = FLAGS) -> AsyncIterator[Scenario]:
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
        shared = {
            "drafter": wiring.drafter,
            "approval_cards": wiring.approval_cards,
            "actions_config": wiring.actions_config,
            # PF-13: `revise_draft` revises through the production RevisionJob.
            "card_reviser": wiring.card_reviser,
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
            yield Scenario(env, fake, app, handlers, wiring, account_id, conv.id)


async def run_queued(s: Scenario, task_name: str, *, limit: int | None = None) -> int:
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


async def advance(s: Scenario, seconds: float, *, periodic: tuple[str, ...] = ()) -> None:
    s.env.clock.advance(seconds=seconds)
    for task in periodic:
        assert task in s.app.tasks, f"periodic task not registered: {task}"
        await s.app.tasks[task].func(timestamp=s.date)


async def press_action(s: Scenario, action_id: int, op: str, *, token: str | None = None) -> None:
    row = await s.action(action_id)
    value = token or row.approval_token
    assert value, "a live action needs its approval token"
    await deliver(s.env, callback(f"act:{value}:{op}", message_id=row.card_message_id or 1))


async def approve(s: Scenario, action_id: int) -> None:
    await press_action(s, action_id, "ok")


async def edit(s: Scenario, action_id: int, value: str) -> None:
    from tests.integration.business_env import private_message

    await press_action(s, action_id, "ed")
    await deliver(s.env, private_message(OWNER, value))


async def reject(s: Scenario, action_id: int) -> None:
    await press_action(s, action_id, "rj")


async def entered_or_finished(task: asyncio.Task[Any], event: asyncio.Event) -> None:
    """Wait on events, with a watchdog only to report a hung test, not to order it."""
    waiter = asyncio.create_task(event.wait())
    try:
        await asyncio.wait_for(
            asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED), 10
        )
        if task.done():
            await task
        assert event.is_set(), "feature finished without entering the expected model/send barrier"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


def tool_outputs(s: Scenario) -> list[dict[str, Any]]:
    from assistant.llm.port import ToolResultItem

    return [
        json.loads(item.output)
        for request in s.fake.requests
        for item in request.items
        if isinstance(item, ToolResultItem)
    ]
