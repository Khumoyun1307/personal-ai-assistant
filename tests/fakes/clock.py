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
    """Record waits, optionally advancing the clock instead of sleeping.

    Pass a clock when the test needs completed waits to consume time. Scenario
    gateways use FakeGatewayClock to keep business time unchanged (AGENTS §11).
    """

    def __init__(self, clock: FakeClock | None = None) -> None:
        self._clock = clock
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        if self._clock is not None:
            self._clock.advance(seconds=seconds)


class FakeGatewayClock:
    """Simulate gateway waits without advancing the scenario's business time.

    Completed sleeps consume local time, so sequential sends do not build a
    phantom backlog. Explicit scenario advances are still visible to the
    gateway. Other services keep the original clock, preserving period keys,
    deadlines and scheduled jobs until the test explicitly moves them.
    """

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self._now = clock.now()
        self.waits: list[float] = []

    def now(self) -> datetime:
        return max(self._clock.now(), self._now)

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self._now = self.now() + timedelta(seconds=seconds)
