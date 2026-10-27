"""Fast domain hooks called inside the business-ingestion transaction.

Listeners may write through the supplied session and register after-commit
work. They never commit, call a model, or perform business-chat effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.conversations.events import Direction
from assistant.conversations.service import Effect


@dataclass(frozen=True, slots=True)
class MessageChange:
    account_id: int
    conversation_id: int
    message_id: int | None
    direction: Direction
    effect: Effect
    is_edit: bool
    metadata_only: bool
    mode: str
    sent_at: datetime
    content_changed: bool

    @property
    def new_message(self) -> bool:
        return self.effect in (Effect.INSERTED, Effect.RESURFACED) and not self.is_edit


@dataclass(frozen=True, slots=True)
class DeletionChange:
    account_id: int
    conversation_id: int
    message_ids: tuple[int, ...]
    assistant_sent_ids: tuple[int, ...]
    metadata_only: bool


class IngestionListener(Protocol):
    async def on_message(self, session: AsyncSession, change: MessageChange) -> None: ...

    async def on_deletion(self, session: AsyncSession, change: DeletionChange) -> None: ...
