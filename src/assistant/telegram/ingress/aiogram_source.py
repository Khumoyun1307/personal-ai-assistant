"""aiogram adapter for the read-only side of the Bot API (AGENTS §5).

This module may call ONLY `getUpdates`, `getMe` and
`deleteWebhook(drop_pending_updates=False)`. Anything with a visible effect
belongs to `telegram/gateway.py`.
"""

from __future__ import annotations

from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramConflictError as AiogramConflictError,
)
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from assistant.telegram.ingress.ports import (
    BotIdentity,
    TelegramConflictError,
    TelegramTransientError,
)

#: Seconds a `getUpdates` request may take beyond its long-poll timeout.
LONG_POLL_REQUEST_MARGIN_SECONDS = 15


class AiogramUpdateSource:
    """Wraps an aiogram `Bot` as an `UpdateSource`."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def get_me(self) -> BotIdentity:
        me = await self._bot.get_me()
        return BotIdentity(
            id=me.id,
            username=me.username,
            can_connect_to_business=bool(me.can_connect_to_business),
        )

    async def delete_webhook(self) -> None:
        # drop_pending_updates stays False. True would permanently discard up
        # to 24 h of business messages (AGENTS §7).
        await self._bot.delete_webhook(drop_pending_updates=False)

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout: int,
        limit: int,
        allowed_updates: list[str],
    ) -> list[dict[str, Any]]:
        try:
            updates = await self._bot.get_updates(
                offset=offset,
                timeout=timeout,
                limit=limit,
                allowed_updates=allowed_updates,
                # The bot's session timeout is short (20 s, for sends — S04.4);
                # a long poll holds the request open for `timeout` seconds.
                request_timeout=timeout + LONG_POLL_REQUEST_MARGIN_SECONDS,
            )
        except AiogramConflictError as error:
            raise TelegramConflictError(type(error).__name__) from error
        except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter) as error:
            raise TelegramTransientError(type(error).__name__) from error
        # Dump to plain JSON-compatible dicts: the raw payload is what gets
        # stored, and keeping aiogram models out means a library upgrade
        # cannot change ingestion semantics silently.
        return [update.model_dump(mode="json", exclude_none=True) for update in updates]
