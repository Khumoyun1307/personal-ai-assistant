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
                callback_id = payload.get("id")
                if isinstance(callback_id, str):
                    await self._gateway.answer_callback(callback_id, CALLBACK_EXPIRED)
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
        text = payload.get("text")
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
            ),
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
