"""The poller's kill-switch fast lane (ARCHITECTURE §6.1; S04.4 §3.7).

When the poller writes a batch, an owner's `/pause` or stop word in it pauses
autonomy **in the same transaction** — before the inbox processor reaches the
row, and before any send can be claimed on state that lags behind it. The
inbox then processes the message normally (idempotent) and acknowledges it.

Only a plain message in the owner's own private chat counts: `from.id ==
chat.id`, an allowlisted owner (the route the inbox would give it is
`CONTROL`), not forwarded, and its whole text `/pause` (optionally addressed
`@<bot username>`) or a stop word. Forwarded text is a third party's words
and never a command (§6 invariant 3b). There is no Pause button in M04.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions import autonomy
from assistant.core.routing import Route
from assistant.observability.logging import get_logger
from assistant.telegram.control.handlers import is_stop_word
from assistant.telegram.payload import chat_of, sender_of
from assistant.telegram.routing import route_update

logger = get_logger(__name__)

#: The fields that mark a message as forwarded (Bot API `forward_origin`, the
#: legacy `forward_*` ones, and channel auto-forwards).
_FORWARD_FIELDS = (
    "forward_origin",
    "forward_from",
    "forward_from_chat",
    "forward_sender_name",
    "forward_date",
)


def _is_pause_command(text: str, bot_username: str | None) -> bool:
    stripped = text.strip().lower()
    if stripped == "/pause":
        return True
    return bool(bot_username) and stripped == f"/pause@{str(bot_username).lower()}"


def pausing_owners(
    updates: Sequence[dict[str, Any]],
    *,
    owner_ids: frozenset[int],
    bot_id: int,
    bot_username: str | None,
) -> list[int]:
    """Telegram user ids of owners whose message in this batch pauses autonomy."""
    found: list[int] = []
    for update in updates:
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        if route_update(update, owner_ids=owner_ids, bot_id=bot_id).route is not Route.CONTROL:
            continue
        sender_id = sender_of(message).get("id")
        if not isinstance(sender_id, int) or sender_id != chat_of(message).get("id"):
            continue
        if message.get("is_automatic_forward") or any(
            message.get(name) is not None for name in _FORWARD_FIELDS
        ):
            continue
        text = message.get("text")
        if not isinstance(text, str):
            continue
        if (_is_pause_command(text, bot_username) or is_stop_word(text)) and (
            sender_id not in found
        ):
            found.append(sender_id)
    return found


async def apply(session: AsyncSession, telegram_user_ids: Sequence[int], now: datetime) -> int:
    """Pause the active account of each owner, in the caller's transaction.
    Returns how many accounts changed."""
    changed = 0
    for telegram_user_id in telegram_user_ids:
        account_id = await autonomy.active_account_id(session, telegram_user_id)
        if account_id is None:
            continue
        if await autonomy.pause(
            session, account_id=account_id, now=now, source=autonomy.SOURCE_FAST_LANE
        ):
            changed += 1
            logger.info("autonomy_paused_fast_lane", account_id=account_id)
    return changed
