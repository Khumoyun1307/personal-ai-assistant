"""P2 preferences validate once and default safely when storage is invalid."""

import pytest

from assistant.accounts import preferences
from assistant.accounts.models import Account


def account(**settings: object) -> Account:
    return Account(telegram_user_id=1, timezone="UTC", settings=settings)


def test_quiet_hours_parse_and_default() -> None:
    assert preferences.current(account())["quiet_hours"] == "23:00-08:00"
    assert preferences.check_quiet_hours("off") == "off"
    assert preferences.check_quiet_hours("22:30-07:15") == "22:30-07:15"
    for value in ("24:00-08:00", "23:60-08:00", "9:00-08:00", "bad"):
        with pytest.raises(preferences.PreferenceError):
            preferences.check_quiet_hours(value)


def test_digest_time_off_and_default() -> None:
    assert preferences.current(account())["digest_time"] == "21:00"
    assert preferences.check_digest_time("off") == "off"
    assert preferences.check_digest_time("09:15") == "09:15"
    for value in ("24:00", "00:60", "9:00", "bad"):
        with pytest.raises(preferences.PreferenceError):
            preferences.check_digest_time(value)


def test_alert_cap_bounds() -> None:
    for value in (1, 6, 30):
        assert preferences.check_alert_cap(value) == value
    for value in (0, 31, True, "6", 1.5):
        with pytest.raises(preferences.PreferenceError):
            preferences.check_alert_cap(value)


def test_an_invalid_stored_value_reads_as_the_default() -> None:
    current = preferences.current(
        account(quiet_hours="bad", digest_time=3, alert_cap_per_hour=True, urgent_bypass="yes")
    )
    assert current["quiet_hours"] == "23:00-08:00"
    assert current["digest_time"] == "21:00"
    assert current["alert_cap_per_hour"] == 6
    assert current["urgent_bypass"] is False
