"""Each variant starts clean even when a preceding case leaves poisoned fakes."""

import pytest

from tests.fakes.llm import text_response
from tests.fakes.sender import network_error
from tests.integration.business_env import NOW
from tests.journeys.harness import Journey, call
from tests.scenarios.m05_drive import M05Scenario

pytestmark = [pytest.mark.integration, pytest.mark.journey]


def assert_clean_then_poison(j: Journey) -> None:
    sender = j.s.env.sender
    assert sender.fail_with is None
    assert sender.on_call is None
    assert sender.plain_script == []
    assert sender.control_script == []
    assert j.s.env.clock.now() == NOW
    assert j.s.env.lookup.fail is None
    assert j.s.fake.requests == []
    assert j.s.fake.parsed == []
    assert j.cards == {}
    assert j.turns == []
    assert call("get_status").tool_calls[0].call_id == "journey-1"

    async def poisoned_hook() -> None:
        raise AssertionError("transport hook leaked from an earlier journey")

    # Leave state dirty deliberately. A reset in the test body would conceal
    # fixture reuse, especially when a strict xfail aborts before cleanup.
    sender.fail_with = network_error()
    sender.on_call = poisoned_hook
    sender.plain_script.append(network_error())
    sender.control_script.append(network_error())
    j.s.env.clock.advance(days=7)
    j.s.env.lookup.fail = network_error()
    j.s.fake.queue(text_response("CANARY-SYNTH unconsumed response"))
    j.s.fake.parsed.append({"synthetic": True})


@pytest.mark.parametrize("trial", range(2))
async def test_owner_journey_fakes_start_clean(journey: Journey, trial: int) -> None:
    assert_clean_then_poison(journey)


@pytest.mark.parametrize("trial", range(2))
async def test_background_journey_fakes_start_clean(
    background: tuple[M05Scenario, Journey], trial: int
) -> None:
    _, journey = background
    assert_clean_then_poison(journey)
