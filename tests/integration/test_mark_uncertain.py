"""`system.mark_uncertain` (S04.4 §3.4; ARCHITECTURE §9.4 step 4).

An attempt that never reached Tx B (a crash, a send cut off) is settled as
`delivery_uncertain` — never retried — once it is certainly no longer in
flight, and the owner is told once. The same sweep raises the
connection-stale alert once per stale period. Synthetic text.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from assistant.accounts import connections
from assistant.accounts.models import AuditLogEntry, BusinessConnection
from assistant.actions.executor import ExecuteStatus, uncertain_after
from assistant.actions.models import Action
from assistant.actions.states import ActionState
from assistant.bootstrap import _PhaseWiring, _wire_m04
from assistant.config import Settings
from assistant.inbox.notify import ALERT_TEXT, AlertKind
from assistant.telegram.gateway import MAX_THROTTLE_WAIT_SECONDS, REQUEST_TIMEOUT_SECONDS
from tests.integration.actions_env import Chat
from tests.integration.business_env import Env
from tests.integration.execution_env import (
    action,
    approved,
    caught_up,
    harness,
    inserted,
    seed,
    update_rows,
)

pytestmark = pytest.mark.integration

FLOOR = timedelta(seconds=REQUEST_TIMEOUT_SECONDS + MAX_THROTTLE_WAIT_SECONDS + 30)


async def executing(env: Env, chat: Chat, *, started_ago: timedelta, attempt: str) -> int:
    action_id = await inserted(env, chat, state=ActionState.EXECUTING)
    await update_rows(
        env,
        Action,
        Action.id == action_id,
        attempt_id=attempt,
        attempt_started_at=env.clock.now() - started_ago,
    )
    return action_id


async def test_mark_uncertain_settles_only_stale_executing_rows(env: Env) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    stale = await executing(env, chat, started_ago=timedelta(minutes=3), attempt="a-old")
    fresh = await executing(env, chat, started_ago=timedelta(seconds=30), attempt="a-new")
    waiting = await approved(env, chat, text="Synthetic waiting reply")

    assert await h.executor.mark_uncertain() == 1
    assert await h.executor.mark_uncertain() == 0  # once per action

    row = await action(env, stale)
    assert (row.state, row.state_reason) == ("delivery_uncertain", "stale_executing")
    assert (await action(env, fresh)).state == "executing"
    assert (await action(env, waiting)).state == "approved"
    assert h.presenter.synced == [stale]
    alert = ALERT_TEXT[AlertKind.DELIVERY_UNCERTAIN].format(contact="this contact")
    assert h.channel.sends == [alert]
    # Never retried: a job for it settles at once and sends nothing.
    assert (await h.execute(stale)).status is ExecuteStatus.SETTLED
    assert h.sender.business_calls == 0


async def test_the_threshold_is_never_below_the_send_timeout_plus_margin(
    env: Env, clean_env: None
) -> None:
    """Whatever the setting says, an attempt is not marked while its request
    (20 s timeout) and throttle wait (10 s) may still be running, plus 30 s."""
    assert (
        uncertain_after(
            timedelta(seconds=1),
            request_timeout=timedelta(seconds=REQUEST_TIMEOUT_SECONDS),
            throttle_wait=timedelta(seconds=MAX_THROTTLE_WAIT_SECONDS),
        )
        == FLOOR
    )
    longer = timedelta(minutes=5)
    assert (
        uncertain_after(
            longer,
            request_timeout=timedelta(seconds=REQUEST_TIMEOUT_SECONDS),
            throttle_wait=timedelta(seconds=MAX_THROTTLE_WAIT_SECONDS),
        )
        == longer
    )

    # The wired configuration applies it (bootstrap `_wire_m04`).
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+psycopg://user:pw@db:5432/assistant",
        mark_uncertain_after_seconds=120,
    )
    wiring = _PhaseWiring()
    _wire_m04(
        wiring,
        settings=settings,
        session_factory=env.factory,
        gateway=None,  # type: ignore[arg-type]
        channel=None,  # type: ignore[arg-type]
        clock=env.clock,
        scheduler=None,  # type: ignore[arg-type]
        process_run_id="synthetic-run",
        llm=None,
        roles=frozenset({"poller", "inbox"}),
    )
    assert wiring.actions_config is not None
    assert wiring.actions_config.mark_uncertain_after >= FLOOR

    # And the sweep honours a floor-level threshold to the second.
    h = harness(env, mark_uncertain_after=FLOOR)
    chat = await seed(env)
    inside = await executing(env, chat, started_ago=FLOOR - timedelta(seconds=1), attempt="in")
    beyond = await executing(env, chat, started_ago=FLOOR + timedelta(seconds=1), attempt="out")
    await h.executor.mark_uncertain()
    assert (await action(env, inside)).state == "executing"
    assert (await action(env, beyond)).state == "delivery_uncertain"


async def test_connection_stale_alert_after_thirty_minutes_once_per_stale_period(
    env: Env,
) -> None:
    h = harness(env, connection_stale_alert_after=timedelta(minutes=30))
    chat = await seed(env)
    async with env.factory() as session, session.begin():
        assert await connections.mark_stale(session, chat.account_id, env.clock.now())

    env.clock.advance(minutes=29)
    await h.executor.mark_uncertain()
    assert h.channel.sends == []  # not yet

    env.clock.advance(minutes=2)
    await h.executor.mark_uncertain()
    await h.executor.mark_uncertain()
    expected = ALERT_TEXT[AlertKind.CONNECTION_STALE].format(minutes=30)
    assert h.channel.sends == [expected]  # once for this stale period

    # Reconnected, then stale again: a new period has its own alert.
    await update_rows(
        env, BusinessConnection, BusinessConnection.account_id == chat.account_id, status="active"
    )
    async with env.factory() as session, session.begin():
        assert await connections.mark_stale(session, chat.account_id, env.clock.now())
    env.clock.advance(minutes=31)
    await h.executor.mark_uncertain()
    assert h.channel.sends == [expected, expected]
    async with env.factory() as session:
        markings = await session.scalar(
            select(func.count())
            .select_from(AuditLogEntry)
            .where(AuditLogEntry.event == "connection_marked_stale")
        )
    assert markings == 2
