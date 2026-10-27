"""A Clock whose time only moves when a test moves it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class FakeClock:
    """Deterministic time. Tests never sleep (AGENTS §11)."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("FakeClock needs an aware datetime")

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> datetime:
        """Move time forward, e.g. `clock.advance(minutes=5)`."""
        self._now += timedelta(**delta)
        return self._now

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            raise ValueError("FakeClock needs an aware datetime")
        self._now = moment.astimezone(UTC)


class FakeSleep:
    """A sleep that records the wait asked for and returns at once.

    The FakeClock never moves by itself, so a real `asyncio.sleep` behind a
    rate limit waits its full length on every call (AGENTS §11)."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
