"""Notification types shared by the P2 streams (F0 contract, F0-interfaces §8.3).

These are the F0 contract types, word for word in names and values: triage
(S05.1) and monitors (S05.4) enqueue with them, and the delivery policy
(S05.2) decides on them. This branch carries a copy until F0 lands them.
"""

from __future__ import annotations

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
    #: SYSTEM rows carry an `AlertKind` in `refs["alert_kind"]`.
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class NotificationRefs:
    """What an alert is about, as ids only: texts are rendered at delivery."""

    conversation_ids: tuple[int, ...] = ()
    contact_ids: tuple[int, ...] = ()
    task_ids: tuple[int, ...] = ()
    message_ids: tuple[int, ...] = ()
    automation_ids: tuple[int, ...] = ()
    action_ids: tuple[int, ...] = ()
    alert_kind: str | None = None
