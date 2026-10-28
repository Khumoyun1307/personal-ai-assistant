"""The S02 business environment with S04.2's drafting wired in (S04.2 part b).

`drafting_environment` adds what `_wire_m04` adds: the `ActionsIngestionListener`
on the real business pipeline, a job scheduler whose app knows `replies.draft`
(deferred only; tests run `AssistDraftJob.run` themselves), a `Drafter` over
the production LLM wiring with a scripted `FakeLLM`, and the recording
approval presenter (F0) in place of S04.3's cards. Texts are synthetic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select, text

from assistant.actions.config import ActionsConfig
from assistant.actions.ingestion import ActionsIngestionListener
from assistant.actions.models import Action
from assistant.agent.drafting import AssistDraftJob, DraftConfig, Drafter
from assistant.conversations import modes
from assistant.conversations.models import Conversation
from assistant.core.clock import Clock
from assistant.core.jobs import DRAFT_TASK
from assistant.db.uow import transaction
from assistant.jobs.app import build_app
from assistant.jobs.scheduler import ProcrastinateScheduler
from tests.fakes.approvals import RecordingApprovalPresenter
from tests.fakes.llm import FakeLLM
from tests.integration.actions_env import CONFIG
from tests.integration.agent_env import gated
from tests.integration.business_env import (
    BOT_ID,
    CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    connect,
    deliver,
    environment,
    grant_managed,
)
from tests.unit.test_draft_units import output

Trigger = Literal["off", "ingestion", "triage"]


@dataclass
class _ClockProxy:
    """The listener is built before the environment's FakeClock exists."""

    target: Clock | None = None

    def now(self) -> datetime:
        assert self.target is not None
        return self.target.now()


@dataclass
class DraftEnv:
    env: Env
    scheduler: ProcrastinateScheduler
    config: ActionsConfig
    fake: FakeLLM = field(default_factory=FakeLLM)
    presenter: RecordingApprovalPresenter = field(default_factory=RecordingApprovalPresenter)

    def drafter(self) -> Drafter:
        return Drafter(
            session_factory=self.env.factory,
            llm=gated(self.env, self.fake),
            clock=self.env.clock,
            config=DraftConfig(model=self.config.draft_route_model),
        )

    def job(self, config: ActionsConfig | None = None) -> AssistDraftJob:
        return AssistDraftJob(
            session_factory=self.env.factory,
            drafter=self.drafter(),
            scheduler=self.scheduler,
            clock=self.env.clock,
            config=config or self.config,
            presenter=self.presenter,
        )

    def script(self, *steps: Any) -> None:
        self.fake.parsed.extend(steps)


@asynccontextmanager
async def drafting_environment(
    migrated_url: str, *, trigger: Trigger = "ingestion", **config: Any
) -> AsyncIterator[DraftEnv]:
    app = build_app(migrated_url)

    @app.task(name=DRAFT_TASK, queue="llm")
    async def replies_draft(conversation_id: int, reason: str) -> None:  # deferred only
        return None

    scheduler = ProcrastinateScheduler(app)
    actions_config = replace(CONFIG, assist_draft_trigger=trigger, **config)
    presenter = RecordingApprovalPresenter()
    clock = _ClockProxy()
    listener = ActionsIngestionListener(
        config=actions_config, scheduler=scheduler, clock=clock, presenter=presenter
    )
    async with app.open_async(), environment(migrated_url, listeners=(listener,)) as env:
        clock.target = env.clock
        yield DraftEnv(env, scheduler, actions_config, presenter=presenter)


# ------------------------------------------------------------ seeding
async def seed_chat(
    denv: DraftEnv, *, mode: str = "assist", managed: bool = True, first: str = "SYNTH-HELLO"
) -> tuple[int, int]:
    """An onboarded, connected owner (with managed consent unless told not
    to) and one chat whose contact wrote `first`; then the chat's mode is
    set. Returns `(account_id, conversation_id)`."""
    env = denv.env
    await connect(env)
    if managed:
        await grant_managed(env)
    await deliver(env, business_message(message_id=1, message_text=first))
    async with transaction(env.factory) as uow:
        conversation = (
            await uow.session.execute(
                select(Conversation).where(Conversation.business_chat_id == CONTACT)
            )
        ).scalar_one()
        if mode != conversation.mode:
            await modes.set_mode(
                uow.session,
                account_id=conversation.account_id,
                conversation_id=conversation.id,
                mode=mode,
                actor="owner",
                clock=env.clock,
            )
        return conversation.account_id, conversation.id


_message_ids = iter(range(100, 100_000))


async def contact_says(denv: DraftEnv, words: str = "SYNTH-CONTACT", **kwargs: Any) -> int:
    """A new contact message; returns its Telegram message id."""
    message_id = kwargs.pop("message_id", next(_message_ids))
    await deliver(denv.env, business_message(message_id=message_id, message_text=words, **kwargs))
    return int(message_id)


async def owner_says(denv: DraftEnv, words: str = "SYNTH-OWNER", **kwargs: Any) -> int:
    """The owner's own message in the chat (owner_manual)."""
    message_id = kwargs.pop("message_id", next(_message_ids))
    await deliver(
        denv.env,
        business_message(from_id=OWNER, message_id=message_id, message_text=words, **kwargs),
    )
    return int(message_id)


async def owner_automated(denv: DraftEnv, words: str = "SYNTH-AWAY") -> int:
    message_id = next(_message_ids)
    await deliver(
        denv.env,
        business_message(
            from_id=OWNER,
            message_id=message_id,
            message_text=words,
            extra={"is_from_offline": True},
        ),
    )
    return message_id


async def assistant_sent(denv: DraftEnv, words: str = "SYNTH-SENT") -> int:
    """A message this bot sent in the chat, as Telegram echoes it."""
    message_id = next(_message_ids)
    await deliver(
        denv.env,
        business_message(
            from_id=OWNER,
            message_id=message_id,
            message_text=words,
            extra={"sender_business_bot": {"id": BOT_ID, "is_bot": True, "first_name": "Bot"}},
        ),
    )
    return message_id


def later(seconds: int) -> int:
    """A Telegram date `seconds` after the first synthetic message."""
    return T_BASE + seconds


# ------------------------------------------------------------ readers
async def draft_jobs(env: Env, conversation_id: int) -> list[dict[str, Any]]:
    """`replies.draft` jobs of one chat, oldest first."""
    async with env.factory() as session:
        rows = await session.execute(
            text(
                "SELECT id, status, args, queueing_lock, scheduled_at "
                "FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = :task AND args->>'conversation_id' = :c ORDER BY id"
            ),
            {"task": DRAFT_TASK, "c": str(conversation_id)},
        )
        return [dict(row._mapping) for row in rows.all()]


async def finish_draft_jobs(env: Env, conversation_id: int) -> None:
    """A worker took and finished the queued jobs (frees their queueing locks)."""
    async with env.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE procrastinate.procrastinate_jobs SET status = 'succeeded' "
                "WHERE task_name = :task AND args->>'conversation_id' = :c "
                "AND status IN ('todo', 'doing')"
            ),
            {"task": DRAFT_TASK, "c": str(conversation_id)},
        )


async def pending_flag(env: Env, conversation_id: int) -> datetime | None:
    async with env.factory() as session:
        flag: datetime | None = await session.scalar(
            select(Conversation.pending_draft_at).where(Conversation.id == conversation_id)
        )
        return flag


async def actions_of(env: Env, conversation_id: int) -> list[Action]:
    async with env.factory() as session:
        return list(
            await session.scalars(
                select(Action).where(Action.conversation_id == conversation_id).order_by(Action.id)
            )
        )


async def message_row_id(env: Env, telegram_message_id: int) -> int:
    async with env.factory() as session:
        found = await session.scalar(
            text(
                "SELECT id FROM app.messages WHERE telegram_message_id = :t "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"t": telegram_message_id},
        )
        assert found is not None
        return int(found)


__all__ = ["output"]
