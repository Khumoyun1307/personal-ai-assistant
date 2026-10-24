"""Telegram error classification pinned to the strings observed live.

Every row here is an error string that an owner-executed live test actually
produced (TELEGRAM_FEASIBILITY §12). Business errors arrive as HTTP 400 with
the meaning in the description, so classification keys on the description
(ARCHITECTURE §6.8). Never weaken these: a misclassified closed window or
pause turns into a retry against a chat the owner cannot see.
"""

from __future__ import annotations

import pytest
from aiogram.exceptions import TelegramNetworkError

from assistant.telegram.errors import ErrorClass, classify, classify_exception
from tests.fakes.sender import network_error, telegram_error

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


# ------------------------------------------- real aiogram exceptions (fix)
# Verification review, 2026-10-23: aiogram's exceptions carry no HTTP status,
# so reading `status_code` off them always got None and every refusal whose
# description matched no rule became TRANSIENT — reported as "uncertain" to
# the owner and retryable. The class aiogram raises IS the status.

DEFINITE = [
    # Telegram answered and refused: nothing was sent, nothing to retry blindly.
    (telegram_error("Bad Request: can't parse entities", status_code=400), ErrorClass.BAD_REQUEST),
    (telegram_error("Bad Request: message is too long", status_code=400), ErrorClass.BAD_REQUEST),
    (telegram_error("Unauthorized", status_code=401), ErrorClass.BAD_REQUEST),
    (telegram_error("Forbidden: something new", status_code=403), ErrorClass.FORBIDDEN),
    (telegram_error("Not Found", status_code=404), ErrorClass.BAD_REQUEST),
    (telegram_error("Conflict: something new", status_code=409), ErrorClass.CONFLICT),
    (telegram_error("Request Entity Too Large", status_code=413), ErrorClass.BAD_REQUEST),
    # An unlisted status still came with Telegram's `ok: false` answer.
    (telegram_error("Something odd", status_code=418), ErrorClass.BAD_REQUEST),
    # Description rules still win over the status (LV-09: this is a 400).
    (telegram_error("Bad Request: chat not found", status_code=400), ErrorClass.CHAT_UNREACHABLE),
]


@pytest.mark.parametrize(("error", "expected"), DEFINITE)
def test_a_telegram_refusal_is_definite_not_transient(
    error: Exception, expected: ErrorClass
) -> None:
    result = classify_exception(error)
    assert result.error_class is expected
    assert result.error_class is not ErrorClass.TRANSIENT
    assert not result.retryable


def test_flood_control_is_rate_limited_with_its_delay() -> None:
    error = telegram_error("Too Many Requests: retry after 7", status_code=429, retry_after=7)
    result = classify_exception(error)
    assert result.error_class is ErrorClass.RATE_LIMITED
    assert result.retry_after == 7


@pytest.mark.parametrize(
    "error",
    [
        network_error("Request timeout error"),
        network_error("ClientConnectorError: synthetic reset"),
        TimeoutError("synthetic timeout"),
        telegram_error("Internal Server Error", status_code=500),
        telegram_error("Bad Gateway", status_code=502),
        # Not from aiogram at all: we cannot tell whether Telegram got it.
        RuntimeError("synthetic failure"),
    ],
)
def test_no_answer_or_a_server_error_is_transient(error: Exception) -> None:
    """A send in this state may have arrived; the gateway reports it uncertain."""
    assert classify_exception(error).error_class is ErrorClass.TRANSIENT


def test_a_network_error_never_matches_a_description_rule() -> None:
    """Description rules are Telegram's words; a client-side message that
    happens to contain one must not turn "no answer" into a definite refusal."""
    error = network_error("ClientResponseError: synthetic chat not found")
    assert classify_exception(error).error_class is ErrorClass.TRANSIENT


def test_an_oversized_upload_is_a_refusal_not_a_network_error() -> None:
    """aiogram makes TelegramEntityTooLarge a subclass of TelegramNetworkError,
    but Telegram did answer it (HTTP 413)."""
    error = telegram_error("Request Entity Too Large", status_code=413)
    assert isinstance(error, TelegramNetworkError)
    assert classify_exception(error).error_class is ErrorClass.BAD_REQUEST
