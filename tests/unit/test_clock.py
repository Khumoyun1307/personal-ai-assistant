"""The Clock port (AGENTS §10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from assistant.core.clock import Clock, SystemClock, ensure_utc
from tests.fakes.clock import FakeClock


def test_system_clock_returns_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_both_clocks_satisfy_the_port() -> None:
    assert isinstance(SystemClock(), Clock)
    assert isinstance(FakeClock(), Clock)


def test_fake_clock_only_moves_when_told() -> None:
    clock = FakeClock(datetime(2026, 3, 1, 9, 0, tzinfo=UTC))
    first = clock.now()
    assert clock.now() == first
    assert clock.advance(minutes=90) == datetime(2026, 3, 1, 10, 30, tzinfo=UTC)


def test_ensure_utc_rejects_naive_datetimes() -> None:
    """A naive value in a timestamptz column silently means server-local."""
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2026, 3, 1, 9, 0))  # noqa: DTZ001 - naive is the point


def test_ensure_utc_converts_other_zones() -> None:
    tashkent = timezone(timedelta(hours=5))
    converted = ensure_utc(datetime(2026, 3, 1, 14, 0, tzinfo=tashkent))
    assert converted == datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
