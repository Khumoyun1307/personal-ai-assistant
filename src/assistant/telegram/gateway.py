"""The outbound gateway — the ONLY place that sends to Telegram.

ARCHITECTURE §6.8, AGENTS §5/§17. Every visible effect goes through here, for
control chat and business chat alike. Control-chat handlers use it too; they
never call `Message.answer()` (an AST test enforces that).

Two things this design buys:

**Targets are typed, so mis-routing is a type error.** `ControlChat` never
attaches a `business_connection_id`; `BusinessChat` always attaches the current
one. aiogram's shortcuts auto-inject the connection id, which would send *as
the owner* into the wrong chat — the single worst failure this product could
have.

**At-most-once.** A send whose outcome is unknown is reported as `ambiguous`
and is **never retried automatically** (§6 invariant 6): a duplicate message
sent as the owner cannot be taken back.

Business sending is deliberately **not implemented here yet.** It requires the
policy engine (§6 invariant 1: nothing sends on the owner's behalf without
passing policy immediately before execution), which arrives in M04. The target
type exists so the shape is right; calling it raises.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from assistant.core.clock import Clock
from assistant.observability.logging import get_logger
from assistant.telegram.errors import ClassifiedError, ErrorClass, classify
from assistant.telegram.render import render_control_message

logger = get_logger(__name__)

#: Telegram tolerates about one message per second per chat (TF §5).
MIN_SECONDS_BETWEEN_SENDS = 1.0


@dataclass(frozen=True, slots=True)
class ControlChat:
    """The owner's private chat with the bot. Never carries a connection id."""

    account_id: int
    chat_id: int


@dataclass(frozen=True, slots=True)
class BusinessChat:
    """A managed conversation. Always resolves the CURRENT connection id."""

    account_id: int
    conversation_id: int


Target = ControlChat | BusinessChat


@dataclass(frozen=True, slots=True)
class SendResult:
    """The outcome of one send. `ok` is the only success."""

    error_class: ErrorClass
    message_id: int | None = None
    error_code: str | None = None
    retry_after: int | None = None

    @property
    def ok(self) -> bool:
        return self.error_class is ErrorClass.OK

    @property
    def delivery_uncertain(self) -> bool:
        """Unknown outcome. Never retry; surface it (§6 invariant 11)."""
        return self.error_class is ErrorClass.AMBIGUOUS


class TelegramSender(Protocol):
    """The subset of the Bot API the gateway may call (§6.8 allow-list)."""

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: Any | None = None,
        business_connection_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Any | None = None,
        business_connection_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def send_chat_action(self, *, chat_id: int, action: str) -> None: ...

    async def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None, show_alert: bool = False
    ) -> None: ...


class BusinessSendNotAvailableError(RuntimeError):
    """Business sending needs the policy engine (M04). Not reachable before."""


class OutboundGateway:
    """The single egress point."""

    def __init__(self, sender: TelegramSender, clock: Clock) -> None:
        self._sender = sender
        self._clock = clock
        self._last_send_at: dict[int, float] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    async def _throttle(self, chat_id: int) -> None:
        """One message per second per chat, serialised per chat."""
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            previous = self._last_send_at.get(chat_id)
            now = self._clock.now().timestamp()
            if previous is not None:
                wait = MIN_SECONDS_BETWEEN_SENDS - (now - previous)
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = self._clock.now().timestamp()
            self._last_send_at[chat_id] = now

    async def send_text(
        self,
        target: Target,
        text: str,
        *,
        keyboard: Any | None = None,
    ) -> SendResult:
        """Send to the control chat. Business targets are refused (see module docstring)."""
        if isinstance(target, BusinessChat):
            raise BusinessSendNotAvailableError(
                "business sends require the policy engine (M04); "
                "nothing may send as the owner before then"
            )

        chunks = render_control_message(text)
        if not chunks:
            return SendResult(ErrorClass.OK)

        result = SendResult(ErrorClass.OK)
        for index, chunk in enumerate(chunks):
            # The keyboard belongs on the last chunk, where the buttons read.
            markup = keyboard if index == len(chunks) - 1 else None
            result = await self._send_one(target, chunk, markup)
            if not result.ok:
                return result
        return result

    async def _send_one(self, target: ControlChat, text: str, keyboard: Any | None) -> SendResult:
        await self._throttle(target.chat_id)
        try:
            message = await self._sender.send_message(
                chat_id=target.chat_id,
                text=text,
                reply_markup=keyboard,
                # Never for a control target. This is the line that keeps the
                # assistant speaking as itself rather than as the owner.
                business_connection_id=None,
            )
        except Exception as error:
            return self._classify_failure(error, account_id=target.account_id)

        message_id = message.get("message_id") if isinstance(message, dict) else None
        logger.info("control_message_sent", account_id=target.account_id, message_id=message_id)
        return SendResult(ErrorClass.OK, message_id=message_id)

    async def edit_control_text(
        self, target: ControlChat, message_id: int, text: str, *, keyboard: Any | None = None
    ) -> SendResult:
        """Edit a control-chat card in place (approval cards, confirmations)."""
        chunks = render_control_message(text)
        body = chunks[0] if chunks else ""
        try:
            await self._sender.edit_message_text(
                chat_id=target.chat_id,
                message_id=message_id,
                text=body,
                reply_markup=keyboard,
                business_connection_id=None,
            )
        except Exception as error:
            return self._classify_failure(error, account_id=target.account_id)
        return SendResult(ErrorClass.OK, message_id=message_id)

    async def chat_action(self, target: ControlChat, action: str = "typing") -> None:
        """Typing indicator. Control chat only.

        A typing indicator in a business chat is an owner-voice action and
        needs a PRD requirement and a policy rule first (AGENTS §7), so the
        signature does not accept a business target at all.
        """
        try:
            await self._sender.send_chat_action(chat_id=target.chat_id, action=action)
        except Exception as error:
            logger.warning("chat_action_failed", exc_type=type(error).__name__)

    async def answer_callback(
        self, callback_query_id: str, text: str | None = None, *, show_alert: bool = False
    ) -> None:
        """Always answer: an unanswered callback leaves a spinner on the button."""
        try:
            await self._sender.answer_callback_query(
                callback_query_id=callback_query_id, text=text, show_alert=show_alert
            )
        except Exception as error:
            logger.warning("answer_callback_failed", exc_type=type(error).__name__)

    def _classify_failure(self, error: Exception, *, account_id: int) -> SendResult:
        description = getattr(error, "message", None) or str(error)
        status = getattr(error, "status_code", None) or getattr(error, "status", None)
        retry_after = getattr(error, "retry_after", None)
        classified: ClassifiedError = classify(description, status=status, retry_after=retry_after)
        logger.warning(
            "control_send_failed",
            account_id=account_id,
            error_class=classified.error_class,
            error_code=classified.code,
            exc_type=type(error).__name__,
        )
        return SendResult(
            classified.error_class,
            error_code=classified.code,
            retry_after=classified.retry_after,
        )
