"""The agent's control-chat channel speaks only as the assistant (§6 invariant 2)."""

from __future__ import annotations

from assistant.telegram.control.agent_channel import GatewayControlChannel
from assistant.telegram.gateway import OutboundGateway
from tests.fakes.clock import FakeClock
from tests.fakes.sender import FakeTelegramSender, network_error, telegram_error


async def test_replies_go_to_the_control_chat_without_a_business_connection() -> None:
    sender = FakeTelegramSender()
    channel = GatewayControlChannel(OutboundGateway(sender, FakeClock()))

    delivery = await channel.send(account_id=1, chat_id=55, text="Synthetic reply")

    assert delivery.ok
    assert delivery.message_id is not None
    assert sender.sends[0]["chat_id"] == 55
    assert sender.sends[0]["business_connection_id"] is None


async def test_typing_is_a_control_chat_action() -> None:
    sender = FakeTelegramSender()
    channel = GatewayControlChannel(OutboundGateway(sender, FakeClock()))

    await channel.typing(account_id=1, chat_id=55)

    assert sender.actions == [{"chat_id": 55, "action": "typing"}]


async def test_a_failed_send_is_reported_not_hidden() -> None:
    sender = FakeTelegramSender()
    sender.fail_with = RuntimeError("synthetic network failure")
    channel = GatewayControlChannel(OutboundGateway(sender, FakeClock()))

    delivery = await channel.send(account_id=1, chat_id=55, text="x")

    assert not delivery.ok
    assert delivery.error is not None


async def test_a_send_with_no_answer_is_uncertain() -> None:
    """A timeout may have reached the chat: callers must not resend it."""
    sender = FakeTelegramSender()
    sender.fail_with = TimeoutError("synthetic timeout")
    channel = GatewayControlChannel(OutboundGateway(sender, FakeClock()))

    delivery = await channel.send(account_id=1, chat_id=55, text="x")

    assert (delivery.ok, delivery.uncertain) == (False, True)


async def test_a_refused_send_is_not_uncertain() -> None:
    sender = FakeTelegramSender()
    sender.fail_with = telegram_error("Forbidden: bot was blocked by the user", status_code=403)
    channel = GatewayControlChannel(OutboundGateway(sender, FakeClock()))

    delivery = await channel.send(account_id=1, chat_id=55, text="x")

    assert (delivery.ok, delivery.uncertain) == (False, False)


async def test_a_bad_request_is_not_reported_as_uncertain() -> None:
    """Verification review, 2026-10-23: a real aiogram TelegramBadRequest
    whose description matched no rule was reported to the agent as
    "may have been sent"; Telegram refused it, so it was not."""
    sender = FakeTelegramSender()
    sender.fail_with = telegram_error("Bad Request: can't parse entities", status_code=400)
    channel = GatewayControlChannel(OutboundGateway(sender, FakeClock()))

    delivery = await channel.send(account_id=1, chat_id=55, text="x")

    assert (delivery.ok, delivery.uncertain) == (False, False)
    assert delivery.error == "bad_request"


async def test_flood_control_is_not_uncertain() -> None:
    sender = FakeTelegramSender()
    sender.fail_with = telegram_error("Too Many Requests", status_code=429, retry_after=3)
    channel = GatewayControlChannel(OutboundGateway(sender, FakeClock()))

    delivery = await channel.send(account_id=1, chat_id=55, text="x")

    assert (delivery.ok, delivery.uncertain) == (False, False)


async def test_a_network_error_or_server_error_is_uncertain() -> None:
    for error in (network_error(), telegram_error("Bad Gateway", status_code=502)):
        sender = FakeTelegramSender()
        sender.fail_with = error
        channel = GatewayControlChannel(OutboundGateway(sender, FakeClock()))

        delivery = await channel.send(account_id=1, chat_id=55, text="x")

        assert (delivery.ok, delivery.uncertain) == (False, True)
