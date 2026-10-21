"""What the domain sees of a business update, after normalization (§6.5).

The transport adapter (`telegram/normalize.py`) turns a raw Telegram payload
into one of these; the domain never looks at the payload. Frozen, so a value
cannot drift between classification and storage.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Direction(StrEnum):
    """Who authored a message (ARCHITECTURE §6.5)."""

    CONTACT = "contact"
    OWNER_MANUAL = "owner_manual"
    OWNER_AUTOMATED = "owner_automated"
    ASSISTANT_SENT = "assistant_sent"


class IgnoreReason(StrEnum):
    """Why a business update is out of scope (TF §12 LV-06/LV-18; §6.5)."""

    BOT_CHAT = "bot_chat"
    BOT_COUNTERPART = "bot_counterpart"
    SAVED_MESSAGES = "saved_messages"
    SERVICE_CHAT = "service_chat"
    NOT_PRIVATE = "not_private"


@dataclass(frozen=True, slots=True)
class Counterpart:
    """The other party of a managed chat. Names are content (§10.1)."""

    telegram_user_id: int
    display_name: str | None = None
    username: str | None = None
    is_bot: bool = False


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """Structural facts about an attachment — never `file_id` or a file name."""

    kind: str
    file_unique_id: str | None = None
    file_size: int | None = None
    mime_type: str | None = None
    duration: int | None = None
    width: int | None = None
    height: int | None = None

    def as_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass(frozen=True, slots=True)
class MessageContent:
    content_type: str
    text: str | None = None
    media: MediaInfo | None = None

    @property
    def content_hash(self) -> str:
        """Over the content only — never `edit_date` — so a no-op edit hashes
        the same (§6.5 step 2)."""
        canonical = json.dumps(
            [
                self.content_type,
                self.text,
                self.media.file_unique_id if self.media else None,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class ObservedMessage:
    business_chat_id: int
    telegram_message_id: int
    sent_at: datetime
    direction: Direction
    counterpart: Counterpart
    content: MessageContent
    is_edit: bool = False
    edited_at: datetime | None = None
    reply_to_telegram_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class ObservedDeletion:
    business_chat_id: int
    telegram_message_ids: tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class IgnoredUpdate:
    reason: IgnoreReason


BusinessEvent = ObservedMessage | ObservedDeletion | IgnoredUpdate
