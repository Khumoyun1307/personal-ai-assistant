"""Fake Telegram, for tests that must not touch the network (AGENTS §11).

`FakeUpdateSource` scripts batches, errors and 409s so the real poller loop can
be exercised — including the crash-between-write-and-confirm case, which is the
only way to prove the durability claim.
"""

from __future__ import annotations

from typing import Any

from assistant.telegram.ingress.ports import (
    BotIdentity,
    TelegramConflictError,
    TelegramTransientError,
)

BOT_ID = 8123456789


def make_update(
    update_id: int,
    *,
    kind: str = "message",
    chat_id: int = 555000111,
    from_id: int | None = None,
    chat_type: str = "private",
    text: str = "synthetic",
    is_bot: bool = False,
) -> dict[str, Any]:
    """A structurally realistic update with synthetic content."""
    sender_id = chat_id if from_id is None else from_id
    payload: dict[str, Any] = {
        "message_id": 1000 + update_id,
        "date": 1792288000 + update_id,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": sender_id, "is_bot": is_bot, "first_name": "Synthetic"},
        "text": text,
    }
    if kind == "callback_query":
        payload = {
            "id": str(update_id),
            "from": {"id": sender_id, "is_bot": is_bot},
            "data": "noop",
        }
    elif kind == "business_connection":
        payload = {
            "id": "synthetic-connection",
            "user_chat_id": chat_id,
            "user": {"id": sender_id, "is_bot": False},
            "is_enabled": True,
        }
    elif kind == "deleted_business_messages":
        payload = {
            "business_connection_id": "synthetic-connection",
            "chat": {"id": chat_id, "type": chat_type},
            "message_ids": [1, 2],
        }
    elif kind in {"business_message", "edited_business_message"}:
        payload["business_connection_id"] = "synthetic-connection"
    return {"update_id": update_id, kind: payload}


class FakeUpdateSource:
    """Scripted `UpdateSource`. Each `get_updates` pops one scripted step."""

    def __init__(
        self,
        batches: list[list[dict[str, Any]]] | None = None,
        *,
        bot_id: int = BOT_ID,
        can_connect_to_business: bool = True,
    ) -> None:
        self.batches: list[list[dict[str, Any]]] = batches or []
        self.identity = BotIdentity(
            id=bot_id, username="synthetic_bot", can_connect_to_business=can_connect_to_business
        )
        #: Every (offset, allowed_updates) we were called with, so tests can
        #: assert the offset only advanced after a commit.
        self.calls: list[dict[str, Any]] = []
        self.delete_webhook_calls = 0
        self.raise_conflict_on: int | None = None
        self.raise_transient_on: int | None = None

    async def get_me(self) -> BotIdentity:
        return self.identity

    async def delete_webhook(self) -> None:
        self.delete_webhook_calls += 1

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout: int,
        limit: int,
        allowed_updates: list[str],
    ) -> list[dict[str, Any]]:
        index = len(self.calls)
        self.calls.append(
            {"offset": offset, "timeout": timeout, "limit": limit, "allowed": allowed_updates}
        )
        if self.raise_conflict_on == index:
            raise TelegramConflictError("terminated by other getUpdates request")
        if self.raise_transient_on == index:
            raise TelegramTransientError("network")
        if index < len(self.batches):
            return self.batches[index]
        return []
