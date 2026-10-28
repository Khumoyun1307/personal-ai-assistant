"""Harness for the S04.4 execution tests.

The real executor over the real Postgres (`business_env`), with
`FakeTelegramSender` behind the real gateway (its target resolver reads the
database), a recording approval presenter, the real `OwnerAlerts` over a
`FakeChannel`, and a scheduler that records defers. Actions are proposed and
approved through the real S04.1 functions unless a test needs a state those
cannot produce (an autonomous action, an over-long approved text). Texts are
synthetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from itertools import count
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry, BusinessConnection
from assistant.actions.config import ActionsConfig
from assistant.actions.executor import ActionExecutor
from assistant.actions.lifecycle import approve, sendable_length, text_hash
from assistant.actions.models import Action
from assistant.actions.policy_loader import KEY_INGESTION_BARRIER
from assistant.actions.proposals import propose
from assistant.actions.states import ActionKind, ActionOrigin, ActionState
from assistant.conversations.models import Contact, Conversation, Message
from assistant.db.models import SystemState
from assistant.inbox.notify import OwnerAlerts
from assistant.telegram.business_targets import BusinessTargetResolver
from assistant.telegram.gateway import OutboundGateway
from assistant.telegram.ingress.writer import write_state
from tests.fakes.approvals import RecordingApprovalPresenter
from tests.fakes.channel import FakeChannel
from tests.fakes.clock import FakeSleep
from tests.fakes.sender import FakeTelegramSender
from tests.integration.actions_env import CONFIG, DRAFT_TEXT, Chat, request, seed_chat
from tests.integration.business_env import Env

_ids = count(7_600_000_000)


@dataclass(frozen=True, slots=True)
class Deferred:
    task: str
    args: dict[str, Any]
    schedule_at: datetime | None
    lock: str | None
    queueing_lock: str | None


@dataclass
class RecordingScheduler:
    """`JobScheduler` that records every defer and queues nothing."""

    deferred: list[Deferred] = field(default_factory=list)

    async def defer(
        self,
        task: str,
        *,
        args: Any,
        lock: str | None = None,
        queueing_lock: str | None = None,
        schedule_at: datetime | None = None,
        connection: Any | None = None,
    ) -> int | None:
        self.deferred.append(Deferred(task, dict(args), schedule_at, lock, queueing_lock))
        return len(self.deferred)


@dataclass
class Harness:
    env: Env
    sender: FakeTelegramSender
    sleep: FakeSleep
    presenter: RecordingApprovalPresenter
    channel: FakeChannel
    scheduler: RecordingScheduler
    executor: ActionExecutor
    config: ActionsConfig

    async def execute(self, action_id: int) -> Any:
        return await self.executor.execute(action_id)


def harness(env: Env, *, config: ActionsConfig = CONFIG, **overrides: Any) -> Harness:
    sender = FakeTelegramSender()
    sleep = FakeSleep()
    gateway = OutboundGateway(
        sender, env.clock, sleep=sleep, targets=BusinessTargetResolver(env.factory)
    )
    presenter = RecordingApprovalPresenter()
    channel = FakeChannel()
    scheduler = RecordingScheduler()
    effective = replace(config, **overrides) if overrides else config
    executor = ActionExecutor(
        session_factory=env.factory,
        clock=env.clock,
        config=effective,
        sender=gateway,
        alerts=OwnerAlerts(env.factory, channel, env.clock),
        scheduler=scheduler,
        presenter=presenter,
    )
    return Harness(env, sender, sleep, presenter, channel, scheduler, executor, effective)


async def caught_up(env: Env, run_id: str = CONFIG.process_run_id) -> None:
    """The poller of `run_id` has drained Telegram's queue (decisions R1)."""
    async with env.factory() as session, session.begin():
        await write_state(
            session,
            KEY_INGESTION_BARRIER,
            {"process_run_id": run_id, "caught_up_at": env.clock.now().isoformat()},
        )


async def barrier(env: Env) -> dict[str, Any] | None:
    async with env.factory() as session:
        value = await session.scalar(
            select(SystemState.value).where(SystemState.key == KEY_INGESTION_BARRIER)
        )
        return None if value is None else dict(value)


async def seed(env: Env, **fields: Any) -> Chat:
    async with env.factory() as session, session.begin():
        return await seed_chat(session, env.clock.now(), **fields)


async def second_chat(env: Env, chat: Chat) -> Chat:
    """Another conversation of the same account, whose contact wrote an hour ago."""
    now = env.clock.now()
    async with env.factory() as session, session.begin():
        contact = Contact(account_id=chat.account_id, telegram_user_id=next(_ids))
        session.add(contact)
        await session.flush()
        conversation = Conversation(
            account_id=chat.account_id,
            contact_id=contact.id,
            business_chat_id=contact.telegram_user_id,
            mode="assist",
            last_contact_msg_at=now - timedelta(hours=1),
        )
        session.add(conversation)
        await session.flush()
        message = Message(
            account_id=chat.account_id,
            conversation_id=conversation.id,
            telegram_message_id=next(_ids),
            direction="contact",
            sent_at=now - timedelta(hours=1),
            content_type="text",
            text="Synthetic message",
        )
        session.add(message)
        await session.flush()
        return Chat(chat.account_id, conversation.id, message.id)


async def approved(env: Env, chat: Chat, *, text: str = DRAFT_TEXT, **fields: Any) -> int:
    """Propose (proposal-phase policy) and approve with the card's token."""
    async with env.factory() as session, session.begin():
        result = await propose(
            session, request(chat, text=text, **fields), clock=env.clock, config=CONFIG
        )
        assert result.state is ActionState.AWAITING_APPROVAL, result
        token = await session.scalar(
            select(Action.approval_token).where(Action.id == result.action_id)
        )
        assert token is not None
        outcome = await approve(
            session, account_id=chat.account_id, token=token, now=env.clock.now()
        )
        assert outcome.action_id == result.action_id
        return result.action_id


async def inserted(
    env: Env,
    chat: Chat,
    *,
    state: ActionState = ActionState.APPROVED,
    origin: ActionOrigin = ActionOrigin.DELEGATION_AUTO,
    text: str = DRAFT_TEXT,
    conversation_id: int | None = None,
) -> int:
    """An action row as only later milestones (or a corrupt row) make it:
    an autonomous one, or an approved text no proposal would accept."""
    now = env.clock.now()
    async with env.factory() as session, session.begin():
        row = Action(
            account_id=chat.account_id,
            conversation_id=conversation_id or chat.conversation_id,
            kind=ActionKind.SEND_MESSAGE.value,
            origin=origin.value,
            state=state.value,
            version=1,
            text=text,
            approved_text_hash=text_hash(text),
            base_message_id=chat.contact_message_id or None,
            shown_through_message_id=chat.contact_message_id or None,
            expires_at=now + timedelta(hours=2),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.flush()
        return row.id


async def action(env: Env, action_id: int) -> Action:
    async with env.factory() as session:
        row = await session.get(Action, action_id)
        assert row is not None
        return row


async def events(env: Env, action_id: int) -> list[AuditLogEntry]:
    async with env.factory() as session:
        rows = await session.scalars(
            select(AuditLogEntry)
            .where(AuditLogEntry.subject_type == "action", AuditLogEntry.subject_id == action_id)
            .order_by(AuditLogEntry.id)
        )
        return list(rows)


async def conversation(env: Env, conversation_id: int) -> Conversation:
    async with env.factory() as session:
        row = await session.get(Conversation, conversation_id)
        assert row is not None
        return row


async def connection(env: Env, account_id: int) -> BusinessConnection:
    async with env.factory() as session:
        row = await session.scalar(
            select(BusinessConnection).where(BusinessConnection.account_id == account_id)
        )
        assert row is not None
        return row


async def sent_rows(env: Env, conversation_id: int) -> list[Message]:
    async with env.factory() as session:
        rows = await session.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id, Message.direction == "assistant_sent"
            )
            .order_by(Message.id)
        )
        return list(rows)


async def add_message_now(
    env: Env, chat: Chat, *, direction: str, sent_at: datetime | None = None
) -> int:
    """A message ingested now; a contact one moves the reply window's anchor."""
    when = sent_at or env.clock.now()
    async with env.factory() as session, session.begin():
        row = Message(
            account_id=chat.account_id,
            conversation_id=chat.conversation_id,
            telegram_message_id=next(_ids),
            direction=direction,
            sent_at=when,
            content_type="text",
            text="Synthetic message",
        )
        session.add(row)
        if direction == "contact":
            await session.execute(
                update(Conversation)
                .where(Conversation.id == chat.conversation_id)
                .values(last_contact_msg_at=when)
            )
        await session.flush()
        return row.id


async def update_rows(env: Env, model: Any, where: Any, **values: Any) -> None:
    async with env.factory() as session, session.begin():
        await session.execute(update(model).where(where).values(**values))


async def count_rows(session: AsyncSession, model: Any, *where: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def text_length(text: str) -> int:
    return sendable_length(text)
