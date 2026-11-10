"""Scripted tool arguments must not collide with the journey helper signature."""

import json

import pytest
from aiogram.exceptions import TelegramNetworkError

from tests.fakes.sender import FakeTelegramSender, network_error
from tests.journeys.harness import call


@pytest.mark.parametrize("key", ["name", "values", "tool_name", "tokens"])
def test_call_preserves_tool_argument_keywords(key: str) -> None:
    arguments = {key: "CANARY-SYNTH Kamola", "limit": None}

    response = call("resolve_contact", **arguments)

    [tool_call] = response.tool_calls
    assert tool_call.name == "resolve_contact"
    assert json.loads(tool_call.arguments) == arguments


async def test_presentation_failure_is_consumed_before_final_control_reply() -> None:
    sender = FakeTelegramSender()
    sender.control_script.append(network_error())

    with pytest.raises(TelegramNetworkError):
        await sender.send_message(chat_id=1, text="CANARY-SYNTH approval card")

    result = await sender.send_message(chat_id=1, text="CANARY-SYNTH awaiting approval")

    assert sender.control_script == []
    assert sender.fail_with is None
    assert len(sender.sends) == 2
    assert [item["message_id"] for item in sender.delivered_control_sends] == [result["message_id"]]


async def test_presentation_failure_does_not_consume_business_outcome() -> None:
    sender = FakeTelegramSender()
    sender.control_script.append(network_error())
    sender.plain_script.append(42)

    result = await sender.send_plain_message(
        chat_id=2, text="CANARY-SYNTH approved reply", business_connection_id="synthetic-connection"
    )

    assert result["message_id"] == 42
    assert len(sender.control_script) == 1
    assert sender.delivered_control_sends == []
