"""Dispatch capacity is bounded; a local wait never becomes an uncertain send."""

from __future__ import annotations

import asyncio

from assistant.telegram.gateway import (
    MAX_THROTTLE_WAIT_SECONDS,
    MIN_SECONDS_BETWEEN_SENDS,
    ControlChat,
    OutboundGateway,
)
from assistant.telegram.render import MAX_MESSAGE_CHARS
from tests.fakes.clock import FakeClock, FakeGatewayClock, FakeSleep
from tests.fakes.sender import FakeTelegramSender, network_error

TARGET = ControlChat(account_id=1, chat_id=584113992)


async def test_gateway_waits_preserve_scenario_time_and_follow_explicit_advances() -> None:
    scenario_clock = FakeClock()
    started = scenario_clock.now()
    clock = FakeGatewayClock(scenario_clock)
    sender = FakeTelegramSender()
    gateway = OutboundGateway(sender, clock, sleep=clock.sleep)
    count = MAX_THROTTLE_WAIT_SECONDS + 3

    for _ in range(count):
        assert (await gateway.send_text(TARGET, "Synthetic control message")).ok

    assert len(sender.sends) == count
    assert clock.waits == [MIN_SECONDS_BETWEEN_SENDS] * (count - 1)
    assert scenario_clock.now() == started
    assert (clock.now() - started).total_seconds() == sum(clock.waits)

    scenario_clock.advance(days=1)
    assert clock.now() == scenario_clock.now()
    assert (await gateway.send_text(TARGET, "Synthetic after explicit advance")).ok
    assert len(clock.waits) == count - 1


async def test_completed_sequential_waits_do_not_accumulate_a_backlog() -> None:
    clock = FakeClock()
    started = clock.now()
    sender = FakeTelegramSender()
    sleep = FakeSleep(clock)
    gateway = OutboundGateway(sender, clock, sleep=sleep)
    count = MAX_THROTTLE_WAIT_SECONDS + 3

    for _ in range(count):
        assert (await gateway.send_text(TARGET, "Synthetic sequential send")).ok

    assert len(sender.sends) == count
    assert sleep.waits == [MIN_SECONDS_BETWEEN_SENDS] * (count - 1)
    assert (clock.now() - started).total_seconds() == sum(sleep.waits)

    sender.fail_with = network_error()
    uncertain = await gateway.send_text(TARGET, "Synthetic ambiguous send")
    assert uncertain.delivery_uncertain
    assert len(sender.sends) == count + 1


async def test_backlog_beyond_the_dispatch_bound_is_not_started() -> None:
    clock = FakeClock()
    sender = FakeTelegramSender()
    sleep = FakeSleep()
    gateway = OutboundGateway(sender, clock, sleep=sleep)
    for _ in range(MAX_THROTTLE_WAIT_SECONDS + 1):
        assert (await gateway.send_text(TARGET, "Synthetic")).ok

    refused = await gateway.send_text(TARGET, "Too late")

    assert not refused.ok
    assert not refused.delivery_uncertain
    assert refused.error_code == "dispatch_not_started"
    assert len(sender.sends) == MAX_THROTTLE_WAIT_SECONDS + 1
    assert max(sleep.waits) == MAX_THROTTLE_WAIT_SECONDS
    # Refusal must not extend the backlog's reserved tail.
    clock.advance(seconds=1)
    assert (await gateway.send_text(TARGET, "Synthetic retry")).ok


async def test_overslept_reservation_never_dispatches_but_a_network_error_is_uncertain() -> None:
    clock = FakeClock()
    sender = FakeTelegramSender()

    async def oversleep(seconds: float) -> None:
        clock.advance(seconds=MAX_THROTTLE_WAIT_SECONDS + seconds)

    gateway = OutboundGateway(sender, clock, sleep=oversleep)
    assert (await gateway.send_text(TARGET, "First")).ok
    refused = await gateway.send_text(TARGET, "Too late")
    assert refused.error_code == "dispatch_not_started"
    assert not refused.delivery_uncertain
    assert len(sender.sends) == 1

    clock.advance(seconds=MAX_THROTTLE_WAIT_SECONDS)
    sender.fail_with = network_error()
    uncertain = await gateway.send_text(TARGET, "Network outcome unknown")
    assert uncertain.delivery_uncertain
    assert len(sender.sends) == 2


async def test_waiting_sends_do_not_hold_a_lock_that_can_hide_the_backlog() -> None:
    clock = FakeClock()
    sender = FakeTelegramSender()
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def hold(seconds: float) -> None:
        waiting.set()
        await release.wait()

    gateway = OutboundGateway(sender, clock, sleep=hold)
    assert (await gateway.send_text(TARGET, "First")).ok
    tasks = []
    try:
        for _ in range(MAX_THROTTLE_WAIT_SECONDS):
            waiting.clear()
            tasks.append(asyncio.create_task(gateway.send_text(TARGET, "Queued")))
            async with asyncio.timeout(5):
                await waiting.wait()
        async with asyncio.timeout(5):
            refused = await gateway.send_text(TARGET, "Beyond capacity")
        assert refused.error_code == "dispatch_not_started"
        clock.advance(seconds=MAX_THROTTLE_WAIT_SECONDS + 1)
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert all(result.error_code == "dispatch_not_started" for result in results)
    assert len(sender.sends) == 1


async def test_partial_control_delivery_is_not_retried_as_not_started() -> None:
    sender = FakeTelegramSender()
    gateway = OutboundGateway(sender, FakeClock(), sleep=FakeSleep())
    for _ in range(MAX_THROTTLE_WAIT_SECONDS):
        assert (await gateway.send_text(TARGET, "Synthetic")).ok

    result = await gateway.send_text(TARGET, "x" * (MAX_MESSAGE_CHARS + 1))

    assert result.delivery_uncertain
    assert result.error_code == "partial_delivery"
    assert len(sender.sends) == MAX_THROTTLE_WAIT_SECONDS + 1
