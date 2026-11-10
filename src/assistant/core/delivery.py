"""Provider-neutral outcomes of an outbound call; uncertain writes never retry."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

#: Bot API sendMessage text limit: 1-4096 characters (core.telegram.org/bots/api#sendmessage).
MAX_TEXT_LENGTH = 4096


@dataclass(slots=True)
class BusinessSendReservation:
    """An ephemeral, single-use capacity slot; never persisted or a send claim."""

    account_id: int
    conversation_id: int
    chat_id: int
    expires_at: datetime
    consumed: bool = False


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
