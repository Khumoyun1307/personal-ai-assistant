"""Business update → domain event (ARCHITECTURE §6.5; TF §3.2, §12).

Two pure functions. `classify` decides who authored a message, in the order
the architecture fixes; `normalize` turns the raw payload into an
`ObservedMessage` / `ObservedDeletion` / `IgnoredUpdate`. Nothing here touches
the database or the network, so the whole module is table-testable against
the sanitised fixtures.

What is deliberately *not* carried over from the payload: `file_id` (a
download capability — the product fetches media at ingestion only when a
feature needs it), file names, and the phone number of a shared contact
(FR-MEM-05 territory: never store it).
"""

from __future__ import annotations

from typing import Any

from assistant.conversations.events import (
    BusinessEvent,
    Counterpart,
    Direction,
    IgnoredUpdate,
    IgnoreReason,
    MediaInfo,
    MessageContent,
    ObservedDeletion,
    ObservedMessage,
)
from assistant.inbox.processor import DeterministicError
from assistant.observability.logging import get_logger
from assistant.telegram.payload import (
    PayloadError,
    chat_of,
    integer,
    optional_timestamp,
    sender_of,
    timestamp,
)

logger = get_logger(__name__)

#: Telegram's service notifications account (TF §12 LV-18 design).
SERVICE_CHAT_IDS: frozenset[int] = frozenset({777000})

#: Message content keys in the order they are checked; the first present wins.
_MEDIA_KINDS: tuple[str, ...] = (
    "photo",
    "video",
    "voice",
    "video_note",
    "audio",
    "document",
    "animation",
    "sticker",
)
#: Content keys that carry no media file but are still a message kind.
_PLAIN_KINDS: tuple[str, ...] = ("location", "venue", "contact", "poll", "dice", "game")


class NormalizeError(DeterministicError):
    """A business payload without the fields storage depends on."""


def classify(
    message: dict[str, Any], *, owner_user_id: int, bot_id: int
) -> Direction | IgnoreReason:
    """Who authored this business message — §6.5, evaluated in order.

    The ignore rules come first (TF §12 LV-06/LV-18): a chat with a bot, the
    owner's Saved Messages, a service chat or anything not private creates no
    conversation at all.
    """
    chat = chat_of(message)
    chat_id = chat.get("id")
    sender = sender_of(message)
    if chat.get("type") != "private":
        return IgnoreReason.NOT_PRIVATE
    if chat_id == bot_id:
        return IgnoreReason.BOT_CHAT
    if chat_id == owner_user_id:
        return IgnoreReason.SAVED_MESSAGES
    if chat_id in SERVICE_CHAT_IDS:
        return IgnoreReason.SERVICE_CHAT
    if sender.get("is_bot") is True:
        return IgnoreReason.BOT_COUNTERPART

    business_bot = message.get("sender_business_bot")
    if isinstance(business_bot, dict) and business_bot.get("id") == bot_id:
        return Direction.ASSISTANT_SENT
    if sender.get("id") == owner_user_id:
        if message.get("is_from_offline") is True or isinstance(business_bot, dict):
            # Owner-authored but not evidence the owner is present: an away /
            # greeting message, or something another business bot sent.
            return Direction.OWNER_AUTOMATED
        return Direction.OWNER_MANUAL
    if sender.get("id") != chat_id:
        logger.warning("business_sender_unexpected", chat_id=chat_id, from_id=sender.get("id"))
    return Direction.CONTACT


def normalize(
    update: dict[str, Any], kind: str, *, owner_user_id: int, bot_id: int
) -> BusinessEvent:
    """The stored update → the event the domain stores. Raises
    `NormalizeError` (deterministic → dead letter) on a malformed payload."""
    body = update.get(kind)
    if not isinstance(body, dict):
        raise NormalizeError(kind)
    try:
        if kind == "deleted_business_messages":
            return _deletion(body)
        if kind in {"business_message", "edited_business_message"}:
            return _message(
                body,
                is_edit=kind == "edited_business_message",
                owner_user_id=owner_user_id,
                bot_id=bot_id,
            )
    except PayloadError as error:
        raise NormalizeError(str(error)) from error
    raise NormalizeError(kind)


def _deletion(body: dict[str, Any]) -> ObservedDeletion:
    chat_id = integer(chat_of(body).get("id"), "chat.id")
    ids = body.get("message_ids")
    if not isinstance(ids, list):
        raise PayloadError("message_ids")
    return ObservedDeletion(
        business_chat_id=chat_id,
        telegram_message_ids=tuple(integer(value, "message_ids") for value in ids),
    )


def _message(
    body: dict[str, Any], *, is_edit: bool, owner_user_id: int, bot_id: int
) -> BusinessEvent:
    verdict = classify(body, owner_user_id=owner_user_id, bot_id=bot_id)
    if isinstance(verdict, IgnoreReason):
        return IgnoredUpdate(verdict)
    chat = chat_of(body)
    chat_id = integer(chat.get("id"), "chat.id")
    sender = sender_of(body)
    if "id" not in sender:
        raise PayloadError("from")
    reply_to = body.get("reply_to_message")
    reply_to_id = (
        integer(reply_to.get("message_id"), "reply_to_message.message_id")
        if isinstance(reply_to, dict) and "message_id" in reply_to
        else None
    )
    return ObservedMessage(
        business_chat_id=chat_id,
        telegram_message_id=integer(body.get("message_id"), "message_id"),
        sent_at=timestamp(body.get("date")),
        direction=verdict,
        counterpart=_counterpart(chat, sender),
        content=_content(body),
        is_edit=is_edit,
        edited_at=optional_timestamp(body.get("edit_date"), "edit_date"),
        reply_to_telegram_message_id=reply_to_id,
    )


def _counterpart(chat: dict[str, Any], sender: dict[str, Any]) -> Counterpart:
    """The contact is the *chat* in a private business chat; `from` is whoever
    typed, which is the owner for owner messages."""
    user_id = integer(chat.get("id"), "chat.id")
    first = chat.get("first_name")
    last = chat.get("last_name")
    parts = [p for p in (first, last) if isinstance(p, str) and p]
    display_name = " ".join(parts) or None
    username = chat.get("username")
    is_bot = sender.get("is_bot") is True if sender.get("id") == user_id else False
    return Counterpart(
        telegram_user_id=user_id,
        display_name=display_name,
        username=username if isinstance(username, str) and username else None,
        is_bot=is_bot,
    )


def _content(body: dict[str, Any]) -> MessageContent:
    text = body.get("text")
    caption = body.get("caption")
    for key in _MEDIA_KINDS:
        raw = body.get(key)
        if raw is None:
            continue
        media = _media(key, raw)
        return MessageContent(
            content_type=key,
            text=caption if isinstance(caption, str) and caption else None,
            media=media,
        )
    if isinstance(text, str):
        return MessageContent(content_type="text", text=text)
    for key in _PLAIN_KINDS:
        if body.get(key) is not None:
            # A shared contact's phone number, a location's coordinates: the
            # kind is recorded, the payload is not (FR-MEM-05).
            return MessageContent(content_type=key)
    return MessageContent(content_type="unknown")


def _media(kind: str, raw: Any) -> MediaInfo | None:
    if kind == "photo":
        if not isinstance(raw, list) or not raw:
            return MediaInfo(kind="photo")
        sizes = [size for size in raw if isinstance(size, dict)]
        if not sizes:
            return MediaInfo(kind="photo")
        raw = max(sizes, key=lambda size: _int_or_zero(size.get("file_size")))
    if not isinstance(raw, dict):
        return MediaInfo(kind=kind)
    thumb_raw = raw.get("thumbnail")
    thumb: dict[str, Any] = thumb_raw if isinstance(thumb_raw, dict) else {}
    return MediaInfo(
        kind=kind,
        file_unique_id=_str_or_none(raw.get("file_unique_id")),
        file_size=_int_or_none(raw.get("file_size")),
        mime_type=_str_or_none(raw.get("mime_type")),
        duration=_int_or_none(raw.get("duration")),
        width=_int_or_none(raw.get("width")) or _int_or_none(thumb.get("width")),
        height=_int_or_none(raw.get("height")) or _int_or_none(thumb.get("height")),
    )


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _int_or_zero(value: Any) -> int:
    return _int_or_none(value) or 0


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
