"""R1 #5: double taps, crashes, ambiguity and late results never duplicate."""

import asyncio
from datetime import timedelta

import pytest

from tests.fakes.sender import network_error, telegram_error
from tests.integration.business_env import callback, deliver
from tests.scenarios.p2_drive import Scenario, advance, run_queued

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("one_batch", [False, True])
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S04.3/S04.4")
async def test_a_double_tap_on_approve_sends_once(p2: Scenario, one_batch: bool) -> None:
    row = await p2.seed()
    taps = [callback(f"act:{row.approval_token}:ok") for _ in range(2)]
    if one_batch:
        await deliver(p2.env, *taps)
    else:
        for tap in taps:
            await deliver(p2.env, tap)
    assert (await p2.action(row.id)).state == "approved"
    await run_queued(p2, "actions.execute")
    await p2.execute(row.id)  # a redelivered trigger remains a no-op
    assert (await p2.action(row.id)).state == "succeeded"
    assert len(p2.business_sends()) == 1
    assert (await p2.events(row.id)).count("action_approved") == 1


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S04.4")
async def test_a_restart_between_claim_and_send_is_uncertain_and_never_resent(p2: Scenario) -> None:
    row = await p2.seed(state="approved")

    async def crash_before_transport() -> None:
        assert (await p2.action(row.id)).state == "executing"  # committed in a separate session
        raise asyncio.CancelledError

    p2.env.sender.on_call = crash_before_transport
    with pytest.raises(asyncio.CancelledError):
        await p2.execute(row.id)
    p2.env.sender.on_call = None
    assert p2.business_sends() == []
    threshold = p2.wiring.actions_config.mark_uncertain_after.total_seconds()
    await advance(p2, threshold + 1, periodic=("system.mark_uncertain",))
    assert (await p2.action(row.id)).state == "delivery_uncertain"
    await p2.handlers.reconcile_pending()
    await run_queued(p2, "actions.execute")
    await p2.execute(row.id)
    assert (await p2.action(row.id)).state == "delivery_uncertain"
    assert p2.business_sends() == []
    assert "not resend" in p2.control_text().lower()


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S04.4")
async def test_a_network_timeout_on_send_is_uncertain_not_retried(p2: Scenario) -> None:
    row = await p2.seed(state="approved")
    p2.env.sender.fail_with = network_error()
    await p2.execute(row.id)
    p2.env.sender.fail_with = None
    assert (await p2.action(row.id)).state == "delivery_uncertain"
    await p2.handlers.reconcile_pending()
    await p2.execute(row.id)
    assert len(p2.business_sends()) == 1
    assert (await p2.action(row.id)).retry_at is None


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S04.4")
async def test_a_rate_limited_send_is_retried_after_retry_after_and_sent_once(p2: Scenario) -> None:
    row = await p2.seed(state="approved")
    p2.env.sender.fail_with = telegram_error("Too Many Requests", status_code=429, retry_after=120)
    await p2.execute(row.id)
    retry = await p2.action(row.id)
    assert retry.state == "approved"
    assert retry.retry_at is not None
    assert retry.retry_at >= p2.env.clock.now() + timedelta(seconds=120)
    p2.env.sender.fail_with = None
    await p2.execute(row.id)  # cannot bypass retry_at
    assert len(p2.business_sends()) == 1
    await advance(p2, 120)
    await p2.execute(row.id)
    final = await p2.action(row.id)
    assert final.state == "succeeded"
    assert final.sent_text_hash == final.approved_text_hash
    assert len(p2.business_sends()) == 2  # one explicit 429 refusal, one success
    events = await p2.events(row.id)
    assert events.count("action_claimed") == 2
    assert events.count("action_rate_limited") == events.count("action_succeeded") == 1


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S04.4")
async def test_a_late_result_after_mark_uncertain_settles_to_succeeded(p2: Scenario) -> None:
    row = await p2.seed(state="approved")
    entered = False

    async def late_transport() -> None:
        nonlocal entered
        if entered:  # control notice emitted by mark_uncertain must not recurse
            return
        entered = True
        assert (await p2.action(row.id)).state == "executing"
        threshold = p2.wiring.actions_config.mark_uncertain_after.total_seconds()
        await advance(p2, threshold + 1, periodic=("system.mark_uncertain",))
        assert (await p2.action(row.id)).state == "delivery_uncertain"

    p2.env.sender.on_call = late_transport
    await p2.execute(row.id)
    p2.env.sender.on_call = None
    assert entered
    assert (await p2.action(row.id)).state == "succeeded"
    assert len(p2.business_sends()) == 1
    assert "action_late_result" in await p2.events(row.id)
