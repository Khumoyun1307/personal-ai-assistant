"""The 24-hour reply window (TF C-03, ARCHITECTURE §6.6).

The single place that knows how the window is computed. It is an
approximation: some incoming events never reach the bot, so every send and
edit still handles the closed-window error as an expected outcome.

Verified (TF §12 LV-08, 2026-10-21): the window closes after 24 h without a
contact message. Whether an owner message or a contact *edit* reopens it is
not yet observed; until it is, only contact messages count — the conservative
reading of the `can_reply` right. `window_anchor` is where that assumption
lives.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from assistant.conversations.models import Conversation

REPLY_WINDOW = timedelta(hours=24)
#: Sends stop this long before the computed close, because the local clock
#: and Telegram's disagree by seconds and the penalty for being late is a
#: failed send the owner has to redo by hand.
SAFETY_MARGIN = timedelta(minutes=5)


def window_anchor(conversation: Conversation) -> datetime | None:
    """The time the current window started: the newest contact message."""
    return conversation.last_contact_msg_at


def window_closes_at(conversation: Conversation) -> datetime | None:
    anchor = window_anchor(conversation)
    return None if anchor is None else anchor + REPLY_WINDOW


def window_open(conversation: Conversation, now: datetime) -> bool:
    closes_at = window_closes_at(conversation)
    return closes_at is not None and now < closes_at - SAFETY_MARGIN
