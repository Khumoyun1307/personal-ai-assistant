"""Telegram error classification pinned to the strings observed live.

Every row here is an error string that an owner-executed live test actually
produced (TELEGRAM_FEASIBILITY §12). Business errors arrive as HTTP 400 with
the meaning in the description, so classification keys on the description
(ARCHITECTURE §6.8). Never weaken these: a misclassified closed window or
pause turns into a retry against a chat the owner cannot see.
"""

from __future__ import annotations

import pytest

from assistant.telegram.errors import ErrorClass, classify

OBSERVED = [
    # LV-08, 2026-10-21: expired 24 h window, send and typing.
    ("Bad Request: PEER_ID_INVALID", ErrorClass.WINDOW_CLOSED),
    # LV-04/LV-12 pause, exclusion, no reply right — and LV-08 edit after close.
    ("Bad Request: BUSINESS_PEER_INVALID", ErrorClass.PEER_UNAVAILABLE),
    # LV-01: the replaced connection id.
    ("Bad Request: BUSINESS_CONNECTION_INVALID", ErrorClass.CONNECTION_INVALID),
    # LV-09: the owner never pressed Start.
    ("Bad Request: chat not found", ErrorClass.CHAT_UNREACHABLE),
]


@pytest.mark.parametrize(("description", "expected"), OBSERVED)
def test_live_observed_business_errors_classify_as_designed(
    description: str, expected: ErrorClass
) -> None:
    # aiogram prefixes "Telegram server says - "; the prefix must not matter.
    result = classify(f"Telegram server says - {description}", status=400)
    assert result.error_class is expected
    assert not result.retryable


def test_the_documented_mtproto_window_code_is_still_recognised() -> None:
    """Never observed via the Bot API (LV-08), kept from the MTProto docs."""
    result = classify("Bad Request: BUSINESS_PEER_USAGE_MISSING", status=400)
    assert result.error_class is ErrorClass.WINDOW_CLOSED


def test_a_closed_window_is_terminal_not_a_pause() -> None:
    """PEER_ID_INVALID must not be confused with BUSINESS_PEER_INVALID: only the
    latter marks a conversation `possibly_paused` (ARCHITECTURE §6.6)."""
    closed = classify("Bad Request: PEER_ID_INVALID", status=400)
    paused = classify("Bad Request: BUSINESS_PEER_INVALID", status=400)
    assert closed.error_class is not paused.error_class
    assert closed.code == "peer_id_invalid"
    assert paused.code == "business_peer_invalid"


def test_an_unknown_400_is_a_plain_bad_request() -> None:
    assert classify("Bad Request: SOMETHING_NEW", status=400).error_class is ErrorClass.BAD_REQUEST
