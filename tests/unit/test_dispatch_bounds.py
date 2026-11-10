"""Dispatch capacity is bounded; a local wait never becomes an uncertain send."""

from __future__ import annotations

import asyncio

import pytest

from assistant.core.delivery import SendResult
from assistant.telegram.gateway import (
    DISPATCH_NOT_STARTED,
    MAX_THROTTLE_WAIT_SECONDS,
    MIN_SECONDS_BETWEEN_ANY_SENDS,
    MIN_SECONDS_BETWEEN_SENDS,
    ControlChat,
    OutboundGateway,
)
from assistant.telegram.render import MAX_MESSAGE_CHARS
from tests.fakes.clock import FakeClock, FakeGatewayClock, FakeSleep
from tests.fakes.sender import FakeTelegramSender, network_error

TARGET = ControlChat(account_id=1, chat_id=584113992)


async def control_effect(gateway: OutboundGateway, operation: str) -> SendResult:
    if operation == "edit":
        return await gateway.edit_control_text(TARGET, 42, "Synthetic card")
    if operation == "keyboard":
        return await gateway.clear_control_keyboard(TARGET, 42)
    return await gateway.chat_action(TARGET)


@pytest.mark.parametrize("operation", ["edit", "keyboard", "typing"])
async def test_concurrent_late_control_effects_cannot_dispatch_together(operation: str) -> None:
    clock = FakeClock()
    sender = FakeTelegramSender()
    waiting: asyncio.Queue[float] = asyncio.Queue()
    release = asyncio.Event()

    async def hold(seconds: float) -> None:
        waiting.put_nowait(seconds)
        await release.wait()

    gateway = OutboundGateway(sender, clock, sleep=hold)
    assert (await gateway.send_text(TARGET, "Synthetic first send")).ok
    tasks = []
    try:
        for _ in range(2):
            tasks.append(asyncio.create_task(control_effect(gateway, operation)))
            async with asyncio.timeout(5):
                assert await waiting.get() > 0
        clock.advance(seconds=5)
    finally:
        release.set()
        results = await asyncio.gather(*tasks)

    assert sum(result.ok for result in results) == 1
    [refused] = [result for result in results if not result.ok]
    assert refused.error_code == DISPATCH_NOT_STARTED
    assert not refused.delivery_uncertain
    assert len(sender.edits) + len(sender.keyboard_edits) + len(sender.actions) == 1


@pytest.mark.parametrize("operation", ["edit", "keyboard", "typing"])
@pytest.mark.parametrize("same_chat", [True, False])
async def test_send_after_delayed_control_effect_respects_actual_start(
    operation: str, same_chat: bool
) -> None:
    clock = FakeClock()
    sender = FakeTelegramSender()
    waits: list[float] = []

    async def wait(seconds: float) -> None:
        waits.append(seconds)
        clock.advance(seconds=5 if len(waits) == 1 else seconds)

    gateway = OutboundGateway(sender, clock, sleep=wait)
    assert (await gateway.send_text(TARGET, "Synthetic first send")).ok
    assert (await control_effect(gateway, operation)).ok
    dispatched_at = clock.now()
    following = TARGET if same_chat else ControlChat(account_id=1, chat_id=TARGET.chat_id + 1)

    assert (await gateway.send_text(following, "Synthetic following send")).ok

    spacing = MIN_SECONDS_BETWEEN_SENDS if same_chat else MIN_SECONDS_BETWEEN_ANY_SENDS
    assert (clock.now() - dispatched_at).total_seconds() == spacing
    assert waits == [MIN_SECONDS_BETWEEN_SENDS, spacing]


@pytest.mark.parametrize("operation", ["edit", "keyboard", "typing"])
async def test_late_control_effect_cannot_extend_its_dispatch_deadline(operation: str) -> None:
    clock = FakeClock()
    sender = FakeTelegramSender()

    async def oversleep(seconds: float) -> None:
        clock.advance(seconds=MAX_THROTTLE_WAIT_SECONDS + seconds)

    gateway = OutboundGateway(sender, clock, sleep=oversleep)
    assert (await gateway.send_text(TARGET, "Synthetic first send")).ok

    refused = await control_effect(gateway, operation)

    assert refused.error_code == DISPATCH_NOT_STARTED
    assert not refused.delivery_uncertain
    assert sender.edits == []
    assert sender.keyboard_edits == []
    assert sender.actions == []


@pytest.mark.parametrize("microsecond", [0, 1, 123456, 999999])
@pytest.mark.parametrize("same_chat", [False, True])
async def test_first_send_and_exactly_spaced_following_send_dispatch(
    microsecond: int, same_chat: bool
) -> None:
    clock = FakeClock()
    clock.advance(microseconds=microsecond)
    started = clock.now()
    sender = FakeTelegramSender()
    sleep = FakeSleep(clock)
    gateway = OutboundGateway(sender, clock, sleep=sleep)
    following = TARGET if same_chat else ControlChat(account_id=1, chat_id=TARGET.chat_id + 1)

    assert (await gateway.send_text(TARGET, "Synthetic first send")).ok
    assert sleep.waits == []
    assert (await gateway.send_text(following, "Synthetic following send")).ok

    spacing = MIN_SECONDS_BETWEEN_SENDS if same_chat else MIN_SECONDS_BETWEEN_ANY_SENDS
    assert (clock.now() - started).total_seconds() == spacing
    assert sleep.waits == [spacing]
    assert len(sender.sends) == 2


async def test_a_noop_wait_cannot_replace_elapsed_dispatch_spacing() -> None:
    clock = FakeClock()
    sender = FakeTelegramSender()
    gateway = OutboundGateway(sender, clock, sleep=FakeSleep())

    assert (await gateway.send_text(TARGET, "Synthetic first send")).ok
    refused = await gateway.send_text(TARGET, "Synthetic premature send")

    assert refused.error_code == DISPATCH_NOT_STARTED
    assert not refused.delivery_uncertain
    assert len(sender.sends) == 1


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
    complete_waits = False

    async def wait(seconds: float) -> None:
        await sleep(seconds)
        if complete_waits:
            clock.advance(seconds=seconds)

    gateway = OutboundGateway(sender, clock, sleep=wait)
    assert (await gateway.send_text(TARGET, "Synthetic")).ok
    # Slots held by other callers have not started requests yet.
    for _ in range(MAX_THROTTLE_WAIT_SECONDS):
        assert await gateway._throttle(TARGET.chat_id)

    refused = await gateway.send_text(TARGET, "Too late")

    assert not refused.ok
    assert not refused.delivery_uncertain
    assert refused.error_code == "dispatch_not_started"
    assert len(sender.sends) == 1
    assert max(sleep.waits) == MAX_THROTTLE_WAIT_SECONDS
    # Refusal must not extend the backlog's reserved tail.
    clock.advance(seconds=1)
    complete_waits = True
    assert (await gateway.send_text(TARGET, "Synthetic retry")).ok
    assert sleep.waits[-1] == MAX_THROTTLE_WAIT_SECONDS
    assert len(sender.sends) == 2


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


async def test_concurrent_late_control_wakeups_cannot_dispatch_together() -> None:
    clock = FakeClock()
    sender = FakeTelegramSender()
    waiting: asyncio.Queue[float] = asyncio.Queue()
    release = asyncio.Event()

    async def hold(seconds: float) -> None:
        waiting.put_nowait(seconds)
        await release.wait()

    gateway = OutboundGateway(sender, clock, sleep=hold)
    assert (await gateway.send_text(TARGET, "Synthetic first send")).ok
    tasks = []
    try:
        for _ in range(2):
            tasks.append(asyncio.create_task(gateway.send_text(TARGET, "Synthetic queued send")))
            async with asyncio.timeout(5):
                await waiting.get()
        # Both sleeps return within their deadlines, but at the same instant.
        clock.advance(seconds=5)
    finally:
        release.set()
        results = await asyncio.gather(*tasks)

    assert sum(result.ok for result in results) == 1
    [refused] = [result for result in results if not result.ok]
    assert refused.error_code == "dispatch_not_started"
    assert not refused.delivery_uncertain
    assert len(sender.sends) == 2


async def test_partial_control_delivery_is_not_retried_as_not_started() -> None:
    sender = FakeTelegramSender()
    clock = FakeClock()

    async def oversleep(seconds: float) -> None:
        clock.advance(seconds=MAX_THROTTLE_WAIT_SECONDS + seconds)

    gateway = OutboundGateway(sender, clock, sleep=oversleep)

    result = await gateway.send_text(TARGET, "x" * (MAX_MESSAGE_CHARS + 1))

    assert result.delivery_uncertain
    assert result.error_code == "partial_delivery"
    assert len(sender.sends) == 1
