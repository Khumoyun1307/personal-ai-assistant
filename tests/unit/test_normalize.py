"""Business update normalization and sender classification (ARCHITECTURE §6.5).

Pure functions, table-tested. The synthetic payloads mirror aiogram's JSON
dump of what TF §12 observed (LV-02, LV-07, LV-11, LV-15, LV-17).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from assistant.conversations.events import (
    Direction,
    IgnoredUpdate,
    IgnoreReason,
    MessageContent,
    ObservedDeletion,
    ObservedMessage,
)
from assistant.telegram.normalize import NormalizeError, classify, normalize

OWNER = 7000000001
CONTACT = 7000000005
BOT = 8845134387


def message(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "message_id": 183518,
        "date": 1792391029,
        "chat": {"id": CONTACT, "type": "private", "first_name": "Synthetic", "username": "syn"},
        "from_user": {"id": CONTACT, "is_bot": False, "first_name": "Synthetic"},
        "text": "synthetic text",
        "business_connection_id": "synthetic-conn",
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, Direction.CONTACT),
        ({"from_user": {"id": OWNER, "is_bot": False}}, Direction.OWNER_MANUAL),
        (
            {"from_user": {"id": OWNER, "is_bot": False}, "is_from_offline": True},
            Direction.OWNER_AUTOMATED,
        ),
        (
            {"from_user": {"id": OWNER, "is_bot": False}, "sender_business_bot": {"id": BOT}},
            Direction.ASSISTANT_SENT,
        ),
        (
            # Another business bot wrote as the owner: owner-authored, not presence.
            {"from_user": {"id": OWNER, "is_bot": False}, "sender_business_bot": {"id": 42}},
            Direction.OWNER_AUTOMATED,
        ),
        ({"chat": {"id": BOT, "type": "private"}}, IgnoreReason.BOT_CHAT),
        ({"chat": {"id": OWNER, "type": "private"}}, IgnoreReason.SAVED_MESSAGES),
        ({"chat": {"id": 777000, "type": "private"}}, IgnoreReason.SERVICE_CHAT),
        ({"from_user": {"id": CONTACT, "is_bot": True}}, IgnoreReason.BOT_COUNTERPART),
        ({"chat": {"id": -100123, "type": "supergroup"}}, IgnoreReason.NOT_PRIVATE),
    ],
    ids=[
        "contact",
        "owner_manual",
        "owner_automated",
        "assistant_sent",
        "other_business_bot",
        "bot_chat",
        "saved_messages",
        "service_chat",
        "bot_counterpart",
        "not_private",
    ],
)
def test_classification_follows_the_architecture_order(
    overrides: dict[str, Any], expected: Direction | IgnoreReason
) -> None:
    assert classify(message(**overrides), owner_user_id=OWNER, bot_id=BOT) is expected


@pytest.mark.parametrize("sender_key", ["from_user", "from"])
def test_owner_messages_are_never_classified_as_contact(sender_key: str) -> None:
    """The takeover signal (TF §12 LV-02): an owner message must never be
    mistaken for the contact's, under either sender spelling."""
    body = message()
    del body["from_user"]
    body[sender_key] = {"id": OWNER, "is_bot": False}
    verdict = classify(body, owner_user_id=OWNER, bot_id=BOT)
    assert verdict is Direction.OWNER_MANUAL
    assert verdict is not Direction.CONTACT


def test_a_message_normalizes_with_the_contact_taken_from_the_chat() -> None:
    update = {"update_id": 1, "business_message": message()}
    event = normalize(update, "business_message", owner_user_id=OWNER, bot_id=BOT)
    assert isinstance(event, ObservedMessage)
    assert event.business_chat_id == CONTACT
    assert event.telegram_message_id == 183518
    assert event.sent_at == datetime(2026, 10, 19, 6, 23, 49, tzinfo=UTC)
    assert event.direction is Direction.CONTACT
    assert event.counterpart.display_name == "Synthetic"
    assert event.counterpart.username == "syn"
    assert event.content == MessageContent("text", "synthetic text")
    assert event.is_edit is False


def test_an_owner_message_still_names_the_contact_as_counterpart() -> None:
    body = message(from_user={"id": OWNER, "is_bot": False, "first_name": "Owner"})
    event = normalize(
        {"business_message": body}, "business_message", owner_user_id=OWNER, bot_id=BOT
    )
    assert isinstance(event, ObservedMessage)
    assert event.direction is Direction.OWNER_MANUAL
    assert event.counterpart.telegram_user_id == CONTACT
    assert event.counterpart.display_name == "Synthetic"


def test_an_edit_carries_its_edit_date_and_flag() -> None:
    body = message(edit_date=1792391100, text="synthetic edited")
    event = normalize(
        {"edited_business_message": body},
        "edited_business_message",
        owner_user_id=OWNER,
        bot_id=BOT,
    )
    assert isinstance(event, ObservedMessage)
    assert event.is_edit is True
    assert event.edited_at == datetime(2026, 10, 19, 6, 25, tzinfo=UTC)


def test_ignored_updates_become_ignored_events() -> None:
    body = message(chat={"id": BOT, "type": "private"})
    event = normalize(
        {"business_message": body}, "business_message", owner_user_id=OWNER, bot_id=BOT
    )
    assert event == IgnoredUpdate(IgnoreReason.BOT_CHAT)


def test_a_deletion_normalizes_to_ids_only() -> None:
    body = {
        "business_connection_id": "synthetic-conn",
        "chat": {"id": CONTACT, "type": "private", "first_name": "Synthetic"},
        "message_ids": [183388, 183389],
    }
    event = normalize(
        {"deleted_business_messages": body},
        "deleted_business_messages",
        owner_user_id=OWNER,
        bot_id=BOT,
    )
    assert event == ObservedDeletion(CONTACT, (183388, 183389))


def test_the_largest_photo_is_recorded_without_its_file_id() -> None:
    body = message(
        text=None,
        caption="synthetic caption",
        photo=[
            {
                "file_id": "AAA",
                "file_unique_id": "u1",
                "width": 90,
                "height": 90,
                "file_size": 1000,
            },
            {
                "file_id": "BBB",
                "file_unique_id": "u2",
                "width": 800,
                "height": 800,
                "file_size": 60000,
            },
        ],
    )
    del body["text"]
    event = normalize(
        {"business_message": body}, "business_message", owner_user_id=OWNER, bot_id=BOT
    )
    assert isinstance(event, ObservedMessage)
    assert event.content.content_type == "photo"
    assert event.content.text == "synthetic caption"
    media = event.content.media
    assert media is not None
    assert media.as_json() == {
        "kind": "photo",
        "file_unique_id": "u2",
        "file_size": 60000,
        "width": 800,
        "height": 800,
    }
    assert "file_id" not in media.as_json()


@pytest.mark.parametrize("kind", ["voice", "document", "video_note"])
def test_media_never_carries_a_file_id_or_file_name(kind: str) -> None:
    raw = {
        "file_id": "SECRET-CAPABILITY",
        "file_unique_id": "u9",
        "file_name": "synthetic-name.pdf",
        "mime_type": "application/pdf",
        "duration": 7,
        "file_size": 123,
    }
    body = message(**{kind: raw})
    del body["text"]
    event = normalize(
        {"business_message": body}, "business_message", owner_user_id=OWNER, bot_id=BOT
    )
    assert isinstance(event, ObservedMessage)
    assert event.content.media is not None
    dumped = event.content.media.as_json()
    assert "SECRET-CAPABILITY" not in str(dumped)
    assert "synthetic-name" not in str(dumped)
    assert dumped["kind"] == kind


def test_a_shared_contact_card_stores_its_kind_but_not_the_phone_number() -> None:
    body = message(contact={"phone_number": "+998901234567", "first_name": "Synthetic"})
    del body["text"]
    event = normalize(
        {"business_message": body}, "business_message", owner_user_id=OWNER, bot_id=BOT
    )
    assert isinstance(event, ObservedMessage)
    assert event.content == MessageContent("contact")
    assert "+998" not in repr(event)


def test_content_hash_ignores_the_edit_date() -> None:
    a = normalize(
        {"business_message": message()}, "business_message", owner_user_id=OWNER, bot_id=BOT
    )
    b = normalize(
        {"edited_business_message": message(edit_date=1792391999)},
        "edited_business_message",
        owner_user_id=OWNER,
        bot_id=BOT,
    )
    assert isinstance(a, ObservedMessage)
    assert isinstance(b, ObservedMessage)
    assert a.content.content_hash == b.content.content_hash
    changed = normalize(
        {"business_message": message(text="synthetic other")},
        "business_message",
        owner_user_id=OWNER,
        bot_id=BOT,
    )
    assert isinstance(changed, ObservedMessage)
    assert changed.content.content_hash != a.content.content_hash


@pytest.mark.parametrize(
    "broken",
    [
        {"message_id": None},
        {"date": "not a date"},
        {"chat": {"type": "private"}},
        {"from_user": {"is_bot": False}},
    ],
    ids=["no message_id", "bad date", "no chat id", "no sender id"],
)
def test_missing_required_fields_raise_a_deterministic_error(broken: dict[str, Any]) -> None:
    body = message(**broken)
    with pytest.raises(NormalizeError):
        normalize({"business_message": body}, "business_message", owner_user_id=OWNER, bot_id=BOT)


def test_an_unknown_kind_is_deterministic_too() -> None:
    with pytest.raises(NormalizeError):
        normalize(
            {"business_connection": {}}, "business_connection", owner_user_id=OWNER, bot_id=BOT
        )
