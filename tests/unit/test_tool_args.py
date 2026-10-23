"""Argument validation shared by the read tools (S03.1 review items J, L)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from assistant.agent.tools.common import (
    MAX_ID,
    check_id,
    day_range,
    decode_cursor,
    encode_cursor,
    parse_local_date,
)


def test_a_cursor_round_trips_exactly() -> None:
    position = (datetime(2026, 10, 18, 20, 6, 40, 123456, tzinfo=UTC), 987654321)
    assert decode_cursor(encode_cursor(position)) == position
    assert encode_cursor(None) is None
    assert decode_cursor(None) is None


@pytest.mark.parametrize("bad", ["", "5", "1.2.3", "-1.5", "a.b", "1.5 ", f"1.{2**63}", "1;5"])
def test_anything_but_a_returned_cursor_is_refused(bad: str) -> None:
    with pytest.raises(ValueError, match=r"cursor|ids"):
        decode_cursor(bad)


@pytest.mark.parametrize("bad", ["0001-01-01", "9999-12-31", "1999-12-31", "2101-01-01"])
def test_dates_outside_the_supported_range_are_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="between"):
        parse_local_date(bad)


def test_the_supported_range_converts_without_overflow() -> None:
    start, end = day_range(
        parse_local_date("2000-01-01"), parse_local_date("2100-12-31"), "Pacific/Kiritimati"
    )
    assert start is not None
    assert end is not None
    assert start < end


@pytest.mark.parametrize("bad", [0, -1, MAX_ID + 1])
def test_ids_must_be_positive_bigints(bad: int) -> None:
    with pytest.raises(ValueError, match="ids"):
        check_id(bad)
    assert check_id(None) is None
    assert check_id(MAX_ID) == MAX_ID
