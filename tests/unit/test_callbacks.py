"""Callback tokens (ARCHITECTURE §6.7).

Callback data leaves the server and comes back from a client, so everything
here is validated rather than trusted.
"""

from __future__ import annotations

import pytest

from assistant.accounts.callbacks import (
    MAX_CALLBACK_DATA_BYTES,
    TOKEN_LENGTH,
    CallbackDataError,
    encode,
    new_token,
    parse,
)


def test_a_token_is_unguessable_and_the_right_length() -> None:
    """Predictable callback data would let anyone forge a valid button press."""
    tokens = {new_token() for _ in range(200)}
    assert len(tokens) == 200  # no collisions in 200 draws
    assert all(len(token) == TOKEN_LENGTH for token in tokens)
    assert all(token.isalnum() for token in tokens)


def test_round_trip() -> None:
    token = new_token()
    parsed = parse(encode("approve", token, "send"))
    assert (parsed.kind, parsed.token, parsed.op) == ("approve", token, "send")


def test_encoded_data_fits_telegrams_limit() -> None:
    data = encode("approve", new_token(), "send")
    assert len(data.encode()) <= MAX_CALLBACK_DATA_BYTES


def test_over_long_data_is_rejected_at_encode_time() -> None:
    """Better to fail building the button than to have Telegram reject it."""
    with pytest.raises(CallbackDataError):
        encode("k" * 40, new_token(), "o" * 40)


@pytest.mark.parametrize(
    "data",
    [
        None,
        "",
        "approve",
        "approve:token",
        "approve:token:send:extra",
        "approve::send",
        ":token:send",
        "approve:tok en:send",
        "approve:tok:send ",
        "x" * 100,
    ],
)
def test_malformed_data_is_rejected(data: str | None) -> None:
    with pytest.raises(CallbackDataError):
        parse(data)


def test_a_token_with_a_delimiter_cannot_be_smuggled_in() -> None:
    """base62 has no `:`, so a token can never split into extra fields."""
    with pytest.raises(CallbackDataError):
        parse("approve:ab:cd:send")
