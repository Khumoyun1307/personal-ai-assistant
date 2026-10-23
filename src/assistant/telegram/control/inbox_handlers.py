"""Inbox route handlers for the control chat (ARCHITECTURE §6.2, §6.3).

The inbox processor routes each stored update; these handlers do the fast part
inside the processor's transaction and push anything slow (the agent) to a job:

- **CONTROL** `message` → `ControlDispatcher` (commands, stop word, pending
  input, or a recorded turn + `agent.owner_request` job in the same
  transaction). `callback_query` → answered at once: no M01 flow has buttons,
  so every press is "no longer active". `my_chat_member` → logged.
- **REFUSED** private message → the fixed refusal, rate limited per chat and
  globally, with no model and no stored state (§6 invariant 12).

Business routes are not handled here and never reach these functions
(§6 invariant 5); M02 adds their pipeline.
"""

from __future__ import annotations

import hashlib
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.core.clock import Clock
from assistant.core.routing import RouteDecision
from assistant.inbox.models import InboundUpdate
from assistant.observability.logging import get_logger
from assistant.telegram.control import handlers
from assistant.telegram.control.dispatcher import ControlDispatcher
from assistant.telegram.control.handlers import IncomingMessage
from assistant.telegram.gateway import ControlChat, OutboundGateway

logger = get_logger(__name__)

TEXT_ONLY = "I can only read text messages for now."
CALLBACK_EXPIRED = "This button is no longer active."


#: Fields that mark a message as forwarded: `forward_origin` (Bot API 7.0+),
#: the legacy `forward_*` fields older payloads carry, and channel
#: auto-forwards (https://core.telegram.org/bots/api#message).
_FORWARD_FIELDS = (
    "forward_origin",
    "forward_from",
    "forward_from_chat",
    "forward_sender_name",
    "forward_date",
)


def _is_forwarded(payload: dict[str, Any]) -> bool:
    return bool(payload.get("is_automatic_forward")) or any(
        payload.get(name) is not None for name in _FORWARD_FIELDS
    )


def third_party_text(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """`(forwarded, quoted_text)` of an owner's control-chat message (§7.1 taint).

    - Forwarded: the message text itself is the third party's. A quote inside
      a forward belongs to the forward, so it is not kept separately.
    - Quoted: `quote.text` counts when it comes from another chat
      (`external_reply`) or from a forwarded message the owner replies to.
      Quoting the assistant's or the owner's own message is not third-party.
    - An `external_reply` without a quote carries no text we store
      (`ExternalReplyInfo` has none), so it adds nothing to the context.
    """
    if _is_forwarded(payload):
        return True, None
    quote = payload.get("quote")
    quote_text = quote.get("text") if isinstance(quote, dict) else None
    if not isinstance(quote_text, str) or not quote_text.strip():
        return False, None
    replied = payload.get("reply_to_message")
    from_elsewhere = payload.get("external_reply") is not None or (
        isinstance(replied, dict) and _is_forwarded(replied)
    )
    return False, (quote_text if from_elsewhere else None)


def _origin(origin: Any, users: set[int], chats: set[int], names: set[str]) -> None:
    """Collect who a `MessageOrigin` names (user, hidden user, chat, channel)."""
    if not isinstance(origin, dict):
        return
    for key, bucket in (("sender_user", users), ("sender_chat", chats), ("chat", chats)):
        value = origin.get(key)
        if isinstance(value, dict) and isinstance(value.get("id"), int):
            bucket.add(value["id"])
    hidden = origin.get("sender_user_name")
    if isinstance(hidden, str) and hidden.strip():
        names.add(hidden)


def third_party_origins(payload: dict[str, Any]) -> tuple[set[int], set[int], set[str]]:
    """`(user_ids, chat_ids, hidden_names)` the forwarded or quoted text came
    from, so the dispatcher can tell whether it is managed-chat content
    (§6 invariant 9, M03 D53)."""
    users: set[int] = set()
    chats: set[int] = set()
    names: set[str] = set()
    sources = [payload]
    replied = payload.get("reply_to_message")
    if isinstance(replied, dict):
        sources.append(replied)
    for source in sources:
        _origin(source.get("forward_origin"), users, chats, names)
        sender = source.get("forward_from")
        if isinstance(sender, dict) and isinstance(sender.get("id"), int):
            users.add(sender["id"])
        chat = source.get("forward_from_chat")
        if isinstance(chat, dict) and isinstance(chat.get("id"), int):
            chats.add(chat["id"])
        hidden = source.get("forward_sender_name")
        if isinstance(hidden, str) and hidden.strip():
            names.add(hidden)
    external = payload.get("external_reply")
    if isinstance(external, dict):
        _origin(external.get("origin"), users, chats, names)
        chat = external.get("chat")
        if isinstance(chat, dict) and isinstance(chat.get("id"), int):
            chats.add(chat["id"])
    return users, chats, names


class RefusalLimiter:
    """At most one refusal per chat per window, and a global cap per window.

    In memory on purpose: the people being refused are not tenants, and keeping
    a durable record of them would be exactly the trace we promise not to keep.
    A restart resets the window, which is acceptable for a courtesy reply.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        per_chat_window: timedelta = timedelta(hours=1),
        global_window: timedelta = timedelta(minutes=10),
        global_limit: int = 20,
    ) -> None:
        self._clock = clock
        self._per_chat_window = per_chat_window
        self._global_window = global_window
        self._global_limit = global_limit
        self._last_by_chat: dict[str, datetime] = {}
        self._recent: deque[datetime] = deque()

    def allow(self, chat_id: int) -> bool:
        now = self._clock.now()
        while self._recent and now - self._recent[0] > self._global_window:
            self._recent.popleft()
        if len(self._recent) >= self._global_limit:
            return False
        key = hashlib.sha256(str(chat_id).encode()).hexdigest()
        last = self._last_by_chat.get(key)
        if last is not None and now - last < self._per_chat_window:
            return False
        self._last_by_chat[key] = now
        self._recent.append(now)
        # Bound memory: forget chats whose window has passed.
        if len(self._last_by_chat) > 10_000:
            cutoff = now - self._per_chat_window
            self._last_by_chat = {k: v for k, v in self._last_by_chat.items() if v >= cutoff}
        return True


class ControlInboxHandlers:
    def __init__(
        self,
        dispatcher: ControlDispatcher,
        gateway: OutboundGateway,
        limiter: RefusalLimiter,
    ) -> None:
        self._dispatcher = dispatcher
        self._gateway = gateway
        self._limiter = limiter

    async def control(
        self, session: AsyncSession, row: InboundUpdate, decision: RouteDecision
    ) -> None:
        payload = (row.payload or {}).get(decision.kind) or {}
        match decision.kind:
            case "message":
                await self._message(session, row, payload)
            case "callback_query":
                await self._callback(session, payload)
            case "my_chat_member":
                logger.info("owner_membership_changed", update_row_id=row.id)
            case _:
                # Edited control messages are not re-executed: an edit must not
                # silently re-run a command.
                logger.info("control_update_ignored", update_row_id=row.id, kind=decision.kind)

    async def _message(
        self, session: AsyncSession, row: InboundUpdate, payload: dict[str, Any]
    ) -> None:
        chat = payload.get("chat") or {}
        sender = payload.get("from") or payload.get("from_user") or {}
        chat_id, from_id = chat.get("id"), sender.get("id")
        message_id = payload.get("message_id")
        if not isinstance(chat_id, int) or not isinstance(from_id, int):
            return
        forwarded, quoted_text = third_party_text(payload)
        users, chats, names = (
            third_party_origins(payload)
            if forwarded or quoted_text is not None
            else (set(), set(), set())
        )
        text = payload.get("text")
        if not isinstance(text, str) and forwarded:
            # A forwarded photo or document: its caption is the forwarded text.
            text = payload.get("caption")
        if not isinstance(text, str) or not text.strip():
            await self._gateway.send_text(
                ControlChat(account_id=row.account_id or 0, chat_id=chat_id), TEXT_ONLY
            )
            return
        await self._dispatcher.dispatch(
            session,
            IncomingMessage(
                account_id=0,
                chat_id=chat_id,
                telegram_message_id=message_id if isinstance(message_id, int) else 0,
                text=text,
                from_id=from_id,
                received_at=row.received_at,
                forwarded=forwarded,
                quoted_text=quoted_text,
                origin_user_ids=frozenset(users),
                origin_chat_ids=frozenset(chats),
                origin_names=frozenset(names),
            ),
        )

    async def _callback(self, session: AsyncSession, payload: dict[str, Any]) -> None:
        """A button press (S02.4 cards). Runs in the singleton inbox processor,
        so a card's effect is serialised with ingestion."""
        callback_id = payload.get("id")
        sender = payload.get("from") or payload.get("from_user") or {}
        from_id = sender.get("id")
        if not isinstance(callback_id, str) or not isinstance(from_id, int):
            return
        message = payload.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        if not isinstance(chat_id, int):
            await self._gateway.answer_callback(callback_id, CALLBACK_EXPIRED)
            return
        await self._dispatcher.dispatch_callback(
            session,
            callback_id=callback_id,
            from_id=from_id,
            data=payload.get("data") if isinstance(payload.get("data"), str) else None,
            chat_id=chat_id,
            message_id=message_id if isinstance(message_id, int) else None,
        )

    async def refused(
        self, session: AsyncSession, row: InboundUpdate, decision: RouteDecision
    ) -> None:
        """The fixed refusal to a non-allowlisted private message, rate limited."""
        if decision.kind != "message" or not isinstance(decision.chat_id, int):
            return
        if not self._limiter.allow(decision.chat_id):
            logger.info("refusal_rate_limited", update_row_id=row.id)
            return
        await handlers.send_refusal(self._gateway, chat_id=decision.chat_id)
