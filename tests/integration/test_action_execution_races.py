"""Races between Tx A and the changes that must stop a send (S04.4 §5;
ARCHITECTURE §9.4 lock order).

Two sessions, sequenced by explicit `asyncio.Event`s and by observing real
lock waits in `pg_stat_activity` — never by sleeping for time. The invariant
asserted in each: the send is either claimed before the competing commit or
blocked; it is never claimed after it. Antony re-runs these independently.
The competing writes emulate their paths' lock steps (S04.2's ingestion
listener is not on this branch: Tx A's re-check is the guarantee tested).
Synthetic text.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts import connections
from assistant.accounts.connections import ConnectionSnapshot, Source
from assistant.accounts.models import Account, BusinessConnection
from assistant.actions import autonomy
from assistant.actions.executor import ExecuteStatus
from assistant.actions.models import Action
from assistant.actions.states import ActionOrigin, ActionState, StateReason
from assistant.conversations.models import Conversation, Message
from tests.integration.actions_env import Chat
from tests.integration.business_env import Env
from tests.integration.execution_env import (
    Harness,
    action,
    approved,
    caught_up,
    harness,
    inserted,
    lock_waits,
    seed,
)

pytestmark = pytest.mark.integration

TEXT = "Synthetic approved reply"
Work = Callable[[AsyncSession], Awaitable[None]]


async def hold(env: Env, work: Work, locked: asyncio.Event, release: asyncio.Event) -> None:
    """One competing transaction: do `work` (it takes its locks), say so,
    then commit only when released."""
    async with env.factory() as session, session.begin():
        await work(session)
        locked.set()
        await release.wait()


async def blocking_the_action(
    env: Env, action_id: int, release: asyncio.Event
) -> asyncio.Task[None]:
    """Hold the action row `FOR UPDATE` until released, so Tx A stops at its
    last lock step while holding the account and the conversation
    `FOR SHARE`. Returns once the row is held."""

    async def lock(session: AsyncSession) -> None:
        await session.execute(select(Action.id).where(Action.id == action_id).with_for_update())

    locked = asyncio.Event()
    task = asyncio.create_task(hold(env, lock, locked, release))
    await locked.wait()
    return task


def action_state(action_id: int, seen: list[str]) -> Work:
    async def read(session: AsyncSession) -> None:
        seen.append(str(await session.scalar(select(Action.state).where(Action.id == action_id))))

    return read


async def add_owner_message(session: AsyncSession, chat: Chat, env: Env) -> None:
    session.add(
        Message(
            account_id=chat.account_id,
            conversation_id=chat.conversation_id,
            telegram_message_id=8_800_000_001,
            direction="owner_manual",
            sent_at=env.clock.now(),
            content_type="text",
            text="Synthetic owner message",
        )
    )
    await session.flush()


def takeover(env: Env, chat: Chat, then: Work | None = None) -> Work:
    """Ingestion of the owner's own message: the conversation `FOR UPDATE`
    first (§9.4 order), then the row."""

    async def work(session: AsyncSession) -> None:
        await session.execute(
            select(Conversation.id).where(Conversation.id == chat.conversation_id).with_for_update()
        )
        if then is not None:
            await then(session)
        await add_owner_message(session, chat, env)

    return work


async def ready(env: Env) -> tuple[Harness, Chat, int]:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    return h, chat, await approved(env, chat, text=TEXT)


async def test_takeover_committed_before_tx_a_blocks_the_send(env: Env) -> None:
    h, chat, action_id = await ready(env)
    locked, release = asyncio.Event(), asyncio.Event()
    competing = asyncio.create_task(hold(env, takeover(env, chat), locked, release))
    await locked.wait()

    run = asyncio.create_task(h.execute(action_id))
    await lock_waits(env, 1)  # Tx A waits for the conversation
    release.set()
    await competing
    outcome = await run

    assert (outcome.state, outcome.reason) == (ActionState.OUTDATED, StateReason.TAKEOVER)
    assert h.sender.business_calls == 0


async def test_takeover_waiting_on_tx_a_lands_after_the_claim_and_the_send_is_recorded_as_claimed_first(  # noqa: E501
    env: Env,
) -> None:
    h, chat, action_id = await ready(env)
    unblock = asyncio.Event()
    blocker = await blocking_the_action(env, action_id, unblock)
    run = asyncio.create_task(h.execute(action_id))
    await lock_waits(env, 1)  # Tx A holds account + conversation, waits for the action

    seen: list[str] = []
    go = asyncio.Event()
    go.set()  # commits as soon as it has its lock
    competing = asyncio.create_task(
        hold(env, takeover(env, chat, then=action_state(action_id, seen)), asyncio.Event(), go)
    )
    await lock_waits(env, 2)  # the takeover waits for Tx A's conversation lock
    unblock.set()
    await blocker
    outcome = await run
    await competing

    # The takeover got the conversation only after Tx A committed its claim.
    assert seen == [ActionState.EXECUTING.value]
    assert outcome.status is ExecuteStatus.SENT
    assert len(h.sender.plain_sends) == 1


async def test_pause_committed_before_tx_a_blocks_an_autonomous_action(env: Env) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    autonomous = await inserted(env, chat, origin=ActionOrigin.DELEGATION_AUTO)
    locked, release = asyncio.Event(), asyncio.Event()

    async def pause(session: AsyncSession) -> None:
        await autonomy.pause(
            session, account_id=chat.account_id, now=env.clock.now(), source="command"
        )

    competing = asyncio.create_task(hold(env, pause, locked, release))
    await locked.wait()
    run = asyncio.create_task(h.execute(autonomous))
    await lock_waits(env, 1)  # Tx A waits for the account
    release.set()
    await competing
    outcome = await run

    assert outcome.status is ExecuteStatus.SETTLED
    row = await action(env, autonomous)
    assert (row.state, row.state_reason) == ("cancelled", "autonomy_paused")
    assert h.sender.business_calls == 0


async def test_pause_waiting_on_tx_a_never_finds_a_claim_made_after_it(env: Env) -> None:
    """The pause queues behind Tx A's account lock: any claim it sees was
    made before it; after its commit, no autonomous claim is possible."""
    h, chat, action_id = await ready(env)
    unblock = asyncio.Event()
    blocker = await blocking_the_action(env, action_id, unblock)
    run = asyncio.create_task(h.execute(action_id))
    await lock_waits(env, 1)

    seen: list[str] = []

    async def pause(session: AsyncSession) -> None:
        await autonomy.pause(
            session, account_id=chat.account_id, now=env.clock.now(), source="command"
        )
        await action_state(action_id, seen)(session)

    go = asyncio.Event()
    go.set()
    competing = asyncio.create_task(hold(env, pause, asyncio.Event(), go))
    await lock_waits(env, 2)  # the pause waits for Tx A's account lock
    unblock.set()
    await blocker
    outcome = await run
    await competing

    # The pause got the account only after the claim committed (Tx B may
    # have finished meanwhile: it does not need the account lock).
    assert seen[0] in {ActionState.EXECUTING.value, ActionState.SUCCEEDED.value}
    assert outcome.status is ExecuteStatus.SENT  # an owner approval is not autonomy
    autonomous = await inserted(env, chat, origin=ActionOrigin.DELEGATION_AUTO)
    after = await h.execute(autonomous)
    assert (after.state, after.reason) == (ActionState.CANCELLED, StateReason.AUTONOMY_PAUSED)
    assert len(h.sender.plain_sends) == 1


async def test_a_contact_message_committed_between_approval_and_tx_a_outdates_the_action(
    env: Env,
) -> None:
    h, chat, action_id = await ready(env)
    locked, release = asyncio.Event(), asyncio.Event()

    async def contact_writes(session: AsyncSession) -> None:
        await session.execute(
            select(Conversation.id).where(Conversation.id == chat.conversation_id).with_for_update()
        )
        session.add(
            Message(
                account_id=chat.account_id,
                conversation_id=chat.conversation_id,
                telegram_message_id=8_800_000_002,
                direction="contact",
                sent_at=env.clock.now(),
                content_type="text",
                text="Synthetic contact message",
            )
        )
        await session.execute(
            update(Conversation)
            .where(Conversation.id == chat.conversation_id)
            .values(last_contact_msg_at=env.clock.now())
        )

    competing = asyncio.create_task(hold(env, contact_writes, locked, release))
    await locked.wait()
    run = asyncio.create_task(h.execute(action_id))
    await lock_waits(env, 1)
    release.set()
    await competing
    outcome = await run

    assert (outcome.state, outcome.reason) == (
        ActionState.OUTDATED,
        StateReason.NEW_CONTACT_MESSAGE,
    )
    assert h.sender.business_calls == 0


async def rights_change(session: AsyncSession, chat: Chat, env: Env) -> None:
    owner = await session.get(Account, chat.account_id)
    row = await session.scalar(
        select(BusinessConnection).where(BusinessConnection.account_id == chat.account_id)
    )
    assert owner is not None
    assert row is not None
    result = await connections.apply_snapshot(
        session,
        ConnectionSnapshot(
            connection_id=row.connection_id,
            user_id=owner.telegram_user_id,
            user_chat_id=row.user_chat_id,
            date=env.clock.now(),
            is_enabled=True,
            rights=frozenset({"can_reply", "can_read_messages"}),
            payload_digest="synthetic",
        ),
        owner_ids=frozenset({owner.telegram_user_id}),
        source=Source.UPDATE,
        clock=env.clock,
    )
    assert result.change is connections.Change.RIGHTS_CHANGED


async def test_a_rights_change_during_tx_a_waits_and_then_outdates_or_finds_it_claimed(
    env: Env,
) -> None:
    # 1. The rights change holds the action first: Tx A waits, then finds it
    #    outdated and sends nothing.
    h, chat, first = await ready(env)
    locked, release = asyncio.Event(), asyncio.Event()

    async def change(session: AsyncSession) -> None:
        await rights_change(session, chat, env)

    competing = asyncio.create_task(hold(env, change, locked, release))
    await locked.wait()
    run = asyncio.create_task(h.execute(first))
    await lock_waits(env, 1)
    release.set()
    await competing
    outcome = await run
    assert outcome.status is ExecuteStatus.SETTLED
    assert (await action(env, first)).state_reason == "rights_changed"
    assert h.sender.business_calls == 0

    # 2. Tx A is waiting for the action when the rights change starts: one
    #    of them settles it first. Either the claim came first (the change
    #    leaves a claimed action alone) or the action is outdated — it is
    #    never claimed after the change committed.
    second = await approved(env, chat, text=TEXT)
    unblock = asyncio.Event()
    blocker = await blocking_the_action(env, second, unblock)
    run = asyncio.create_task(h.execute(second))
    await lock_waits(env, 1)
    go = asyncio.Event()
    go.set()

    async def change_back(session: AsyncSession) -> None:
        # Rights back to the reply right only: another rights change.
        row = await session.scalar(
            select(BusinessConnection).where(BusinessConnection.account_id == chat.account_id)
        )
        owner = await session.get(Account, chat.account_id)
        assert row is not None
        assert owner is not None
        await connections.apply_snapshot(
            session,
            ConnectionSnapshot(
                connection_id=row.connection_id,
                user_id=owner.telegram_user_id,
                user_chat_id=row.user_chat_id,
                date=env.clock.now(),
                is_enabled=True,
                rights=frozenset({"can_reply"}),
                payload_digest="synthetic-2",
            ),
            owner_ids=frozenset({owner.telegram_user_id}),
            source=Source.UPDATE,
            clock=env.clock,
        )

    competing = asyncio.create_task(hold(env, change_back, asyncio.Event(), go))
    await lock_waits(env, 2)
    unblock.set()
    await blocker
    outcome = await run
    await competing

    row = await action(env, second)
    if outcome.status is ExecuteStatus.SENT:
        assert row.state == "succeeded"  # claimed first; the change left it alone
        assert len(h.sender.plain_sends) == 1
    else:
        assert (row.state, row.state_reason) == ("outdated", "rights_changed")
        assert h.sender.business_calls == 0
