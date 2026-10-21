"""Inline keyboards for confirmation cards (ARCHITECTURE §6.7, S01.3 primitive).

A card is a control-chat message with buttons whose `callback_data` carries
`<kind>:<token>:<op>`. The token is the single-use id stored on the card's
`pending_inputs` row (`accounts/service.py::create_card`); the handler
claims it under a row lock, so a double tap makes exactly one transition.

Owner-facing controls appear only in the control chat (AGENTS §7).
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from assistant.accounts.callbacks import encode


def card_keyboard(kind: str, token: str, buttons: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """One row of buttons: `(label, op)` pairs, encoded with the card's token."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=encode(kind, token, op))
                for label, op in buttons
            ]
        ]
    )
