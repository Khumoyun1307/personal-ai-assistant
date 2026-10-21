"""The 24 h reply window (TF C-03, §12 LV-08; ARCHITECTURE §6.6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from assistant.conversations.models import Conversation
from assistant.conversations.window import (
    REPLY_WINDOW,
    SAFETY_MARGIN,
    window_anchor,
    window_closes_at,
    window_open,
)

T0 = datetime(2026, 10, 19, 6, 23, 49, tzinfo=UTC)


def conversation(**fields: object) -> Conversation:
    return Conversation(account_id=1, business_chat_id=1, contact_id=1, **fields)


def test_no_contact_message_means_no_window() -> None:
    assert window_anchor(conversation()) is None
    assert window_closes_at(conversation()) is None
    assert window_open(conversation(), T0) is False


def test_the_window_is_anchored_on_the_last_contact_message_only() -> None:
    conv = conversation(
        last_contact_msg_at=T0,
        # Later owner and assistant activity does not extend it (conservative
        # reading of `can_reply` until the LV-08 reopen half is observed).
        last_owner_msg_at=T0 + timedelta(hours=20),
        last_assistant_msg_at=T0 + timedelta(hours=23),
    )
    assert window_anchor(conv) == T0
    assert window_closes_at(conv) == T0 + REPLY_WINDOW


def test_the_window_closes_a_safety_margin_early() -> None:
    conv = conversation(last_contact_msg_at=T0)
    assert window_open(conv, T0 + timedelta(hours=23, minutes=54)) is True
    assert window_open(conv, T0 + REPLY_WINDOW - SAFETY_MARGIN) is False
    assert window_open(conv, T0 + timedelta(hours=61)) is False  # LV-08, as run
