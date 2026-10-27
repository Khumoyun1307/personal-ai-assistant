"""Pure facts evaluated at proposal and immediately before execution."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from assistant.actions.states import ActionKind, ActionOrigin, ActionState


class Phase(StrEnum):
    PROPOSAL = "proposal"
    EXECUTION = "execution"


class Outcome(StrEnum):
    ALLOW_AUTO = "allow_auto"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"
    WAIT = "wait"


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    account_id: int
    status: str
    autonomy_paused: bool
    managed_consent_ok: bool


@dataclass(frozen=True, slots=True)
class ConnectionSnapshot:
    exists: bool
    status: str | None
    is_enabled: bool
    can_reply: bool


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    conversation_id: int
    account_id: int
    mode: str
    availability: str
    is_bot: bool
    last_contact_msg_at: datetime | None
    last_owner_msg_at: datetime | None
    autonomy_paused_reason: str | None


@dataclass(frozen=True, slots=True)
class Freshness:
    newest_owner_manual_sent_at: datetime | None
    newest_contact_message_id: int | None
    base_message_sent_at: datetime | None
    base_message_present: bool
    target_is_assistant_sent: bool | None


@dataclass(frozen=True, slots=True)
class IngestionState:
    pending_inbound: int
    barrier_passed: bool


@dataclass(frozen=True, slots=True)
class ActionFacts:
    action_id: int | None
    kind: ActionKind
    origin: ActionOrigin
    state: ActionState
    version: int
    text_length: int
    created_at: datetime
    expires_at: datetime | None
    shown_through_message_id: int | None
    owner_retry: bool


@dataclass(frozen=True, slots=True)
class PolicyContext:
    now: datetime
    phase: Phase
    account: AccountSnapshot
    connection: ConnectionSnapshot
    conversation: ConversationSnapshot
    freshness: Freshness
    ingestion: IngestionState
    action: ActionFacts
    ai_generated: bool
    sending_enabled: bool
    delegation: None = None


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class Decision:
    outcome: Outcome
    checks: tuple[CheckResult, ...]
