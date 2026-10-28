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

**At-most-once.** A send whose outcome is unknown is `delivery_uncertain`
and is **never retried automatically** (§6 invariant 6): a duplicate message
sent as the owner cannot be taken back.

**Business sends are plain text, one call, current connection.** Only
`actions/executor.py` calls the business methods (a confinement test), after
its committed claim passed the policy re-check (§6 invariants 1, 6). Each call
resolves the account's *current* connection id and the conversation's chat at
call time (`business_targets.py`) and refuses locally — no Telegram call — when
either is missing, inactive or not the account's, or (send, edit) the
connection lacks `can_reply`. The send reports the connection id it used,
so a failure stales only that connection. Text is never parsed as HTML
and never split. `send_text(BusinessChat, ...)` still raises: there is no
second way to send as the owner.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from aiogram import Bot

from assistant.core.clock import Clock
from assistant.core.delivery import SendResult as SendResult
from assistant.observability.logging import get_logger
from assistant.telegram.business_targets import BusinessTarget, BusinessTargetResolver
from assistant.telegram.errors import ClassifiedError, ErrorClass, classify_exception
from assistant.telegram.render import render_control_message

logger = get_logger(__name__)

#: Telegram tolerates about one message per second per chat (TF §5).
MIN_SECONDS_BETWEEN_SENDS = 1.0
#: And under 25 messages per second in all (ARCHITECTURE §6.8): any two sends
#: are at least this far apart, whatever their chats (S04.4, TL decision).
MIN_SECONDS_BETWEEN_ANY_SENDS = 0.04
#: The Bot API request timeout set on the bot's session (bootstrap). It stays
#: below the outbound queue's 30 s shutdown grace, so a send in flight at
#: SIGTERM ends and its outcome is recorded before the worker stops (S04.4).
REQUEST_TIMEOUT_SECONDS = 20
#: The longest a send may wait in the throttle before its request starts. With
#: the request timeout it bounds how long a claimed attempt can still be in
#: flight (`system.mark_uncertain` never marks one earlier).
MAX_THROTTLE_WAIT_SECONDS = 10
#: `error_code` of a business call refused before Telegram was asked.
LOCAL_TARGET_REFUSED = "local_target_refused"

#: How the throttle waits: `asyncio.sleep` in production; tests inject one
#: that returns at once, since their clock never moves by itself (AGENTS §11).
Sleep = Callable[[float], Awaitable[None]]


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

    async def edit_message_reply_markup(
        self,
        *,
        chat_id: int,
        message_id: int,
        reply_markup: Any | None = None,
        business_connection_id: str | None = None,
    ) -> dict[str, Any]: ...

    async def send_chat_action(self, *, chat_id: int, action: str) -> None: ...

    async def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None, show_alert: bool = False
    ) -> None: ...

    # Business (owner-voice) methods: plain text, never a parse mode, so text
    # a contact or a model influenced can carry no markup (S04.4).
    async def send_plain_message(
        self, *, chat_id: int, text: str, business_connection_id: str
    ) -> dict[str, Any]: ...

    async def edit_plain_message_text(
        self, *, chat_id: int, message_id: int, text: str, business_connection_id: str
    ) -> dict[str, Any]: ...

    async def delete_business_messages(
        self, *, business_connection_id: str, message_ids: Sequence[int]
    ) -> bool: ...


class BusinessSendNotAvailableError(RuntimeError):
    """`send_text` never sends as the owner: business sends go only through
    `send_business_text`, called by `actions/executor.py` after its claim."""


class OutboundGateway:
    """The single egress point."""

    def __init__(
        self,
        sender: TelegramSender,
        clock: Clock,
        *,
        sleep: Sleep = asyncio.sleep,
        targets: BusinessTargetResolver | None = None,
    ) -> None:
        self._sender = sender
        self._clock = clock
        self._sleep = sleep
        #: None: no business target resolves, so every business call is
        #: refused locally (a process or test that never sends as the owner).
        self._targets = targets
        self._last_send_at: dict[int, float] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._last_any_send_at: float | None = None

    async def _throttle(self, chat_id: int) -> None:
        """One message per second per chat, serialised per chat, and any two
        sends at least `MIN_SECONDS_BETWEEN_ANY_SENDS` apart.

        The send's slot is reserved before waiting (one wait, never a lock
        held across it for other chats): the later of now, this chat's last
        slot plus one second, and anyone's last slot plus the global spacing.
        """
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            now = self._clock.now().timestamp()
            async with self._global_lock:
                due = now
                previous = self._last_send_at.get(chat_id)
                if previous is not None:
                    due = max(due, previous + MIN_SECONDS_BETWEEN_SENDS)
                if self._last_any_send_at is not None:
                    due = max(due, self._last_any_send_at + MIN_SECONDS_BETWEEN_ANY_SENDS)
                self._last_any_send_at = due
                self._last_send_at[chat_id] = due
            if due > now:
                await self._sleep(due - now)

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
                "business sends go only through send_business_text, after the "
                "executor's committed claim"
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

    async def clear_control_keyboard(self, target: ControlChat, message_id: int) -> SendResult:
        """Remove a control-chat card's buttons and keep its text — for a
        card whose body says more than its question (the connection notice)."""
        try:
            await self._sender.edit_message_reply_markup(
                chat_id=target.chat_id,
                message_id=message_id,
                reply_markup=None,
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

    # ------------------------------------------------------- business (owner voice)
    def attach_business_targets(self, targets: BusinessTargetResolver) -> None:
        """Let business calls resolve their targets (bootstrap `_wire_m04`,
        for the roles that run the executor). Without it every business call
        is refused locally."""
        self._targets = targets

    async def send_business_text(
        self, *, account_id: int, conversation_id: int, text: str
    ) -> SendResult:
        """One plain-text `sendMessage` as the owner, never split.

        Called only by `actions/executor.py` after its committed claim. The
        target is resolved now (the current connection id); without one the
        call is refused with `local_target_refused` and Telegram is not
        called. `message_id` is returned as Telegram gave it, 0 included
        ("pending identity", ARCHITECTURE §6.8).
        """
        result, _ = await self._send_business(
            account_id=account_id, conversation_id=conversation_id, text=text
        )
        return result

    async def send_business_text_with_connection(
        self, *, account_id: int, conversation_id: int, text: str
    ) -> tuple[SendResult, str | None]:
        """`send_business_text`, and the connection id the call was sent on
        (None when refused locally: no call was made). The executor stales
        only that connection on BUSINESS_CONNECTION_INVALID — never a
        replacement that arrived while the call was in flight (§6.4)."""
        return await self._send_business(
            account_id=account_id, conversation_id=conversation_id, text=text
        )

    async def _send_business(
        self, *, account_id: int, conversation_id: int, text: str
    ) -> tuple[SendResult, str | None]:
        target = await self._business_target(account_id, conversation_id)
        if target is None:
            refused = self._refused("send", account_id=account_id, conversation_id=conversation_id)
            return refused, None
        await self._throttle(target.chat_id)
        try:
            message = await self._sender.send_plain_message(
                chat_id=target.chat_id,
                text=text,
                business_connection_id=target.connection_id,
            )
        except Exception as error:
            failure = self._business_failure(
                "send", error, account_id=account_id, conversation_id=conversation_id
            )
            return failure, target.connection_id
        message_id = message.get("message_id") if isinstance(message, dict) else None
        logger.info(
            "business_message_sent",
            account_id=account_id,
            conversation_id=conversation_id,
            message_id=message_id,
        )
        return SendResult(ErrorClass.OK, message_id=message_id), target.connection_id

    async def edit_assistant_message(
        self, *, account_id: int, conversation_id: int, assistant_message_ref: int, text: str
    ) -> SendResult:
        """Edit one of the assistant's own sends (RG-12 mechanics, P2-T10).

        `assistant_message_ref` is a `messages.id`; anything but an
        `assistant_sent` row of this conversation with a known Telegram id is
        refused locally — an owner's or a contact's message is never edited
        (§6 invariant 14). No product feature calls this in P2.
        """
        target, telegram_ids = await self._own_messages(
            account_id, conversation_id, [assistant_message_ref]
        )
        if target is None or telegram_ids is None:
            return self._refused("edit", account_id=account_id, conversation_id=conversation_id)
        await self._throttle(target.chat_id)
        try:
            await self._sender.edit_plain_message_text(
                chat_id=target.chat_id,
                message_id=telegram_ids[0],
                text=text,
                business_connection_id=target.connection_id,
            )
        except Exception as error:
            return self._business_failure(
                "edit", error, account_id=account_id, conversation_id=conversation_id
            )
        logger.info(
            "business_message_edited",
            account_id=account_id,
            conversation_id=conversation_id,
            message_id=telegram_ids[0],
        )
        return SendResult(ErrorClass.OK, message_id=telegram_ids[0])

    async def delete_own_business_messages(
        self, *, account_id: int, conversation_id: int, assistant_message_refs: Sequence[int]
    ) -> SendResult:
        """Delete the assistant's own sends (RG-12 mechanics, P2-T10): every ref
        must be an `assistant_sent` row of this conversation, or nothing is
        deleted and Telegram is not called. No product feature calls this in P2."""
        target, telegram_ids = await self._own_messages(
            account_id, conversation_id, assistant_message_refs, needs_reply_right=False
        )
        if target is None or telegram_ids is None:
            return self._refused("delete", account_id=account_id, conversation_id=conversation_id)
        await self._throttle(target.chat_id)
        try:
            await self._sender.delete_business_messages(
                business_connection_id=target.connection_id, message_ids=telegram_ids
            )
        except Exception as error:
            return self._business_failure(
                "delete", error, account_id=account_id, conversation_id=conversation_id
            )
        logger.info(
            "business_own_messages_deleted",
            account_id=account_id,
            conversation_id=conversation_id,
            count=len(telegram_ids),
        )
        return SendResult(ErrorClass.OK)

    async def _business_target(
        self, account_id: int, conversation_id: int, *, needs_reply_right: bool = True
    ) -> BusinessTarget | None:
        if self._targets is None:
            return None
        return await self._targets.resolve(
            account_id, conversation_id, needs_reply_right=needs_reply_right
        )

    async def _own_messages(
        self,
        account_id: int,
        conversation_id: int,
        refs: Sequence[int],
        *,
        needs_reply_right: bool = True,
    ) -> tuple[BusinessTarget | None, list[int] | None]:
        """Deleting needs a delete right, not `can_reply`; Telegram enforces
        which one (no product feature deletes in P2)."""
        target = await self._business_target(
            account_id, conversation_id, needs_reply_right=needs_reply_right
        )
        if target is None or self._targets is None:
            return None, None
        return target, await self._targets.assistant_message_ids(account_id, conversation_id, refs)

    def _refused(self, operation: str, *, account_id: int, conversation_id: int) -> SendResult:
        logger.warning(
            "business_call_refused",
            operation=operation,
            account_id=account_id,
            conversation_id=conversation_id,
            error_code=LOCAL_TARGET_REFUSED,
        )
        return SendResult(ErrorClass.BAD_REQUEST, error_code=LOCAL_TARGET_REFUSED)

    def _business_failure(
        self, operation: str, error: Exception, *, account_id: int, conversation_id: int
    ) -> SendResult:
        classified: ClassifiedError = classify_exception(error)
        logger.warning(
            "business_call_failed",
            operation=operation,
            account_id=account_id,
            conversation_id=conversation_id,
            error_class=classified.error_class,
            error_code=classified.code,
            exc_type=type(error).__name__,
        )
        return SendResult(
            classified.error_class,
            error_code=classified.code,
            retry_after=classified.retry_after,
        )

    def _classify_failure(self, error: Exception, *, account_id: int) -> SendResult:
        classified: ClassifiedError = classify_exception(error)
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


class AiogramSender:
    """`TelegramSender` over an aiogram `Bot`. Constructed only by bootstrap.

    The allow-listed calls and nothing else. `parse_mode="HTML"` is set per
    control-chat call, because the renderer produces Telegram HTML; the bot's
    default stays unset so no other path can send HTML by accident. The
    business methods pass `parse_mode=None` explicitly: an owner-voice text is
    sent exactly as approved, never interpreted as markup.
    """

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: Any | None = None,
        business_connection_id: str | None = None,
    ) -> dict[str, Any]:
        message = await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            business_connection_id=business_connection_id,
        )
        return {"message_id": message.message_id}

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Any | None = None,
        business_connection_id: str | None = None,
    ) -> dict[str, Any]:
        await self._bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            business_connection_id=business_connection_id,
        )
        return {"message_id": message_id}

    async def edit_message_reply_markup(
        self,
        *,
        chat_id: int,
        message_id: int,
        reply_markup: Any | None = None,
        business_connection_id: str | None = None,
    ) -> dict[str, Any]:
        await self._bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            business_connection_id=business_connection_id,
        )
        return {"message_id": message_id}

    async def send_chat_action(self, *, chat_id: int, action: str) -> None:
        await self._bot.send_chat_action(chat_id=chat_id, action=action)

    async def answer_callback_query(
        self, *, callback_query_id: str, text: str | None = None, show_alert: bool = False
    ) -> None:
        await self._bot.answer_callback_query(
            callback_query_id=callback_query_id, text=text, show_alert=show_alert
        )

    async def send_plain_message(
        self, *, chat_id: int, text: str, business_connection_id: str
    ) -> dict[str, Any]:
        message = await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=None,
            business_connection_id=business_connection_id,
        )
        return {"message_id": message.message_id}

    async def edit_plain_message_text(
        self, *, chat_id: int, message_id: int, text: str, business_connection_id: str
    ) -> dict[str, Any]:
        await self._bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=None,
            business_connection_id=business_connection_id,
        )
        return {"message_id": message_id}

    async def delete_business_messages(
        self, *, business_connection_id: str, message_ids: Sequence[int]
    ) -> bool:
        return await self._bot.delete_business_messages(
            business_connection_id=business_connection_id, message_ids=list(message_ids)
        )
