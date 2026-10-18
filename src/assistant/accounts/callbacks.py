"""Callback tokens for control-chat buttons (ARCHITECTURE §6.7).

Format: `<kind>:<token>:<op>`, at most 64 bytes — Telegram's hard limit on
`callback_data`.

The token is a random id stored on the target row, never the row id itself.
Callback data travels to the client and comes back from it, so a predictable
`approve:actions:41:send` would let anyone who can press a button in any chat
guess a valid payload for someone else's action. A random token is
unguessable, and the handler still verifies the presser owns the row (§6
invariant 12) and that its state permits the operation.
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass

#: base62. No separators, so it can never collide with the `:` delimiter.
_ALPHABET = string.ascii_letters + string.digits
TOKEN_LENGTH = 10

#: Telegram rejects callback_data over 64 bytes outright.
MAX_CALLBACK_DATA_BYTES = 64


class CallbackDataError(ValueError):
    """Malformed callback data. Treated as hostile, not as a bug."""


def new_token() -> str:
    """A fresh unguessable token for a row that will carry buttons."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(TOKEN_LENGTH))


@dataclass(frozen=True, slots=True)
class CallbackData:
    """A parsed button press."""

    kind: str
    token: str
    op: str

    def encode(self) -> str:
        data = f"{self.kind}:{self.token}:{self.op}"
        if len(data.encode()) > MAX_CALLBACK_DATA_BYTES:
            raise CallbackDataError("callback data exceeds Telegram's 64-byte limit")
        return data


def encode(kind: str, token: str, op: str) -> str:
    return CallbackData(kind=kind, token=token, op=op).encode()


def parse(data: str | None) -> CallbackData:
    """Parse callback data from a button press.

    Everything here arrives from the client, so it is validated rather than
    trusted: shape, length, and character set.
    """
    if not data:
        raise CallbackDataError("empty callback data")
    if len(data.encode()) > MAX_CALLBACK_DATA_BYTES:
        raise CallbackDataError("callback data too long")

    parts = data.split(":")
    if len(parts) != 3:
        raise CallbackDataError("expected <kind>:<token>:<op>")

    kind, token, op = parts
    if not kind or not token or not op:
        raise CallbackDataError("empty component in callback data")
    if not all(char in _ALPHABET for char in token):
        raise CallbackDataError("token is not base62")
    if not kind.replace("_", "").isalnum() or not op.replace("_", "").isalnum():
        raise CallbackDataError("kind and op must be alphanumeric")
    return CallbackData(kind=kind, token=token, op=op)
