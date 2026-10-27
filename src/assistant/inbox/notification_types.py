"""Content-free notification types shared with domain callers."""

from dataclasses import dataclass
from enum import StrEnum


class AlertClass(StrEnum):
    SYSTEM = "system"
    STANDARD = "standard"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class NotificationKind(StrEnum):
    TRIAGE_URGENT = "triage_urgent"
    TRIAGE_DECISION = "triage_decision"
    TRIAGE_NEEDS_REPLY = "triage_needs_reply"
    TRIAGE_SUSPICIOUS = "triage_suspicious"
    TASK_CLOSED = "task_closed"
    COMMITMENT_DUE = "commitment_due"
    MONITOR_MATCH = "monitor_match"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class NotificationRefs:
    conversation_ids: tuple[int, ...] = ()
    contact_ids: tuple[int, ...] = ()
    task_ids: tuple[int, ...] = ()
    message_ids: tuple[int, ...] = ()
    automation_ids: tuple[int, ...] = ()
    action_ids: tuple[int, ...] = ()
    alert_kind: str | None = None
