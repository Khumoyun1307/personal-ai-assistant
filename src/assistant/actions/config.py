"""Immutable settings injected into the actions domain."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal


@dataclass(frozen=True, slots=True)
class ActionsConfig:
    approval_max_age: timedelta
    draft_debounce: timedelta
    mark_uncertain_after: timedelta
    connection_stale_alert_after: timedelta
    sending_enabled: bool
    process_run_id: str
    assist_draft_trigger: Literal["off", "ingestion", "triage"]
    draft_route_model: str
