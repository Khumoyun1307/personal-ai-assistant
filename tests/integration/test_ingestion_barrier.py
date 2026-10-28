"""The ingestion barrier, bound to the process run (S04.4 §3.3; decisions R1).

No send is ever decided on state that lags behind updates Telegram already
holds: the poller of THIS process run must have drained Telegram's queue
(`caught_up_at` for its own run id) and no inbound row may be pending. A
barrier left by another run never counts. The real poller runs against
`FakeUpdateSource`; texts are synthetic.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update

from assistant.actions.executor import ExecuteStatus
from assistant.actions.policy_loader import KEY_INGESTION_BARRIER
from assistant.actions.states import ActionOrigin, ActionState, StateReason
from assistant.core.jobs import EXECUTE_TASK
from assistant.inbox.models import InboundUpdate
from assistant.telegram.ingress.poller import Poller
from assistant.telegram.ingress.writer import write_state
from tests.fakes.telegram import FakeUpdateSource, make_update
from tests.integration.business_env import (
    CONNECTION,
    CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    private_message,
)
from tests.integration.execution_env import (
    action,
    approved,
    barrier,
    caught_up,
    harness,
    inserted,
    owner_account,
    owner_chat,
    seed,
)

pytestmark = pytest.mark.integration

RUN = "synthetic-run-new"
OLD_RUN = "synthetic-run-old"
TEXT = "Synthetic approved reply"


async def reset(env: Env, run_id: str = RUN) -> None:
    """What `bootstrap.running_process` writes once it holds the poller lock."""
    async with env.factory() as session, session.begin():
        await write_state(
            session, KEY_INGESTION_BARRIER, {"process_run_id": run_id, "caught_up_at": None}
        )


async def poller(env: Env, source: FakeUpdateSource, *, run_id: str = RUN, **kw: Any) -> Poller:
    built = Poller(
        source,
        env.factory,
        env.clock,
        process_run_id=run_id,
        owner_ids=frozenset({OWNER}),
        **kw,
    )
    await built.startup()
    return built


async def process_all(env: Env) -> None:
    async with env.factory() as session, session.begin():
        await session.execute(
            update(InboundUpdate)
            .where(InboundUpdate.status == "pending")
            .values(status="processed")
        )


async def test_the_first_poll_after_start_uses_timeout_zero(env: Env) -> None:
    source = FakeUpdateSource(bot_id=env.bot_id)
    polling = await poller(env, source)

    await polling.poll_once()
    await polling.poll_once()

    assert [call["timeout"] for call in source.calls] == [0, 50]


async def test_sends_wait_until_the_poller_has_caught_up(env: Env) -> None:
    """A full batch (= `limit`) means Telegram may hold more: wait. A short
    batch drains it: caught up, in the batch's own transaction — and the
    batch's rows must still be processed before anything is sent."""
    chat = await seed(env)
    await reset(env)
    h = harness(env, process_run_id=RUN)
    action_id = await approved(env, chat, text=TEXT)
    source = FakeUpdateSource(
        [[make_update(1, chat_id=1), make_update(2, chat_id=1)], [make_update(3, chat_id=1)]],
        bot_id=env.bot_id,
    )
    polling = await poller(env, source, limit=2)

    await polling.poll_once()  # 2 of a limit of 2: maybe more queued
    await process_all(env)
    assert (await h.execute(action_id)).status is ExecuteStatus.WAITING
    assert (await barrier(env)) == {"process_run_id": RUN, "caught_up_at": None}

    await polling.poll_once()  # 1 < 2: drained
    assert (await barrier(env) or {}).get("caught_up_at") == env.clock.now().isoformat()
    assert (await h.execute(action_id)).status is ExecuteStatus.WAITING  # row 3 pending

    await process_all(env)
    assert (await h.execute(action_id)).status is ExecuteStatus.SENT
    assert len(h.sender.plain_sends) == 1


async def test_a_pending_inbound_row_makes_execution_wait_and_redefer_in_three_seconds(
    env: Env,
) -> None:
    """`inbox_drained` is a wait, never a denial (§9.3; P2-T06): any account's
    pending row holds every send, and the job comes back in 3 s."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    async with env.factory() as session, session.begin():
        session.add(
            InboundUpdate(bot_id=env.bot_id, update_id=123_456, kind="message", status="pending")
        )

    outcome = await h.execute(action_id)

    assert outcome.status is ExecuteStatus.WAITING
    row = await action(env, action_id)
    assert row.state == "approved"
    assert row.policy_decision is not None
    assert row.policy_decision["outcome"] == "wait"
    deferred = [item for item in h.scheduler.deferred if item.task == EXECUTE_TASK]
    assert [(item.schedule_at, item.lock, item.queueing_lock) for item in deferred] == [
        (env.clock.now() + timedelta(seconds=3), None, None)
    ]
    assert h.sender.business_calls == 0
    await process_all(env)
    assert (await h.execute(action_id)).status is ExecuteStatus.SENT


async def test_a_waiting_action_expires_at_its_expiry(env: Env) -> None:
    chat = await seed(env)
    await reset(env)  # this run never catches up
    h = harness(env, process_run_id=RUN)
    action_id = await approved(env, chat, text=TEXT)
    assert (await h.execute(action_id)).status is ExecuteStatus.WAITING

    row = await action(env, action_id)
    assert row.expires_at is not None
    env.clock.set(row.expires_at)
    outcome = await h.execute(action_id)

    assert (outcome.state, outcome.reason) == (ActionState.EXPIRED, StateReason.MAX_AGE)
    assert h.sender.business_calls == 0


async def test_restart_with_a_backlogged_takeover_and_pause_sends_nothing(env: Env) -> None:
    """An overdue approved reply and an autonomous one at restart, while
    Telegram still holds the owner's own message in that chat and a /pause:
    nothing is sent — the pause lands in the poller's transaction, the
    takeover through the inbox before Tx A may pass."""
    chat = await owner_chat(env)
    reply = await approved(env, chat, text=TEXT)
    autonomous = await inserted(env, chat, origin=ActionOrigin.DELEGATION_AUTO)
    env.clock.advance(hours=1)  # overdue: its job would run at once
    await reset(env)
    h = harness(env, process_run_id=RUN)
    backlog = [
        business_message(
            CONNECTION, from_id=OWNER, chat_id=CONTACT, message_id=12, date=T_BASE + 100
        ),
        private_message(OWNER, "/pause"),
    ]
    source = FakeUpdateSource([backlog], bot_id=env.bot_id)
    polling = await poller(env, source)

    assert (await h.execute(reply)).status is ExecuteStatus.WAITING  # not caught up
    await polling.poll_once()
    assert (await owner_account(env)).autonomy_paused  # the fast lane
    assert (await h.execute(reply)).status is ExecuteStatus.WAITING  # rows pending
    await env.processor.process_batch()

    first = await h.execute(reply)
    second = await h.execute(autonomous)

    assert (first.state, first.reason) == (ActionState.OUTDATED, StateReason.TAKEOVER)
    assert second.status is ExecuteStatus.SETTLED  # cancelled by the pause
    assert (await action(env, autonomous)).state_reason == "autonomy_paused"
    assert h.sender.business_calls == 0


async def test_a_worker_running_before_the_poller_reset_never_sends(env: Env) -> None:
    """R1, the blocking review finding: the previous run's key says caught
    up; the new run's worker starts before its poller resets the key. It
    must wait, never claim."""
    chat = await seed(env)
    await caught_up(env, run_id=OLD_RUN)
    h = harness(env, process_run_id=RUN)
    action_id = await approved(env, chat, text=TEXT)

    outcome = await h.execute(action_id)

    assert outcome.status is ExecuteStatus.WAITING
    assert (await action(env, action_id)).state == "approved"
    assert h.sender.business_calls == 0


async def test_a_process_without_the_poller_role_never_sends(env: Env) -> None:
    """R1: a process started without the poller role has a run id nobody ever
    marks caught up — even while another process's poller is caught up."""
    chat = await seed(env)
    await caught_up(env, run_id="synthetic-run-with-poller")
    h = harness(env, process_run_id="synthetic-run-worker-only")
    action_id = await approved(env, chat, text=TEXT)

    for _ in range(3):
        assert (await h.execute(action_id)).status is ExecuteStatus.WAITING
        env.clock.advance(seconds=3)
    assert h.sender.business_calls == 0


async def test_a_poller_never_marks_another_runs_barrier_caught_up(env: Env) -> None:
    """P2-T36: only the run that reset the key may mark it; a poller of
    another run (one that lost the lock race, say) leaves it alone."""
    await reset(env, run_id=OLD_RUN)
    source = FakeUpdateSource([[make_update(1, chat_id=1)], []], bot_id=env.bot_id)
    polling = await poller(env, source, run_id=RUN)

    await polling.poll_once()
    await polling.poll_once()

    assert await barrier(env) == {"process_run_id": OLD_RUN, "caught_up_at": None}
