"""Tests for schedule maths: occurrences, DST handling, timezone policies, lateness."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from assistant.automation.schedule import (
    Lateness,
    ScheduleError,
    ScheduleSpec,
    daily_at,
    lateness,
    next_local_time,
    next_occurrence,
    rezone,
    to_utc,
    validate,
)


def test_to_utc_plain_time() -> None:
    res = to_utc(datetime(2026, 11, 2, 9, 0), "Europe/Berlin")  # noqa: DTZ001
    assert res == datetime(2026, 11, 2, 8, 0, tzinfo=UTC)


def test_to_utc_in_the_spring_gap_gives_the_first_valid_instant() -> None:
    res = to_utc(datetime(2026, 3, 29, 2, 30), "Europe/Berlin")  # noqa: DTZ001
    assert res == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)


def test_to_utc_in_the_autumn_fold_gives_the_first_occurrence() -> None:
    res = to_utc(datetime(2026, 10, 25, 2, 30), "Europe/Berlin")  # noqa: DTZ001
    assert res == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_berlin_daily_0230_across_spring_forward() -> None:
    spec = ScheduleSpec(
        timezone="Europe/Berlin",
        tz_policy="follow_owner",
        rrule="FREQ=DAILY",
        dtstart_local=datetime(2026, 3, 27, 2, 30),  # noqa: DTZ001
    )
    t1 = next_occurrence(spec, after=datetime(2026, 3, 28, 1, 0, tzinfo=UTC))
    assert t1 == datetime(2026, 3, 28, 1, 30, tzinfo=UTC)

    t2 = next_occurrence(spec, after=t1)
    assert t2 == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)

    t3 = next_occurrence(spec, after=t2)
    assert t3 == datetime(2026, 3, 30, 0, 30, tzinfo=UTC)


def test_berlin_daily_0230_runs_once_on_fall_back_day() -> None:
    spec = ScheduleSpec(
        timezone="Europe/Berlin",
        tz_policy="follow_owner",
        rrule="FREQ=DAILY",
        dtstart_local=datetime(2026, 10, 23, 2, 30),  # noqa: DTZ001
    )
    t1 = next_occurrence(spec, after=datetime(2026, 10, 24, 0, 30, tzinfo=UTC))
    assert t1 == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)

    t2 = next_occurrence(spec, after=t1)
    assert t2 == datetime(2026, 10, 26, 1, 30, tzinfo=UTC)


def test_berlin_daily_0900_keeps_local_time_across_both_changes() -> None:
    # Spring forward
    spec_spring = ScheduleSpec(
        timezone="Europe/Berlin",
        tz_policy="follow_owner",
        rrule="FREQ=DAILY",
        dtstart_local=datetime(2026, 3, 27, 9, 0),  # noqa: DTZ001
    )
    t1 = next_occurrence(spec_spring, after=datetime(2026, 3, 27, 8, 0, tzinfo=UTC))
    assert t1 == datetime(2026, 3, 28, 8, 0, tzinfo=UTC)

    t2 = next_occurrence(spec_spring, after=t1)
    assert t2 == datetime(2026, 3, 29, 7, 0, tzinfo=UTC)

    # Autumn fall back
    spec_autumn = ScheduleSpec(
        timezone="Europe/Berlin",
        tz_policy="follow_owner",
        rrule="FREQ=DAILY",
        dtstart_local=datetime(2026, 10, 23, 9, 0),  # noqa: DTZ001
    )
    t3 = next_occurrence(spec_autumn, after=datetime(2026, 10, 23, 7, 0, tzinfo=UTC))
    assert t3 == datetime(2026, 10, 24, 7, 0, tzinfo=UTC)

    t4 = next_occurrence(spec_autumn, after=t3)
    assert t4 == datetime(2026, 10, 25, 8, 0, tzinfo=UTC)


def test_tashkent_to_moscow_follow_owner_keeps_the_local_time() -> None:
    spec = daily_at("21:00", "Asia/Tashkent", starting=date(2026, 11, 1))
    t1 = next_occurrence(spec, after=datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
    assert t1 == datetime(2026, 11, 1, 16, 0, tzinfo=UTC)

    rezoned = rezone(spec, "Europe/Moscow")
    t2 = next_occurrence(rezoned, after=datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
    assert t2 == datetime(2026, 11, 1, 18, 0, tzinfo=UTC)


def test_tashkent_to_moscow_fixed_keeps_the_utc_instant() -> None:
    spec = ScheduleSpec(
        timezone="Asia/Tashkent",
        tz_policy="fixed",
        rrule="FREQ=DAILY",
        dtstart_local=datetime(2026, 11, 1, 21, 0),  # noqa: DTZ001
    )
    rezoned = rezone(spec, "Europe/Moscow")
    t = next_occurrence(rezoned, after=datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
    assert t == datetime(2026, 11, 1, 16, 0, tzinfo=UTC)


def test_a_relative_one_off_reminder_is_unchanged_by_a_timezone_change() -> None:
    for policy in ("follow_owner", "fixed"):
        spec = ScheduleSpec(
            timezone="Europe/Berlin",
            tz_policy=policy,  # type: ignore[arg-type]
            run_at=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
        )
        rezoned = rezone(spec, "Asia/Tashkent")
        assert rezoned == spec
        t = next_occurrence(rezoned, after=datetime(2026, 10, 31, 0, 0, tzinfo=UTC))
        assert t == datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


def test_a_one_off_in_the_past_has_no_next_occurrence() -> None:
    spec = ScheduleSpec(
        timezone="UTC",
        tz_policy="fixed",
        run_at=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
    )
    assert next_occurrence(spec, after=datetime(2026, 11, 1, 10, 0, tzinfo=UTC)) is None
    assert next_occurrence(spec, after=datetime(2026, 11, 1, 10, 1, tzinfo=UTC)) is None


def test_leap_day_rule_skips_to_the_next_leap_year() -> None:
    spec = ScheduleSpec(
        timezone="UTC",
        tz_policy="fixed",
        rrule="FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29",
        dtstart_local=datetime(2024, 2, 29, 9, 0),  # noqa: DTZ001
    )
    t = next_occurrence(spec, after=datetime(2026, 10, 26, 0, 0, tzinfo=UTC))
    assert t == datetime(2028, 2, 29, 9, 0, tzinfo=UTC)


def test_an_exhausted_count_gives_none() -> None:
    spec = ScheduleSpec(
        timezone="UTC",
        tz_policy="fixed",
        rrule="FREQ=DAILY;COUNT=2",
        dtstart_local=datetime(2026, 11, 1, 9, 0),  # noqa: DTZ001
    )
    t1 = next_occurrence(spec, after=datetime(2026, 11, 1, 9, 0, tzinfo=UTC))
    assert t1 == datetime(2026, 11, 2, 9, 0, tzinfo=UTC)

    t2 = next_occurrence(spec, after=datetime(2026, 11, 2, 9, 0, tzinfo=UTC))
    assert t2 is None


def test_occurrences_are_strictly_after_and_one_per_local_day() -> None:
    spec = ScheduleSpec(
        timezone="Europe/Berlin",
        tz_policy="follow_owner",
        rrule="FREQ=DAILY",
        dtstart_local=datetime(2026, 1, 1, 2, 30),  # noqa: DTZ001
    )
    current = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    berlin_tz = ZoneInfo("Europe/Berlin")
    seen_days: set[date] = set()

    for _ in range(400):
        nxt = next_occurrence(spec, after=current)
        assert nxt is not None
        assert nxt > current
        assert nxt.tzinfo == UTC

        local_dt = nxt.astimezone(berlin_tz)
        local_day = local_dt.date()
        assert local_day not in seen_days, f"Duplicate occurrence on local date {local_day}"
        seen_days.add(local_day)
        current = nxt

    assert sorted(seen_days) == [date(2026, 1, 1) + timedelta(days=i) for i in range(400)]


def test_an_invalid_rrule_is_rejected() -> None:
    spec = ScheduleSpec(
        timezone="UTC",
        tz_policy="fixed",
        rrule="NOT_A_VALID_RRULE",
        dtstart_local=datetime(2026, 1, 1, 9, 0),  # noqa: DTZ001
    )
    with pytest.raises(ScheduleError):
        validate(spec)


def test_minutely_and_secondly_rules_are_rejected() -> None:
    for freq in ("FREQ=SECONDLY", "FREQ=MINUTELY", "freq=secondly", "freq=minutely"):
        spec = ScheduleSpec(
            timezone="UTC",
            tz_policy="fixed",
            rrule=freq,
            dtstart_local=datetime(2026, 1, 1, 9, 0),  # noqa: DTZ001
        )
        with pytest.raises(ScheduleError):
            validate(spec)


def test_a_rule_with_dtstart_or_tzid_inside_is_rejected() -> None:
    for rule in (
        "FREQ=DAILY;DTSTART=20260101T000000Z",
        "FREQ=DAILY;TZID=Europe/Berlin",
    ):
        spec = ScheduleSpec(
            timezone="UTC",
            tz_policy="fixed",
            rrule=rule,
            dtstart_local=datetime(2026, 1, 1, 9, 0),  # noqa: DTZ001
        )
        with pytest.raises(ScheduleError):
            validate(spec)


def test_an_unknown_timezone_is_rejected() -> None:
    spec = ScheduleSpec(
        timezone="Mars/Olympus_Mons",
        tz_policy="fixed",
        rrule="FREQ=DAILY",
        dtstart_local=datetime(2026, 1, 1, 9, 0),  # noqa: DTZ001
    )
    with pytest.raises(ScheduleError):
        validate(spec)


def test_a_naive_run_at_is_rejected() -> None:
    spec = ScheduleSpec(
        timezone="UTC",
        tz_policy="fixed",
        run_at=datetime(2026, 1, 1, 9, 0),  # noqa: DTZ001 - naive
    )
    with pytest.raises(ScheduleError):
        validate(spec)


def test_rrule_and_run_at_together_are_rejected() -> None:
    spec = ScheduleSpec(
        timezone="UTC",
        tz_policy="fixed",
        rrule="FREQ=DAILY",
        dtstart_local=datetime(2026, 1, 1, 9, 0),  # noqa: DTZ001
        run_at=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
    )
    with pytest.raises(ScheduleError):
        validate(spec)


def test_neither_rrule_nor_run_at_is_rejected() -> None:
    spec = ScheduleSpec(
        timezone="UTC",
        tz_policy="fixed",
    )
    with pytest.raises(ScheduleError):
        validate(spec)


def test_a_bad_hhmm_is_rejected() -> None:
    for bad in ("24:00", "12:60", "9:00", "12:0", "abc", "21:00:00", ""):
        with pytest.raises(ScheduleError):
            daily_at(bad, "UTC", starting=date(2026, 1, 1))

        with pytest.raises(ScheduleError):
            next_local_time(bad, "UTC", after=datetime(2026, 1, 1, 0, 0, tzinfo=UTC))


def test_next_local_time_today_when_still_ahead() -> None:
    res = next_local_time(
        "21:00",
        "Asia/Tashkent",
        after=datetime(2026, 11, 1, 10, 0, tzinfo=UTC),
    )
    assert res == datetime(2026, 11, 1, 16, 0, tzinfo=UTC)


def test_next_local_time_tomorrow_when_passed() -> None:
    res = next_local_time(
        "21:00",
        "Asia/Tashkent",
        after=datetime(2026, 11, 1, 17, 0, tzinfo=UTC),
    )
    assert res == datetime(2026, 11, 2, 16, 0, tzinfo=UTC)


def test_next_local_time_on_the_spring_dst_day() -> None:
    res = next_local_time(
        "21:00",
        "Europe/Berlin",
        after=datetime(2026, 3, 29, 12, 0, tzinfo=UTC),
    )
    assert res == datetime(2026, 3, 29, 19, 0, tzinfo=UTC)


def test_lateness_boundaries() -> None:
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    grace = timedelta(minutes=2)
    max_late = timedelta(hours=6)

    # 0 s -> ON_TIME
    assert lateness(now, now, grace=grace, max_lateness=max_late) == Lateness.ON_TIME
    # -30 s (early) -> ON_TIME
    assert (
        lateness(now + timedelta(seconds=30), now, grace=grace, max_lateness=max_late)
        == Lateness.ON_TIME
    )
    # 2 min -> ON_TIME
    assert (
        lateness(now - timedelta(minutes=2), now, grace=grace, max_lateness=max_late)
        == Lateness.ON_TIME
    )
    # 2 min 1 s -> LATE
    assert (
        lateness(
            now - timedelta(minutes=2, seconds=1),
            now,
            grace=grace,
            max_lateness=max_late,
        )
        == Lateness.LATE
    )
    # 6 h -> LATE
    assert (
        lateness(now - timedelta(hours=6), now, grace=grace, max_lateness=max_late) == Lateness.LATE
    )
    # 6 h 1 s -> SKIP
    assert (
        lateness(
            now - timedelta(hours=6, seconds=1),
            now,
            grace=grace,
            max_lateness=max_late,
        )
        == Lateness.SKIP
    )


def test_timezone_keyword_argument_compatibility() -> None:
    res = to_utc(datetime(2026, 11, 2, 9, 0), timezone="Europe/Berlin")  # noqa: DTZ001
    assert res == datetime(2026, 11, 2, 8, 0, tzinfo=UTC)

    spec = daily_at("09:00", timezone="Europe/Berlin", starting=date(2026, 11, 1))
    assert spec.timezone == "Europe/Berlin"

    nxt = next_local_time(
        "09:00",
        timezone="Europe/Berlin",
        after=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
    )
    assert nxt == datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
