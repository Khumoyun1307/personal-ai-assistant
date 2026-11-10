"""Capacity waiting precedes the policy claim, not the Telegram request."""

from __future__ import annotations

import pytest

from assistant.accounts.models import BusinessConnection
from assistant.actions.executor import ExecuteStatus
from assistant.actions.models import Action
from assistant.actions.states import ActionState, StateReason
from assistant.core.delivery import BusinessSendReservation
from assistant.core.jobs import EXECUTE_TASK
from assistant.telegram.business_targets import BusinessTarget, BusinessTargetResolver
from assistant.telegram.gateway import (
    DISPATCH_NOT_STARTED,
    LOCAL_TARGET_REFUSED,
    MAX_THROTTLE_WAIT_SECONDS,
    OutboundGateway,
)
from tests.fakes.sender import FakeTelegramSender, network_error
from tests.integration.business_env import Env
from tests.integration.execution_env import (
    action,
    add_message_now,
    approved,
    caught_up,
    harness,
    seed,
    update_rows,
)

pytestmark = pytest.mark.integration


async def test_backlog_waits_without_claiming_and_requeues_only_not_started_work(env: Env) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    for _ in range(MAX_THROTTLE_WAIT_SECONDS + 1):
        assert (await h.execute(await approved(env, chat))).status is ExecuteStatus.SENT
    action_id = await approved(env, chat)
    calls = h.sender.business_calls

    waiting = await h.execute(action_id)

    assert waiting.status is ExecuteStatus.WAITING
    row = await action(env, action_id)
    assert row.state == ActionState.APPROVED.value
    assert row.attempt_id is None
    assert row.attempt_started_at is None
    assert waiting.redefer_at is not None
    assert any(item.task == EXECUTE_TASK for item in h.scheduler.deferred)
    assert h.sender.business_calls == calls
    assert h.channel.sends == []

    env.clock.set(waiting.redefer_at)
    h.sender.plain_script = [network_error()]
    assert (await h.execute(action_id)).status is ExecuteStatus.UNCERTAIN
    assert h.sender.business_calls == calls + 1
    assert (await h.execute(action_id)).status is ExecuteStatus.SETTLED
    assert h.sender.business_calls == calls + 1


async def test_a_wait_past_the_uncertainty_threshold_cannot_dispatch_late(env: Env) -> None:
    chat = await seed(env)
    await caught_up(env)
    pending: int | None = None

    async def stalled(seconds: float) -> None:
        assert pending is not None
        assert (await action(env, pending)).attempt_id is None
        env.clock.advance(seconds=h.config.mark_uncertain_after.total_seconds() + seconds)
        assert await h.executor.mark_uncertain() == 0

    h = harness(env, gateway_sleep=stalled)
    assert (await h.execute(await approved(env, chat))).status is ExecuteStatus.SENT
    pending = await approved(env, chat)
    outcome = await h.execute(pending)

    assert outcome.status is ExecuteStatus.WAITING
    assert (await action(env, pending)).state == ActionState.APPROVED.value
    assert (await action(env, pending)).attempt_id is None
    assert h.sender.business_calls == 1
    assert h.channel.sends == []


@pytest.mark.parametrize("change", ["owner_manual", "contact", "expired"])
async def test_policy_sees_changes_during_capacity_wait_before_claiming(
    env: Env, change: str
) -> None:
    chat = await seed(env)
    await caught_up(env)
    pending: int | None = None

    async def change_while_waiting(seconds: float) -> None:
        assert pending is not None
        # No executing attempt may exist while capacity is being awaited.
        assert (await action(env, pending)).attempt_id is None
        env.clock.advance(seconds=seconds)
        if change == "expired":
            await update_rows(env, Action, Action.id == pending, expires_at=env.clock.now())
        else:
            await add_message_now(env, chat, direction=change)

    h = harness(env, gateway_sleep=change_while_waiting)
    assert (await h.execute(await approved(env, chat))).status is ExecuteStatus.SENT
    pending = await approved(env, chat)
    outcome = await h.execute(pending)

    assert outcome.status is ExecuteStatus.DENIED
    expected = {
        "owner_manual": StateReason.TAKEOVER,
        "contact": StateReason.NEW_CONTACT_MESSAGE,
        "expired": StateReason.MAX_AGE,
    }
    assert outcome.reason is expected[change]
    assert (await action(env, pending)).attempt_id is None
    assert h.sender.business_calls == 1


@pytest.mark.parametrize("change", ["rotate", "disabled", "rights"])
async def test_connection_is_resolved_again_after_the_throttle_wait(env: Env, change: str) -> None:
    chat = await seed(env)
    sender = FakeTelegramSender()

    async def change_connection(seconds: float) -> None:
        env.clock.advance(seconds=seconds)
        fields: dict[str, object]
        if change == "rotate":
            fields = {"connection_id": "synthetic-after-wait"}
        elif change == "disabled":
            fields = {"is_enabled": False}
        else:
            fields = {"rights": []}
        await update_rows(
            env,
            BusinessConnection,
            BusinessConnection.account_id == chat.account_id,
            **fields,
        )

    gateway = OutboundGateway(
        sender,
        env.clock,
        sleep=change_connection,
        targets=BusinessTargetResolver(env.factory),
    )
    assert (
        await gateway.send_business_text(
            account_id=chat.account_id, conversation_id=chat.conversation_id, text="First"
        )
    ).ok
    result, used = await gateway.send_business_text_with_connection(
        account_id=chat.account_id, conversation_id=chat.conversation_id, text="After waiting"
    )

    if change == "rotate":
        assert result.ok
        assert used == "synthetic-after-wait"
        assert sender.plain_sends[-1]["business_connection_id"] == used
    else:
        assert result.error_code == LOCAL_TARGET_REFUSED
        assert not result.delivery_uncertain
        assert used is None
        assert sender.business_calls == 1


async def test_expired_slot_after_claim_is_definitely_not_started_and_may_retry(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat)
    h = harness(env)
    resolve = BusinessTargetResolver.resolve
    lookups = 0

    async def delayed_refresh(
        self: BusinessTargetResolver,
        account_id: int,
        conversation_id: int,
        *,
        needs_reply_right: bool = True,
    ) -> BusinessTarget | None:
        nonlocal lookups
        lookups += 1
        if lookups == 2:
            assert (await action(env, action_id)).state == ActionState.EXECUTING.value
            env.clock.advance(seconds=MAX_THROTTLE_WAIT_SECONDS + 1)
        return await resolve(self, account_id, conversation_id, needs_reply_right=needs_reply_right)

    monkeypatch.setattr(BusinessTargetResolver, "resolve", delayed_refresh)
    outcome = await h.execute(action_id)

    assert outcome.status is ExecuteStatus.RATE_LIMITED
    assert h.sender.business_calls == 0
    row = await action(env, action_id)
    assert row.state == ActionState.APPROVED.value
    assert row.error_code == DISPATCH_NOT_STARTED
    assert row.attempt_id is None
    assert h.channel.sends == []
    assert outcome.redefer_at is not None
    env.clock.set(outcome.redefer_at)
    assert (await h.execute(action_id)).status is ExecuteStatus.SENT
    assert h.sender.business_calls == 1


async def test_a_reservation_is_scoped_and_cannot_dispatch_twice(env: Env) -> None:
    mine = await seed(env)
    theirs = await seed(env)
    sender = FakeTelegramSender()
    gateway = OutboundGateway(sender, env.clock, targets=BusinessTargetResolver(env.factory))
    slot = await gateway.reserve_business_send(
        account_id=mine.account_id, conversation_id=mine.conversation_id
    )
    assert isinstance(slot, BusinessSendReservation)

    foreign, used = await gateway.send_business_text_with_connection(
        account_id=theirs.account_id,
        conversation_id=theirs.conversation_id,
        text="Foreign slot",
        reservation=slot,
    )
    assert foreign.error_code == DISPATCH_NOT_STARTED
    assert used is None
    assert sender.business_calls == 0

    first, _ = await gateway.send_business_text_with_connection(
        account_id=mine.account_id,
        conversation_id=mine.conversation_id,
        text="Synthetic",
        reservation=slot,
    )
    assert first.ok
    again, used = await gateway.send_business_text_with_connection(
        account_id=mine.account_id,
        conversation_id=mine.conversation_id,
        text="Duplicate slot",
        reservation=slot,
    )
    assert again.error_code == DISPATCH_NOT_STARTED
    assert used is None
    assert sender.business_calls == 1
