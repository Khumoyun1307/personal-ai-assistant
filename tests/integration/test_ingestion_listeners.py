"""P2 ingestion extension points remain inside the storing transaction."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.conversations.listeners import DeletionChange, MessageChange
from assistant.conversations.models import Conversation, Message
from tests.integration.business_env import (
    BOT_ID,
    CONTACT,
    business_message,
    connect,
    deleted_messages,
    deliver,
    environment,
    inbound,
)

pytestmark = pytest.mark.integration


@dataclass
class Listener:
    name: str
    seen: list[tuple[str, MessageChange | DeletionChange]]
    fail: bool = False
    sessions: list[AsyncSession] = field(default_factory=list)

    async def on_message(self, session: AsyncSession, change: MessageChange) -> None:
        self.sessions.append(session)
        self.seen.append((self.name, change))
        assert session.in_transaction()
        assert await session.scalar(select(Message.id).where(Message.id == change.message_id))
        if self.fail:
            raise ValueError("synthetic listener failure")

    async def on_deletion(self, session: AsyncSession, change: DeletionChange) -> None:
        self.sessions.append(session)
        self.seen.append((self.name, change))
        for message_id in change.message_ids:
            assert (
                await session.scalar(select(Message.text).where(Message.id == message_id)) is None
            )


async def test_listeners_run_in_order_inside_the_ingesting_transaction(migrated_url: str) -> None:
    seen: list[tuple[str, MessageChange | DeletionChange]] = []
    first, second = Listener("first", seen), Listener("second", seen)
    async with environment(migrated_url, listeners=(first, second)) as env:
        await connect(env)
        await deliver(env, business_message())
        assert [name for name, _ in seen] == ["first", "second"]
        assert first.sessions[0] is second.sessions[0]
        change = seen[0][1]
        assert isinstance(change, MessageChange)
        assert change.new_message
        assert change.content_changed
        assert not change.metadata_only


async def test_a_listener_error_fails_the_row_like_any_handler_error(migrated_url: str) -> None:
    listener = Listener("fails", [], fail=True)
    async with environment(migrated_url, listeners=(listener,)) as env:
        await connect(env)
        update = business_message()
        await deliver(env, update)
        async with env.factory() as session:
            assert await session.scalar(select(Message.id)) is None
        row = await inbound(env, update)
        assert row.status == "pending"
        assert row.attempts == 1


async def test_a_deletion_reaches_listeners_with_its_assistant_sent_ids(migrated_url: str) -> None:
    listener = Listener("records", [])
    async with environment(migrated_url, listeners=(listener,)) as env:
        await connect(env)
        await deliver(env, business_message(message_id=71))
        async with env.factory() as session, session.begin():
            row = await session.scalar(select(Message).where(Message.telegram_message_id == 71))
            assert row is not None
            row.direction = "assistant_sent"
            message_id = row.id
        await deliver(env, deleted_messages(message_ids=[71]))
        change = listener.seen[-1][1]
        assert isinstance(change, DeletionChange)
        assert change.message_ids == change.assistant_sent_ids == (message_id,)


async def test_ignored_and_rejected_updates_reach_no_listener(migrated_url: str) -> None:
    listener = Listener("records", [])
    async with environment(migrated_url, listeners=(listener,)) as env:
        await connect(env)
        await deliver(env, business_message(chat_id=BOT_ID))
        await deliver(env, business_message(connection_id="synthetic-foreign"))
        assert listener.seen == []


async def test_an_ignore_mode_message_reaches_listeners_as_metadata_only(migrated_url: str) -> None:
    listener = Listener("records", [])
    async with environment(migrated_url, listeners=(listener,)) as env:
        await connect(env)
        await deliver(env, business_message())
        async with env.factory() as session, session.begin():
            conversation = await session.scalar(
                select(Conversation).where(Conversation.business_chat_id == CONTACT)
            )
            assert conversation is not None
            conversation.mode = "ignore"
        await deliver(env, business_message(message_id=72))
        change = listener.seen[-1][1]
        assert isinstance(change, MessageChange)
        assert change.metadata_only
        assert not change.content_changed
        assert change.mode == "ignore"
