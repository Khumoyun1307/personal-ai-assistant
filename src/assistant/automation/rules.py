"""Versioned persisted automation rules remain independent of prompt schemas."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class RuleBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int


# ------------------------------------------------------------ time automations (S05.3b)
#: `ScheduleRule.dtstart_local` as stored: a naive local wall time to the minute.
DTSTART_FORMAT = "%Y-%m-%dT%H:%M"
#: The longest lateness any kind may tolerate (a week).
MAX_LATENESS_MINUTES = 7 * 24 * 60


class ScheduleRule(RuleBase):
    """A time automation's rule (S05.3b §3.1). The schedule itself lives in
    the row's columns (`rrule`, `run_at`, `timezone`, `tz_policy`,
    `next_run_at`); after `max_lateness_minutes` an occurrence is skipped
    with a note instead of firing late (FR-FUP-08)."""

    schema_version: Literal[1] = 1
    max_lateness_minutes: int = Field(ge=0, le=MAX_LATENESS_MINUTES)
    #: The automation's first local occurrence, `"YYYY-MM-DDTHH:MM"`, for an
    #: `rrule` (P2-T27); None for a one-off `run_at`.
    dtstart_local: str | None = Field(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$", max_length=16
    )

    def start(self) -> datetime | None:
        """`dtstart_local` as a naive datetime; ValueError for an impossible
        date the pattern lets through (`2026-02-30T09:00`)."""
        if self.dtstart_local is None:
            return None
        return datetime.strptime(self.dtstart_local, DTSTART_FORMAT)  # noqa: DTZ007 - wall time


class DigestRule(ScheduleRule):
    """The daily digest's rule: no content (S05.3b §3.1)."""

    kind: Literal["digest"] = "digest"


def load_schedule_rule(kind: str, spec: Mapping[str, Any]) -> ScheduleRule:
    """The stored rule of a time automation; raises `pydantic.ValidationError`
    when it no longer validates (the caller pauses the row: fail closed)."""
    if kind == "digest":
        return DigestRule.model_validate(spec)
    return ScheduleRule.model_validate(spec)
