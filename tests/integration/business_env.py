"""Shared environment for business-traffic integration tests (S02.1, S02.2).

A real `InboxProcessor` wired as in `bootstrap.py` — control handlers, the
connection registry with its preparer, the business pipeline and its
dead-letter hook — over the real Postgres, with Telegram's
`getBusinessConnection` and the outbound sender faked. Payloads are synthetic
and shaped like aiogram's JSON dump (`from_user`, integer dates).
"""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import (
    Account,
    BusinessConnection,
    BusinessConnectionEvent,
    PendingInput,
)
from assistant.conversations.models import Contact, Conversation, CoverageGap, Message
from assistant.core.routing import Route
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.uow import transaction
from assistant.inbox.models import InboundUpdate
from assistant.inbox.processor import InboxProcessor
from assistant.jobs.app import build_app
from assistant.jobs.scheduler import ProcrastinateScheduler
from assistant.memory.summaries import SummaryRefreshTrigger
from assistant.telegram.business import BusinessPipeline
from assistant.telegram.connections import ConnectionRegistry
from assistant.telegram.control.dispatcher import ControlDispatcher
from assistant.telegram.control.inbox_handlers import ControlInboxHandlers, RefusalLimiter
from assistant.telegram.gateway import OutboundGateway
from assistant.telegram.ingress.writer import write_batch
from assistant.telegram.routing import route_update
from tests.fakes.clock import FakeClock
from tests.fakes.sender import FakeTelegramSender

OWNER = 7000000001
STRANGER = 7000000009
CONTACT = 7000000005
OTHER_CONTACT = 7000000006
BOT_ID = 8845134387
CONNECTION = "synthetic-conn-a"
NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
#: Telegram `date` values used by the builders, as Unix seconds.
T_BASE = 1792354000
RESET = (
    "TRUNCATE app.accounts, app.inbound_updates, app.system_state, "
    "app.foreign_connections CASCADE; "
    "TRUNCATE procrastinate.procrastinate_jobs CASCADE"
)

_ids = iter(range(1_000_000, 1_999_999))


def next_update_id() -> int:
    return next(_ids)


@dataclass
class FakeLookup:
    """`getBusinessConnection`, scripted."""

    known: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    fail: Exception | None = None

    async def get(self, connection_id: str) -> dict[str, Any] | None:
        self.calls.append(connection_id)
        if self.fail is not None:
            raise self.fail
        return self.known.get(connection_id)


@dataclass
class Env:
    factory: async_sessionmaker[AsyncSession]
    processor: InboxProcessor
    sender: FakeTelegramSender
    lookup: FakeLookup
    clock: FakeClock
    dispatcher: ControlDispatcher
    bot_id: int = BOT_ID
    #: The job scheduler the dispatcher defers with (for reconciler tests).
    scheduler: ProcrastinateScheduler | None = None
    #: The pipeline's summary-refresh trigger, when built with
    #: `summaries=True` (S03.2); tests may swap its scheduler.
    summary_trigger: SummaryRefreshTrigger | None = None


# ------------------------------------------------------------ payloads
def connection_body(
    *,
    user_id: int = OWNER,
    connection_id: str = CONNECTION,
    enabled: bool = True,
    rights: tuple[str, ...] | None = ("can_reply",),
    date: int = 1792350000,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": connection_id,
        "user": {"id": user_id, "is_bot": False, "first_name": "Synthetic"},
        "user_chat_id": user_id,
        "date": date,
        "is_enabled": enabled,
    }
    if rights is not None:
        body["rights"] = dict.fromkeys(rights, True)
    return body


def connection_update(**kwargs: Any) -> dict[str, Any]:
    return {"update_id": next(_ids), "business_connection": connection_body(**kwargs)}


def business_message(
    connection_id: str = CONNECTION,
    *,
    from_id: int = CONTACT,
    chat_id: int = CONTACT,
    message_id: int | None = None,
    date: int = T_BASE,
    message_text: str | None = "synthetic contact text",
    edit: bool = False,
    edit_date: int | None = None,
    first_name: str | None = "Synthetic",
    username: str | None = None,
    sender_is_bot: bool = False,
    extra: dict[str, Any] | None = None,
    chat_type: str = "private",
) -> dict[str, Any]:
    """A `business_message` (or `edited_business_message` with `edit=True`)."""
    update_id = next(_ids)
    chat: dict[str, Any] = {"id": chat_id, "type": chat_type}
    if first_name is not None:
        chat["first_name"] = first_name
    if username is not None:
        chat["username"] = username
    body: dict[str, Any] = {
        "message_id": message_id if message_id is not None else update_id % 100_000,
        "date": date,
        "chat": chat,
        "from_user": {"id": from_id, "is_bot": sender_is_bot, "first_name": "Synthetic"},
        "business_connection_id": connection_id,
    }
    if message_text is not None:
        body["text"] = message_text
    if edit:
        body["edit_date"] = edit_date if edit_date is not None else date + 60
    if extra:
        body.update(extra)
    kind = "edited_business_message" if edit else "business_message"
    return {"update_id": update_id, kind: body}


def deleted_messages(
    connection_id: str = CONNECTION, *, chat_id: int = CONTACT, message_ids: list[int]
) -> dict[str, Any]:
    return {
        "update_id": next(_ids),
        "deleted_business_messages": {
            "business_connection_id": connection_id,
            "chat": {"id": chat_id, "type": "private", "first_name": "Synthetic"},
            "message_ids": list(message_ids),
        },
    }


def private_message(
    sender_id: int, message_text: str, *, language_code: str | None = None
) -> dict[str, Any]:
    update_id = next(_ids)
    sender: dict[str, Any] = {"id": sender_id, "is_bot": False, "first_name": "Synthetic"}
    if language_code is not None:
        sender["language_code"] = language_code
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id % 100_000,
            "date": T_BASE,
            "chat": {"id": sender_id, "type": "private"},
            "from": sender,
            "text": message_text,
        },
    }


# ------------------------------------------------------------ the environment
@asynccontextmanager
async def environment(
    migrated_url: str,
    *,
    owner_ids: frozenset[int] = frozenset({OWNER}),
    bot_id: int = BOT_ID,
    summaries: bool = False,
) -> AsyncIterator[Env]:
    """The production wiring over a clean database, for one test.

    `summaries=True` wires the summary-refresh trigger, as production does
    with flag `summary.refresh` on (off by default, like production)."""
    engine = build_engine(migrated_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(RESET)
    factory = build_session_factory(engine)
    clock = FakeClock(NOW)
    sender = FakeTelegramSender()
    gateway = OutboundGateway(sender, clock)
    app = build_app(migrated_url)

    @app.task(name="agent.owner_request", queue="llm")
    async def owner_request(turn_id: int) -> None:  # deferred only
        return None

    @app.task(name="connections.notify_owner", queue="system")
    async def notify_owner(event_id: int) -> None:  # deferred only
        return None

    @app.task(name="agent.apply_confirmation", queue="llm")
    async def apply_confirmation(confirmation_id: int) -> None:  # deferred only
        return None

    @app.task(name="memory.refresh_summary", queue="llm")
    async def refresh_summary(conversation_id: int) -> None:  # deferred only
        return None

    @app.task(name="privacy.delete_data", queue="llm")
    async def delete_data(
        account_id: int, scope: str, subject_id: int, card_id: int
    ) -> None:  # deferred only; tests run it with `run_deletion_jobs`
        return None

    scheduler = ProcrastinateScheduler(app)
    owners = owner_ids
    lookup = FakeLookup()
    registry = ConnectionRegistry(factory, clock, lookup, scheduler, owner_ids=owners)
    trigger = SummaryRefreshTrigger(scheduler, clock) if summaries else None
    pipeline = BusinessPipeline(registry, clock, bot_id=bot_id, summaries=trigger)
    dispatcher = ControlDispatcher(gateway, clock, scheduler)
    control = ControlInboxHandlers(dispatcher, gateway, RefusalLimiter(clock))
    processor = InboxProcessor(
        factory,
        clock,
        owner_ids=owners,
        bot_id=bot_id,
        router=functools.partial(route_update, owner_ids=owners, bot_id=bot_id),
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
    try:
        async with app.open_async():
            yield Env(
                factory, processor, sender, lookup, clock, dispatcher, bot_id, scheduler, trigger
            )
    finally:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(RESET)
        await engine.dispose()


async def deliver(env: Env, *updates: dict[str, Any]) -> None:
    async with transaction(env.factory) as uow:
        await write_batch(uow.session, bot_id=env.bot_id, updates=list(updates))
    await env.processor.process_batch()


def callback(
    data: str | None, *, from_id: int = OWNER, message_id: int | None = 1, chat_id: int = OWNER
) -> dict[str, Any]:
    """A `callback_query` (a button press on a control-chat card)."""
    body: dict[str, Any] = {
        "id": f"cb-{next_update_id()}",
        "from": {"id": from_id, "is_bot": False, "first_name": "Synthetic"},
        "chat_instance": "synthetic",
    }
    if data is not None:
        body["data"] = data
    if message_id is not None:
        body["message"] = {
            "message_id": message_id,
            "date": T_BASE,
            "chat": {"id": chat_id, "type": "private"},
        }
    return {"update_id": next_update_id(), "callback_query": body}


async def live_card(env: Env, kind: str) -> PendingInput:
    """The one open card of `kind` (fails if there is none or several)."""
    async with env.factory() as session:
        return (
            await session.execute(
                select(PendingInput)
                .where(PendingInput.kind == kind)
                .where(PendingInput.token.is_not(None))
            )
        ).scalar_one()


async def press(env: Env, kind: str, op: str, **kwargs: Any) -> dict[str, Any]:
    """Press `op` on the open card of `kind`, as the owner."""
    card = await live_card(env, kind)
    update = callback(f"{kind}:{card.token}:{op}", message_id=card.prompt_message_id, **kwargs)
    await deliver(env, update)
    return update


DELETE_DATA_TASK = "privacy.delete_data"


async def run_deletion_jobs(env: Env, channel: Any = None) -> int:
    """Run the queued button deletions (S03.5) as the worker would: in order,
    under the owner's control lock (one at a time). Returns how many ran."""
    async with env.factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, args FROM procrastinate.procrastinate_jobs "
                    "WHERE task_name = :t AND status = 'todo' ORDER BY id"
                ),
                {"t": DELETE_DATA_TASK},
            )
        ).all()
    if not rows:
        return 0
    from assistant.privacy.requests import RequestedDeletions
    from tests.fakes.channel import FakeChannel

    runner = RequestedDeletions(env.factory, env.clock, channel or FakeChannel())
    for job_id, job_args in rows:
        await runner.run(**job_args)
        async with env.factory() as session, session.begin():
            await session.execute(
                text("DELETE FROM procrastinate.procrastinate_jobs WHERE id = :id"),
                {"id": job_id},
            )
    return len(rows)


async def grant_managed(env: Env) -> None:
    """`/privacy managed`, then Turn on on its card (FR-ONB-05b)."""
    await deliver(env, private_message(OWNER, "/privacy managed"))
    await press(env, "managed_consent", "agree")


async def decline_managed(env: Env) -> None:
    """`/privacy managed`, then Not now on its card."""
    await deliver(env, private_message(OWNER, "/privacy managed"))
    await press(env, "managed_consent", "decline")


async def own_card(env: Env, kind: str, owner_id: int = OWNER) -> PendingInput | None:
    """This owner's open card of `kind`, if there is one (fails if several)."""
    async with env.factory() as session:
        return (
            await session.execute(
                select(PendingInput)
                .join(Account, Account.id == PendingInput.account_id)
                .where(Account.telegram_user_id == owner_id)
                .where(PendingInput.kind == kind)
                .where(PendingInput.token.is_not(None))
            )
        ).scalar_one_or_none()


async def press_own(env: Env, kind: str, op: str, owner_id: int = OWNER) -> dict[str, Any]:
    """Press `op` on this owner's open card of `kind`, as that owner."""
    card = await own_card(env, kind, owner_id)
    assert card is not None, f"no open {kind} card"
    update = callback(
        f"{kind}:{card.token}:{op}",
        from_id=owner_id,
        chat_id=owner_id,
        message_id=card.prompt_message_id,
    )
    await deliver(env, update)
    return update


#: The onboarding cards (M03 fix round C4).
LANGUAGE_CARD = "onb_lang"
TIMEZONE_CARD = "onb_tz"


async def onboard(env: Env, owner_id: int = OWNER, *, language: str = "en") -> None:
    """/start, a language on the picker, "yes" to the consent question that
    follows it, and UTC on the time-zone card (C4): an onboarded owner with
    no onboarding card left open. `en` keeps every fixed text as it was
    when the language was unset."""
    await deliver(env, private_message(owner_id, "/start"))
    await press_own(env, LANGUAGE_CARD, language, owner_id)
    await deliver(env, private_message(owner_id, "yes"))
    if await own_card(env, TIMEZONE_CARD, owner_id) is not None:
        await press_own(env, TIMEZONE_CARD, "utc", owner_id)


async def connect(env: Env, **kwargs: Any) -> None:
    """Onboard the owner and deliver a live connection update."""
    await onboard(env)
    await deliver(env, connection_update(**kwargs))


# ------------------------------------------------------------ readers
async def inbound(env: Env, update: dict[str, Any]) -> InboundUpdate:
    async with env.factory() as session:
        return (
            await session.execute(
                select(InboundUpdate).where(InboundUpdate.update_id == update["update_id"])
            )
        ).scalar_one()


async def payload_is_sql_null(env: Env, update: dict[str, Any]) -> bool:
    async with env.factory() as session:
        result = await session.execute(
            text("SELECT payload IS NULL FROM app.inbound_updates WHERE update_id = :id"),
            {"id": update["update_id"]},
        )
        return bool(result.scalar_one())


async def connection_row(env: Env) -> BusinessConnection | None:
    async with env.factory() as session:
        return (await session.execute(select(BusinessConnection))).scalar_one_or_none()


async def events(env: Env) -> list[BusinessConnectionEvent]:
    async with env.factory() as session:
        result = await session.execute(
            select(BusinessConnectionEvent).order_by(BusinessConnectionEvent.id)
        )
        return list(result.scalars().all())


async def notify_jobs(env: Env) -> list[int]:
    async with env.factory() as session:
        result = await session.execute(
            text(
                "SELECT (args->>'event_id')::bigint FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = 'connections.notify_owner' ORDER BY id"
            )
        )
        return [int(row[0]) for row in result.all()]


async def job_names(env: Env) -> list[str]:
    async with env.factory() as session:
        result = await session.execute(
            text("SELECT task_name FROM procrastinate.procrastinate_jobs ORDER BY id")
        )
        return [str(row[0]) for row in result.all()]


async def take_job(env: Env, conversation: int) -> None:
    """A worker picks the queued job up (`doing`), freeing its queueing lock.

    Until then a later defer of a refresh for the same chat is swallowed
    (`AlreadyEnqueued`, `sum:{id}`): a test that checks no refresh is queued
    must free the lock first, or the check cannot fail (C3 review)."""
    async with env.factory() as session, session.begin():
        worker = (
            await session.execute(
                text(
                    "INSERT INTO procrastinate.procrastinate_workers (last_heartbeat) "
                    "VALUES (now()) RETURNING id"
                )
            )
        ).scalar_one()
        await session.execute(
            text(
                "UPDATE procrastinate.procrastinate_jobs SET status = 'doing', worker_id = :w "
                "WHERE task_name = 'memory.refresh_summary' AND status = 'todo' "
                "AND args->>'conversation_id' = :c"
            ),
            {"w": worker, "c": str(conversation)},
        )


async def queued_refreshes(env: Env, conversation: int) -> int:
    """`memory.refresh_summary` jobs waiting (`todo`) for one chat."""
    async with env.factory() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM procrastinate.procrastinate_jobs "
                        "WHERE task_name = 'memory.refresh_summary' AND status = 'todo' "
                        "AND args->>'conversation_id' = :c"
                    ),
                    {"c": str(conversation)},
                )
            ).scalar_one()
        )


async def count(env: Env, model: type[Any]) -> int:
    async with env.factory() as session:
        return len((await session.execute(select(model))).scalars().all())


async def conversation(env: Env, chat_id: int = CONTACT) -> Conversation | None:
    async with env.factory() as session:
        return (
            await session.execute(
                select(Conversation).where(Conversation.business_chat_id == chat_id)
            )
        ).scalar_one_or_none()


async def contact(env: Env, telegram_user_id: int = CONTACT) -> Contact | None:
    async with env.factory() as session:
        return (
            await session.execute(
                select(Contact).where(Contact.telegram_user_id == telegram_user_id)
            )
        ).scalar_one_or_none()


async def messages(env: Env, chat_id: int = CONTACT) -> list[Message]:
    async with env.factory() as session:
        result = await session.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.business_chat_id == chat_id)
            .order_by(Message.id)
        )
        return list(result.scalars().all())


async def message(env: Env, telegram_message_id: int, chat_id: int = CONTACT) -> Message | None:
    for row in await messages(env, chat_id):
        if row.telegram_message_id == telegram_message_id:
            return row
    return None


async def gaps(env: Env) -> list[CoverageGap]:
    async with env.factory() as session:
        return list(
            (await session.execute(select(CoverageGap).order_by(CoverageGap.id))).scalars().all()
        )


async def control_texts(env: Env) -> list[str]:
    """What the owner saw in the control chat (synthetic, for assertions)."""
    return list(env.sender.texts)
