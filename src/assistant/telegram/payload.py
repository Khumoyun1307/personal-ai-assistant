"""Small readers for stored Telegram payloads (aiogram JSON dumps).

Stored updates are `model_dump(mode="json", exclude_none=True)` of aiogram
models, so the sender key is `from_user`; a raw Bot API object says `from`.
Both are accepted everywhere so a fixture captured either way parses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


class PayloadError(ValueError):
    """A payload without a field the code depends on. Deterministic."""


def chat_of(payload: dict[str, Any]) -> dict[str, Any]:
    chat = payload.get("chat")
    return chat if isinstance(chat, dict) else {}


def sender_of(payload: dict[str, Any]) -> dict[str, Any]:
    sender = payload.get("from_user")
    if not isinstance(sender, dict):
        sender = payload.get("from")
    return sender if isinstance(sender, dict) else {}


def timestamp(value: Any, field: str = "date") -> datetime:
    """Telegram's integer Unix time, or an ISO string a fixture may carry."""
    if isinstance(value, bool):
        raise PayloadError(field)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise PayloadError(field) from error
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise PayloadError(field)


def optional_timestamp(value: Any, field: str) -> datetime | None:
    return None if value is None else timestamp(value, field)


def integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PayloadError(field)
    return value
