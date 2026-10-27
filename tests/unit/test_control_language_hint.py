"""Telegram's `language_code` on a control-chat message (M03 fix round C4).

The inbox hands the sender's `language_code` to the dispatcher as a hint
that only orders the language picker's buttons. Only a string of sane length
passes; anything else is no hint. The business pipeline never calls this
handler (§6 invariant 5), so a contact's `language_code` cannot reach it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from assistant.telegram.control.handlers import IncomingMessage
from assistant.telegram.control.inbox_handlers import ControlInboxHandlers
from tests.fakes.clock import FakeClock
from tests.fakes.sender import FakeTelegramSender


class RecordingDispatcher:
    def __init__(self) -> None:
        self.messages: list[IncomingMessage] = []

    async def dispatch(self, session: Any, message: IncomingMessage) -> None:
        self.messages.append(message)


async def hint_of(sender: dict[str, Any], *, key: str = "from_user") -> str | None:
    from assistant.telegram.control.inbox_handlers import RefusalLimiter
    from assistant.telegram.gateway import OutboundGateway

    clock = FakeClock()
    dispatcher = RecordingDispatcher()
    handlers = ControlInboxHandlers(
        dispatcher,  # type: ignore[arg-type]
        OutboundGateway(FakeTelegramSender(), clock),
        RefusalLimiter(clock),
    )
    payload = {
        "message_id": 1,
        "chat": {"id": 5, "type": "private"},
        key: {"id": 5, "is_bot": False, "first_name": "Synthetic", **sender},
        "text": "/start",
    }
    row = SimpleNamespace(account_id=None, received_at=None, id=1)
    await handlers._message(None, row, payload)  # type: ignore[arg-type]
    [message] = dispatcher.messages
    return message.language_hint


@pytest.mark.parametrize("key", ["from_user", "from"])
async def test_the_senders_language_code_is_passed_as_a_hint(key: str) -> None:
    assert await hint_of({"language_code": "uz"}, key=key) == "uz"


@pytest.mark.parametrize(
    "sender",
    [{}, {"language_code": None}, {"language_code": 7}, {"language_code": "x" * 36}],
)
async def test_a_missing_or_odd_language_code_is_no_hint(sender: dict[str, Any]) -> None:
    assert await hint_of(sender) is None


def test_a_message_without_a_hint_has_none() -> None:
    message = IncomingMessage(
        account_id=0, chat_id=5, telegram_message_id=1, text="/start", from_id=5
    )
    assert message.language_hint is None
