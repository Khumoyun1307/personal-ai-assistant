"""Telegram error classification (ARCHITECTURE §6.8).

Classification keys on the error *description*, never the HTTP status: business
errors surface as `400 Bad Request: <CODE>` (TF §4), and LV-09 showed that an
unreachable owner is also a 400, not the 403 everyone expects.

Bot API calls raise aiogram exceptions, which carry no status attribute; the
status is the exception class, and `classify_exception` is the entry point for
them.

The distinction that matters most is **transient vs deterministic**. A transient
error must leave work pending and be retried; a deterministic one must not be
retried forever. Getting this backwards either loses messages or spins.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramConflictError,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramNetworkError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)


class ErrorClass(StrEnum):
    """How the caller should react. Ordered roughly by severity."""

    OK = "ok"
    RATE_LIMITED = "rate_limited"
    WINDOW_CLOSED = "window_closed"
    PEER_UNAVAILABLE = "peer_unavailable"
    CONNECTION_INVALID = "connection_invalid"
    CHAT_UNREACHABLE = "chat_unreachable"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"
    BAD_REQUEST = "bad_request"
    TRANSIENT = "transient"
    AMBIGUOUS = "ambiguous"


#: Classes that mean "try again later"; the work stays pending and does not
#: count as an attempt (§6.2).
RETRYABLE: frozenset[ErrorClass] = frozenset({ErrorClass.RATE_LIMITED, ErrorClass.TRANSIENT})

#: Never retried automatically. AMBIGUOUS in particular: the send may have
#: happened, and retrying could double-send (§6 invariant 6).
TERMINAL: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.WINDOW_CLOSED,
        ErrorClass.PEER_UNAVAILABLE,
        ErrorClass.CONNECTION_INVALID,
        ErrorClass.CHAT_UNREACHABLE,
        ErrorClass.FORBIDDEN,
        ErrorClass.BAD_REQUEST,
        ErrorClass.AMBIGUOUS,
    }
)


@dataclass(frozen=True, slots=True)
class ClassifiedError:
    """A Telegram failure, reduced to something decisions can be made on."""

    error_class: ErrorClass
    code: str | None = None
    retry_after: int | None = None

    @property
    def retryable(self) -> bool:
        return self.error_class in RETRYABLE


#: Description fragments → class. Checked in order; first match wins, so more
#: specific fragments must come first.
_DESCRIPTION_RULES: tuple[tuple[str, ErrorClass], ...] = (
    # LV-08, 2026-10-21: an expired 24 h window fails a business send or a
    # typing action with PEER_ID_INVALID, not with the MTProto code the docs
    # suggest. The MTProto rule stays in case the Bot API ever surfaces it.
    ("peer_id_invalid", ErrorClass.WINDOW_CLOSED),
    ("business_peer_usage_missing", ErrorClass.WINDOW_CLOSED),
    # Paused, excluded, no reply right (LV-04, LV-12) — and also an *edit*
    # after the window closed (LV-08), which Telegram does not distinguish.
    ("business_peer_invalid", ErrorClass.PEER_UNAVAILABLE),
    ("business_connection_invalid", ErrorClass.CONNECTION_INVALID),
    ("business connection not found", ErrorClass.CONNECTION_INVALID),
    # LV-09, 2026-10-18: this is what an owner who never pressed Start returns.
    ("chat not found", ErrorClass.CHAT_UNREACHABLE),
    ("user not found", ErrorClass.CHAT_UNREACHABLE),
    ("bot was blocked by the user", ErrorClass.FORBIDDEN),
    ("user is deactivated", ErrorClass.FORBIDDEN),
    ("terminated by other getupdates", ErrorClass.CONFLICT),
    ("conflict", ErrorClass.CONFLICT),
    ("too many requests", ErrorClass.RATE_LIMITED),
    ("flood", ErrorClass.RATE_LIMITED),
)


def classify(
    description: str | None, *, status: int | None = None, retry_after: int | None = None
) -> ClassifiedError:
    """Classify a Telegram failure.

    `description` is Telegram's message; `status` is only a fallback, because
    business errors arrive as 400 regardless of meaning.
    """
    text = (description or "").lower()

    for fragment, error_class in _DESCRIPTION_RULES:
        if fragment in text:
            return ClassifiedError(error_class, code=fragment, retry_after=retry_after)

    if status == 429:
        return ClassifiedError(ErrorClass.RATE_LIMITED, retry_after=retry_after)
    if status == 409:
        return ClassifiedError(ErrorClass.CONFLICT)
    if status == 403:
        return ClassifiedError(ErrorClass.FORBIDDEN)
    if status is not None and 500 <= status < 600:
        return ClassifiedError(ErrorClass.TRANSIENT, retry_after=retry_after)
    if status == 400:
        return ClassifiedError(ErrorClass.BAD_REQUEST)
    # No status at all means the request never got an answer: a timeout or a
    # reset. For a read that is transient; a *send* must be treated as
    # ambiguous by its caller (§6 invariant 6).
    if status is None:
        return ClassifiedError(ErrorClass.TRANSIENT, retry_after=retry_after)
    return ClassifiedError(ErrorClass.BAD_REQUEST)


#: The HTTP status behind each aiogram exception. aiogram's exceptions carry no
#: status attribute: `BaseSession.check_response` (aiogram 3.31) encodes the
#: status in the exception CLASS. First match wins, and the order matters:
#: `TelegramEntityTooLarge` subclasses `TelegramNetworkError` although Telegram
#: did answer it, `TelegramNetworkError` subclasses `TelegramAPIError` although
#: Telegram did not, and `TelegramRetryAfter` is raised for any status whose
#: answer carries `retry_after`. `None` means no answer arrived.
_STATUS_BY_EXCEPTION: tuple[tuple[type[TelegramAPIError], int | None], ...] = (
    (TelegramRetryAfter, 429),
    (TelegramMigrateToChat, 400),
    (TelegramBadRequest, 400),
    (TelegramUnauthorizedError, 401),
    (TelegramForbiddenError, 403),
    (TelegramNotFound, 404),
    (TelegramConflictError, 409),
    (TelegramEntityTooLarge, 413),
    (TelegramServerError, 500),
    (TelegramNetworkError, None),
    # Any other status: aiogram raises the base class only after Telegram
    # answered `ok: false`, so it is a refusal, not a lost request.
    (TelegramAPIError, 400),
)


def classify_exception(error: BaseException) -> ClassifiedError:
    """Classify an exception raised by an aiogram Bot API call.

    A refusal Telegram answered (any 4xx) is definite: nothing was sent and
    retrying the same request cannot help (AGENTS §7). An exception without an
    answer — `TelegramNetworkError` (timeout, reset), a bare `TimeoutError`, or
    anything that is not an aiogram API error — is TRANSIENT, and so is a 5xx:
    in both cases the request may have been processed, so a *send* is
    ambiguous (the gateway reports it as `delivery_uncertain`, §6 invariant 6).
    Description rules apply only to Telegram's own answers.
    """
    if not isinstance(error, TelegramAPIError):
        return ClassifiedError(ErrorClass.TRANSIENT)
    status = next(status for kind, status in _STATUS_BY_EXCEPTION if isinstance(error, kind))
    if status is None:
        return ClassifiedError(ErrorClass.TRANSIENT)
    retry_after = error.retry_after if isinstance(error, TelegramRetryAfter) else None
    return classify(error.message, status=status, retry_after=retry_after)
