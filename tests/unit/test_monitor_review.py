"""The pure parts of a monitor's review coverage (C7, I4): range merging and
the decision whether a monitor may be read now. The database behaviour is in
`tests/integration/test_monitors.py`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from assistant.automation.monitor_review import (
    MAX_PENDING_RANGES,
    ReviewRange,
    eligibility,
    merge_ranges,
    position_of,
)
from assistant.inbox.models import ReviewProgress

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def span(first: int, last: int, reason: str = "invalid_output") -> ReviewRange:
    return ReviewRange.model_validate(
        {"first_message_id": first, "last_message_id": last, "reason": reason}
    )


def row(**values: object) -> ReviewProgress:
    values.setdefault("status", "pending")
    values.setdefault("pending_ranges", [])
    return ReviewProgress(reader="monitor", scope_key="rolling", **values)


def test_adjacent_and_overlapping_ranges_of_one_reason_merge_others_do_not() -> None:
    merged = merge_ranges([span(5, 7), span(8, 9), span(20, 21), span(6, 6, "deleted")])
    assert merged == [span(5, 9), span(6, 6, "deleted"), span(20, 21)]


def test_overflowing_ranges_widen_and_never_lose_a_message() -> None:
    many = [span(10 * i, 10 * i, "deleted") for i in range(1, MAX_PENDING_RANGES + 5)]
    [wide] = merge_ranges(many)
    assert (wide.first_message_id, wide.last_message_id) == (10, 10 * (MAX_PENDING_RANGES + 4))


def test_a_corrupt_position_is_no_position() -> None:
    assert position_of(None) == 0
    assert position_of(row(position={"message_id": "x"})) == 0
    assert position_of(row(position={"message_id": 42, "content_revision": 3})) == 42


def test_a_monitor_without_a_row_or_past_its_backoff_may_be_read() -> None:
    assert eligibility(None, first_pending=3, now=NOW).ready
    assert eligibility(row(retry_at=NOW - timedelta(seconds=1)), first_pending=3, now=NOW).ready


def test_a_monitor_in_backoff_waits_until_it_ends() -> None:
    until = NOW + timedelta(seconds=60)
    verdict = eligibility(row(retry_at=until), first_pending=3, now=NOW)
    assert (verdict.ready, verdict.retry_at, verdict.degraded) == (False, until, False)


def test_a_degraded_batch_is_not_read_again_until_it_changes() -> None:
    failed = row(status="degraded", pending_ranges=[span(3, 9).model_dump()])
    assert eligibility(failed, first_pending=3, now=NOW).degraded
    # Its first message was deleted: this is different work.
    assert eligibility(failed, first_pending=4, now=NOW).ready
