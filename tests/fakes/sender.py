"""A fake TelegramSender that records requests (AGENTS §11).

Records the exact keyword arguments each call was made with, so tests can
assert on what WOULD have gone to Telegram — in particular that a control
target never carries a `business_connection_id`.
"""

from __future__ import annotations

from typing import Any


class FakeTelegramSender:
    """Records sends; can be scripted to fail."""

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.answered_callbacks: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None
        self._next_message_id = 1000

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: Any | None = None,
        business_connection_id: str | None = None,
    ) -> dict[str, Any]:
        call = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
            "business_connection_id": business_connection_id,
        }
        self.sends.append(call)
        if self.fail_with is not None:
            raise self.fail_with
        self._next_message_id += 1
        return {"message_id": self._next_message_id, "chat": {"id": chat_id}}

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Any | None = None,
        business_connection_id: str | None = None,
    ) -> dict[str, Any]:
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "business_connection_id": business_connection_id,
            }
        )
        if self.fail_with is not None:
            raise self.fail_with
        return {"message_id": message_id}

    async def send_chat_action(self, *, chat_id: int, action: str) -> None:
        self.actions.append({"chat_id": chat_id, "action": action})

    async def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None, show_alert: bool = False
    ) -> None:
        self.answered_callbacks.append(
            {"id": callback_query_id, "text": text, "show_alert": show_alert}
        )

    @property
    def texts(self) -> list[str]:
        return [call["text"] for call in self.sends]


class FakeTelegramError(Exception):
    """Stands in for an aiogram error, carrying what the gateway reads."""

    def __init__(
        self, message: str, *, status_code: int | None = None, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after
