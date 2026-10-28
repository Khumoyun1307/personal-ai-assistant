"""Races between Tx A and the changes that must stop a send (S04.4 §5;
ARCHITECTURE §9.4 lock order).

Two sessions, sequenced by explicit `asyncio.Event`s and by observing real
lock waits in `pg_stat_activity` — never by sleeping for time. The invariant
asserted in each: the send is either claimed before the competing commit or
blocked; it is never claimed after it. Antony re-runs these independently.
The competing writes emulate their paths' lock steps (S04.2's ingestion
listener is not on this branch: Tx A's re-check is the guarantee tested).
Every event and task of a test belongs to its `Race`, which releases them
when the test ends, so a failing race fails instead of hanging. Synthetic
text.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from typing import Any

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
#: How long closing a race waits for its tasks to end once released, before
#: cancelling them. It only bounds a failing test; nothing is ordered by it.
CLOSE_DEADLINE_SECONDS = 20.0


class Race:
    """The competing transactions and executor runs of one test.

    `close` sets every event and waits for every task, cancelling what is
    still running after that: a test that fails midway (an assertion in
    `lock_waits`, say) then fails, instead of leaving a transaction idle
    with row locks that the env teardown's TRUNCATE waits on forever (Viro
    on #107).
    """

    def __init__(self) -> None:
        self._events: list[asyncio.Event] = []
        self._tasks: list[asyncio.Task[Any]] = []

    def event(self, *, is_set: bool = False) -> asyncio.Event:
        event = asyncio.Event()
        if is_set:
            event.set()
        self._events.append(event)
        return event

    def spawn[T](self, work: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        task = asyncio.create_task(work)
        self._tasks.append(task)
        return task

    async def reached(self, event: asyncio.Event, task: asyncio.Task[Any]) -> None:
        """Wait for `event`; if `task` ends first, fail with its error."""
        waiter = self.spawn(event.wait())
        await asyncio.wait({waiter, task}, return_when=asyncio.FIRST_COMPLETED)
        if not event.is_set():
            task.result()
            raise AssertionError("the competing transaction ended before taking its locks")

    async def close(self) -> None:
        for event in self._events:
            event.set()
        running = [task for task in self._tasks if not task.done()]
        if running:
            _, stuck = await asyncio.wait(running, timeout=CLOSE_DEADLINE_SECONDS)
            for task in stuck:
                task.cancel()
            await asyncio.gather(*stuck, return_exceptions=True)
        for task in self._tasks:
            if not task.cancelled():
                task.exception()  # retrieved: the test's own failure is what counts


@pytest.fixture
async def race(env: Env) -> AsyncIterator[Race]:
    """Torn down before `env`, so nothing holds a lock at its TRUNCATE."""
    race = Race()
    try:
        yield race
    finally:
        await race.close()


async def hold(env: Env, work: Work, locked: asyncio.Event, release: asyncio.Event) -> None:
    """One competing transaction: do `work` (it takes its locks), say so,
    then commit only when released."""
    async with env.factory() as session, session.begin():
        await work(session)
        locked.set()
        await release.wait()


async def blocking_the_action(
    env: Env, race: Race, action_id: int
) -> tuple[asyncio.Task[None], asyncio.Event]:
    """Hold the action row `FOR UPDATE` until the returned event is set, so
    Tx A stops at its last lock step while holding the account and the
    conversation `FOR SHARE`. Returns once the row is held."""

    async def lock(session: AsyncSession) -> None:
        await session.execute(select(Action.id).where(Action.id == action_id).with_for_update())

    locked, unblock = race.event(), race.event()
    task = race.spawn(hold(env, lock, locked, unblock))
    await race.reached(locked, task)
    return task, unblock


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


async def test_a_race_that_fails_midway_releases_every_lock(env: Env) -> None:
    """The harness itself (Viro on #107): a race test that fails while a
    competing transaction holds the action row and Tx A waits on it ends
    both when its race closes (what the `race` fixture does on any
    failure), and the row is free again — the test fails, never hangs."""
    h, _chat, action_id = await ready(env)
    race = Race()
    blocker, unblock = await blocking_the_action(env, race, action_id)
    run = race.spawn(h.execute(action_id))
    await lock_waits(env, 1)

    await race.close()  # never unblocked by the test: the race does it

    assert unblock.is_set()
    assert blocker.done()
    assert run.done()
    async with env.factory() as session, session.begin():
        await session.execute(
            select(Action.id).where(Action.id == action_id).with_for_update(nowait=True)
        )


async def test_takeover_committed_before_tx_a_blocks_the_send(env: Env, race: Race) -> None:
    h, chat, action_id = await ready(env)
    locked, release = race.event(), race.event()
    competing = race.spawn(hold(env, takeover(env, chat), locked, release))
    await race.reached(locked, competing)

    run = race.spawn(h.execute(action_id))
    await lock_waits(env, 1)  # Tx A waits for the conversation
    release.set()
    await competing
    outcome = await run

    assert (outcome.state, outcome.reason) == (ActionState.OUTDATED, StateReason.TAKEOVER)
    assert h.sender.business_calls == 0


async def test_takeover_waiting_on_tx_a_lands_after_the_claim_and_the_send_is_recorded_as_claimed_first(  # noqa: E501
    env: Env, race: Race
) -> None:
    h, chat, action_id = await ready(env)
    blocker, unblock = await blocking_the_action(env, race, action_id)
    run = race.spawn(h.execute(action_id))
    await lock_waits(env, 1)  # Tx A holds account + conversation, waits for the action

    seen: list[str] = []
    competing = race.spawn(
        hold(
            env,
            takeover(env, chat, then=action_state(action_id, seen)),
            race.event(),
            race.event(is_set=True),  # commits as soon as it has its lock
        )
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


async def test_pause_committed_before_tx_a_blocks_an_autonomous_action(
    env: Env, race: Race
) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    autonomous = await inserted(env, chat, origin=ActionOrigin.DELEGATION_AUTO)
    locked, release = race.event(), race.event()

    async def pause(session: AsyncSession) -> None:
        await autonomy.pause(
            session, account_id=chat.account_id, now=env.clock.now(), source="command"
        )

    competing = race.spawn(hold(env, pause, locked, release))
    await race.reached(locked, competing)
    run = race.spawn(h.execute(autonomous))
    await lock_waits(env, 1)  # Tx A waits for the account
    release.set()
    await competing
    outcome = await run

    assert outcome.status is ExecuteStatus.SETTLED
    row = await action(env, autonomous)
    assert (row.state, row.state_reason) == ("cancelled", "autonomy_paused")
    assert h.sender.business_calls == 0


async def test_pause_waiting_on_tx_a_never_finds_a_claim_made_after_it(
    env: Env, race: Race
) -> None:
    """The pause queues behind Tx A's account lock: any claim it sees was
    made before it; after its commit, no autonomous claim is possible."""
    h, chat, action_id = await ready(env)
    blocker, unblock = await blocking_the_action(env, race, action_id)
    run = race.spawn(h.execute(action_id))
    await lock_waits(env, 1)

    seen: list[str] = []

    async def pause(session: AsyncSession) -> None:
        await autonomy.pause(
            session, account_id=chat.account_id, now=env.clock.now(), source="command"
        )
        await action_state(action_id, seen)(session)

    competing = race.spawn(hold(env, pause, race.event(), race.event(is_set=True)))
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
    env: Env, race: Race
) -> None:
    h, chat, action_id = await ready(env)
    locked, release = race.event(), race.event()

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

    competing = race.spawn(hold(env, contact_writes, locked, release))
    await race.reached(locked, competing)
    run = race.spawn(h.execute(action_id))
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
    env: Env, race: Race
) -> None:
    # 1. The rights change holds the action first: Tx A waits, then finds it
    #    outdated and sends nothing.
    h, chat, first = await ready(env)
    locked, release = race.event(), race.event()

    async def change(session: AsyncSession) -> None:
        await rights_change(session, chat, env)

    competing = race.spawn(hold(env, change, locked, release))
    await race.reached(locked, competing)
    run = race.spawn(h.execute(first))
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
    blocker, unblock = await blocking_the_action(env, race, second)
    run = race.spawn(h.execute(second))
    await lock_waits(env, 1)

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

    competing = race.spawn(hold(env, change_back, race.event(), race.event(is_set=True)))
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
