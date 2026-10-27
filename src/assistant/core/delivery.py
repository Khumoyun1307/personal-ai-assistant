"""Provider-neutral outcomes of an outbound call; uncertain writes never retry."""

from dataclasses import dataclass
from enum import StrEnum


class ErrorClass(StrEnum):
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


@dataclass(frozen=True, slots=True)
class SendResult:
    """The outcome of one send. `ok` is the only success."""

    error_class: ErrorClass
    message_id: int | None = None
    error_code: str | None = None
    retry_after: int | None = None

    @property
    def ok(self) -> bool:
        return self.error_class is ErrorClass.OK

    @property
    def delivery_uncertain(self) -> bool:
        return self.error_class in (ErrorClass.AMBIGUOUS, ErrorClass.TRANSIENT)
