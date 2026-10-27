"""Pure action lifecycle vocabulary (ARCHITECTURE §9.4; P2 decisions R4-R6)."""

from enum import StrEnum
from types import MappingProxyType


class ActionKind(StrEnum):
    SEND_MESSAGE = "send_message"
    EDIT_MESSAGE = "edit_message"
    DELETE_OWN = "delete_own"


class ActionOrigin(StrEnum):
    OWNER_DICTATED = "owner_dictated"
    AGENT_PROPOSAL = "agent_proposal"
    DELEGATION_AUTO = "delegation_auto"


class ActionState(StrEnum):
    PROPOSED = "proposed"
    AWAITING_APPROVAL = "awaiting_approval"
    REVISING = "revising"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DELIVERY_UNCERTAIN = "delivery_uncertain"
    REJECTED = "rejected"
    OUTDATED = "outdated"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


LIVE_CARD = frozenset({ActionState.AWAITING_APPROVAL, ActionState.REVISING})
PENDING = LIVE_CARD | {ActionState.PROPOSED, ActionState.APPROVED, ActionState.SCHEDULED}
TERMINAL = frozenset(
    {
        ActionState.SUCCEEDED,
        ActionState.FAILED,
        ActionState.REJECTED,
        ActionState.OUTDATED,
        ActionState.EXPIRED,
        ActionState.CANCELLED,
        ActionState.BLOCKED,
    }
)

TRANSITIONS: frozenset[tuple[ActionState, ActionState]] = frozenset(
    {
        (ActionState.PROPOSED, ActionState.AWAITING_APPROVAL),
        (ActionState.PROPOSED, ActionState.APPROVED),
        (ActionState.PROPOSED, ActionState.BLOCKED),
        (ActionState.PROPOSED, ActionState.CANCELLED),
        (ActionState.AWAITING_APPROVAL, ActionState.APPROVED),
        (ActionState.AWAITING_APPROVAL, ActionState.REVISING),
        (ActionState.AWAITING_APPROVAL, ActionState.REJECTED),
        (ActionState.AWAITING_APPROVAL, ActionState.OUTDATED),
        (ActionState.AWAITING_APPROVAL, ActionState.EXPIRED),
        (ActionState.AWAITING_APPROVAL, ActionState.CANCELLED),
        (ActionState.REVISING, ActionState.AWAITING_APPROVAL),
        (ActionState.REVISING, ActionState.OUTDATED),
        (ActionState.REVISING, ActionState.EXPIRED),
        (ActionState.REVISING, ActionState.CANCELLED),
        (ActionState.APPROVED, ActionState.SCHEDULED),
        (ActionState.APPROVED, ActionState.EXECUTING),
        (ActionState.APPROVED, ActionState.OUTDATED),
        (ActionState.APPROVED, ActionState.EXPIRED),
        (ActionState.APPROVED, ActionState.BLOCKED),
        (ActionState.APPROVED, ActionState.CANCELLED),
        (ActionState.SCHEDULED, ActionState.APPROVED),
        (ActionState.SCHEDULED, ActionState.CANCELLED),
        (ActionState.EXECUTING, ActionState.SUCCEEDED),
        (ActionState.EXECUTING, ActionState.FAILED),
        (ActionState.EXECUTING, ActionState.APPROVED),
        (ActionState.EXECUTING, ActionState.DELIVERY_UNCERTAIN),
        (ActionState.EXECUTING, ActionState.EXPIRED),
        (ActionState.DELIVERY_UNCERTAIN, ActionState.SUCCEEDED),
        (ActionState.DELIVERY_UNCERTAIN, ActionState.FAILED),
    }
)


class StateReason(StrEnum):
    SUPERSEDED = "superseded"
    NEW_CONTACT_MESSAGE = "new_contact_message"
    TAKEOVER = "takeover"
    OWNER_EDIT = "owner_edit"
    SOURCE_EDITED = "source_edited"
    SOURCE_DELETED = "source_deleted"
    RIGHTS_CHANGED = "rights_changed"
    CONNECTION_DISABLED = "connection_disabled"
    CONNECTION_INVALID = "connection_invalid"
    IGNORE_MODE = "ignore_mode"
    DATA_DELETED = "data_deleted"
    CONSENT_REVOKED = "consent_revoked"
    INGEST_ERROR = "ingest_error"
    RESTORED_FROM_BACKUP = "restored_from_backup"
    WINDOW_CLOSED = "window_closed"
    MAX_AGE = "max_age"
    OWNER_REJECTED = "owner_rejected"
    POLICY_DENIED = "policy_denied"
    SENDING_DISABLED = "sending_disabled"
    TOO_LONG = "too_long"
    AUTONOMY_PAUSED = "autonomy_paused"
    SENT = "sent"
    NOT_DELIVERED = "not_delivered"
    AMBIGUOUS = "ambiguous"
    STALE_EXECUTING = "stale_executing"
    LATE_RESULT = "late_result"


STATE_REASON_EXPLANATIONS = MappingProxyType(
    {
        StateReason.SUPERSEDED: "A newer draft replaced this one.",
        StateReason.NEW_CONTACT_MESSAGE: "A newer message arrived after this draft was made.",
        StateReason.TAKEOVER: "You replied in this chat after this draft was made.",
        StateReason.OWNER_EDIT: "You edited a message in this chat.",
        StateReason.SOURCE_EDITED: "A message used for this draft was edited.",
        StateReason.SOURCE_DELETED: "A message used for this draft was deleted.",
        StateReason.RIGHTS_CHANGED: "The connection's permissions changed; make a new draft.",
        StateReason.CONNECTION_DISABLED: "Chat Automation is disconnected or disabled.",
        StateReason.CONNECTION_INVALID: "Telegram no longer accepts the old connection.",
        StateReason.IGNORE_MODE: "This chat is set to Ignore.",
        StateReason.DATA_DELETED: "The data used for this draft was deleted.",
        StateReason.CONSENT_REVOKED: "AI permission for managed chats was revoked.",
        StateReason.INGEST_ERROR: (
            "A message in this chat could not be processed; this draft may be outdated."
        ),
        StateReason.RESTORED_FROM_BACKUP: "A backup was restored; check the chat before replying.",
        StateReason.WINDOW_CLOSED: "Telegram's 24-hour reply window has closed.",
        StateReason.MAX_AGE: "This draft's approval period has expired.",
        StateReason.OWNER_REJECTED: "You rejected this draft.",
        StateReason.POLICY_DENIED: "A required sending check did not pass.",
        StateReason.SENDING_DISABLED: "Sending is off; you can copy the text to send it yourself.",
        StateReason.TOO_LONG: "The reply must contain between 1 and 4096 characters.",
        StateReason.AUTONOMY_PAUSED: "Automatic replies are paused.",
        StateReason.SENT: "Telegram confirmed that this reply was sent.",
        StateReason.NOT_DELIVERED: "Telegram did not send this reply.",
        StateReason.AMBIGUOUS: (
            "Delivery is uncertain; check the chat. This reply will not be resent."
        ),
        StateReason.STALE_EXECUTING: (
            "The attempt did not finish; check the chat. It will not be resent."
        ),
        StateReason.LATE_RESULT: "Telegram's result arrived after delivery was marked uncertain.",
    }
)

STATE_LABELS = MappingProxyType(
    {
        ActionState.PROPOSED: "Draft",
        ActionState.AWAITING_APPROVAL: "Draft — awaiting your approval",
        ActionState.REVISING: "Revising…",
        ActionState.APPROVED: "Sending…",
        ActionState.SCHEDULED: "Scheduled",
        ActionState.EXECUTING: "Sending…",
        ActionState.SUCCEEDED: "Sent ✓",
        ActionState.FAILED: "Not sent",
        ActionState.DELIVERY_UNCERTAIN: "Delivery uncertain — please check the chat",
        ActionState.REJECTED: "Rejected",
        ActionState.OUTDATED: "Outdated",
        ActionState.EXPIRED: "Expired",
        ActionState.CANCELLED: "Cancelled",
        ActionState.BLOCKED: "Not sendable",
    }
)
