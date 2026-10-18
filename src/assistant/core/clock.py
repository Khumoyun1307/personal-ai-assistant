"""Time as an injected dependency (AGENTS §10).

Domain code never calls `datetime.now()`: tests must be able to control time
without sleeping, and schedule arithmetic must be reproducible. Everything is
UTC; the owner's timezone is applied only at presentation and scheduling
(ARCHITECTURE §10.1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Source of the current time. Implementations must return aware UTC."""

    def now(self) -> datetime:
        """The current instant, timezone-aware and in UTC."""
        ...


class SystemClock:
    """The real clock. The only implementation wired in production."""

    def now(self) -> datetime:
        return datetime.now(UTC)


def ensure_utc(moment: datetime) -> datetime:
    """Reject naive datetimes rather than silently assuming a timezone.

    A naive datetime that reaches a `timestamptz` column is interpreted as the
    server's local time, which is how "off by N hours" bugs get into stored
    schedules. Fail loudly instead.
    """
    if moment.tzinfo is None:
        raise ValueError("naive datetime: all times must be timezone-aware UTC")
    return moment.astimezone(UTC)
