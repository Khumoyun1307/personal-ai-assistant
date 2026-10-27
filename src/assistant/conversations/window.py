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

from datetime import datetime

from assistant.conversations.models import Conversation
from assistant.core import reply_window
from assistant.core.reply_window import REPLY_WINDOW as REPLY_WINDOW
from assistant.core.reply_window import SAFETY_MARGIN as SAFETY_MARGIN


def window_anchor(conversation: Conversation) -> datetime | None:
    """The time the current window started: the newest contact message."""
    return conversation.last_contact_msg_at


def window_closes_at(conversation: Conversation) -> datetime | None:
    return reply_window.closes_at(window_anchor(conversation))


def window_open(conversation: Conversation, now: datetime) -> bool:
    return reply_window.is_open(window_anchor(conversation), now)
