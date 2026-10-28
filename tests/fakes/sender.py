"""A fake TelegramSender that records requests (AGENTS §11).

Records the exact keyword arguments each call was made with, so tests can
assert on what WOULD have gone to Telegram — in particular that a control
target never carries a `business_connection_id`.

Failures are scripted with REAL aiogram exceptions (`telegram_error`,
`network_error`), never a look-alike: aiogram's exceptions carry no HTTP
status, and a fake that did once hid a classification bug that only showed
in production.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from http import HTTPStatus
from typing import Any

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramConflictError,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)


class FakeTelegramSender:
    """Records sends; can be scripted to fail."""

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []
        #: `editMessageReplyMarkup` calls (a card's buttons removed, text kept).
        self.keyboard_edits: list[dict[str, Any]] = []
        self.actions: list[dict[str, Any]] = []
        self.answered_callbacks: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None
        #: Awaited at the start of every call, to observe the world "while
        #: Telegram is being called" (e.g. which rows are locked).
        self.on_call: Callable[[], Awaitable[None]] | None = None
        self._next_message_id = 1000
        #: Plain-text business calls (S04.4): `sendMessage` and
        #: `editMessageText` as the owner, with every argument.
        self.plain_sends: list[dict[str, Any]] = []
        self.plain_edits: list[dict[str, Any]] = []
        #: Scripted outcomes of plain sends, consumed in order before
        #: `fail_with` applies: an exception is raised, an int is returned as
        #: the message id (0 = Telegram's "pending identity").
        self.plain_script: list[Exception | int] = []

    async def _observe(self) -> None:
        if self.on_call is not None:
            await self.on_call()

    async def send_plain_message(
        self, *, chat_id: int, text: str, business_connection_id: str
    ) -> dict[str, Any]:
        await self._observe()
        self.plain_sends.append(
            {"chat_id": chat_id, "text": text, "business_connection_id": business_connection_id}
        )
        if self.plain_script:
            scripted = self.plain_script.pop(0)
            if isinstance(scripted, Exception):
                raise scripted
            return {"message_id": scripted, "chat": {"id": chat_id}}
        if self.fail_with is not None:
            raise self.fail_with
        self._next_message_id += 1
        return {"message_id": self._next_message_id, "chat": {"id": chat_id}}

    async def edit_plain_message_text(
        self, *, chat_id: int, message_id: int, text: str, business_connection_id: str
    ) -> dict[str, Any]:
        await self._observe()
        self.plain_edits.append(
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

    @property
    def business_calls(self) -> int:
        """Every call that would act as the owner in a business chat."""
        return len(self.plain_sends) + len(self.plain_edits) + len(self.deletes)

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: Any | None = None,
        business_connection_id: str | None = None,
    ) -> dict[str, Any]:
        await self._observe()
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
        await self._observe()
        self.edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
                "business_connection_id": business_connection_id,
            }
        )
        if self.fail_with is not None:
            raise self.fail_with
        return {"message_id": message_id}

    async def edit_message_reply_markup(
        self,
        *,
        chat_id: int,
        message_id: int,
        reply_markup: Any | None = None,
        business_connection_id: str | None = None,
    ) -> dict[str, Any]:
        await self._observe()
        self.keyboard_edits.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": reply_markup,
                "business_connection_id": business_connection_id,
            }
        )
        if self.fail_with is not None:
            raise self.fail_with
        return {"message_id": message_id}

    async def send_chat_action(self, *, chat_id: int, action: str) -> None:
        self.actions.append({"chat_id": chat_id, "action": action})

    async def delete_business_messages(
        self, *, business_connection_id: str, message_ids: Sequence[int]
    ) -> bool:
        await self._observe()
        self.deletes.append(
            {"business_connection_id": business_connection_id, "message_ids": list(message_ids)}
        )
        if self.fail_with is not None:
            raise self.fail_with
        return True

    async def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None, show_alert: bool = False
    ) -> None:
        await self._observe()
        self.answered_callbacks.append(
            {"id": callback_query_id, "text": text, "show_alert": show_alert}
        )

    @property
    def texts(self) -> list[str]:
        return [call["text"] for call in self.sends]


class SyntheticMethod:
    """The failed Bot API method an aiogram exception carries.

    aiogram reads only its type name and `chat_id`. A real method object would
    need `aiogram.methods`, which only the Bot API modules may import (AGENTS
    §5); nothing here calls Telegram.
    """

    chat_id = 1


#: aiogram 3.31 `BaseSession.check_response`: the exception it raises for each
#: HTTP status Telegram answers with. 5xx is handled below.
_EXCEPTION_BY_STATUS: dict[int, type[TelegramAPIError]] = {
    HTTPStatus.BAD_REQUEST: TelegramBadRequest,
    HTTPStatus.NOT_FOUND: TelegramNotFound,
    HTTPStatus.CONFLICT: TelegramConflictError,
    HTTPStatus.UNAUTHORIZED: TelegramUnauthorizedError,
    HTTPStatus.FORBIDDEN: TelegramForbiddenError,
    HTTPStatus.REQUEST_ENTITY_TOO_LARGE: TelegramEntityTooLarge,
}


def telegram_error(
    description: str, *, status_code: int, retry_after: int | None = None
) -> TelegramAPIError:
    """The exception aiogram raises when Telegram answers `ok: false`.

    Built the way `check_response` builds it: `retry_after` in the response
    parameters wins over the status, then the status picks the class, and an
    unlisted status is a plain `TelegramAPIError`. Like the real thing, it
    carries no status attribute.
    """
    method: Any = SyntheticMethod()
    if retry_after is not None:
        return TelegramRetryAfter(method=method, message=description, retry_after=retry_after)
    exception = _EXCEPTION_BY_STATUS.get(status_code)
    if exception is None and status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
        exception = TelegramServerError
    return (exception or TelegramAPIError)(method=method, message=description)


def network_error(message: str = "Request timeout error") -> TelegramNetworkError:
    """What aiogram raises when Telegram never answered (timeout, reset)."""
    method: Any = SyntheticMethod()
    return TelegramNetworkError(method=method, message=message)
