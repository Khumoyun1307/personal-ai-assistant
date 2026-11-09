"""`update_preferences` alert settings (2026-11-09): the digest time (or
off), quiet hours, urgent bypass and the hourly cap, validated by the same
checks as the /settings panel; like every field of a strict schema they are
required, null meaning unchanged (D52)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from assistant.agent.tools.modes import UpdatePreferencesArgs
from assistant.agent.tools.registry import strict_schema

#: Every pre-2026-11-09 field, unchanged (null = leave as is; D52).
BASE: dict[str, Any] = dict.fromkeys(
    (
        "preferred_language",
        "timezone",
        "default_mode",
        "reply_language",
        "reply_tone",
        "reply_style",
        "message_retention_days",
        "control_chat_retention_days",
        "raw_update_retention_days",
    )
)
ALERT_FIELDS = ("digest_time", "quiet_hours", "urgent_bypass", "alert_cap_per_hour")
BASE |= dict.fromkeys(ALERT_FIELDS)


@pytest.mark.parametrize(
    "values",
    [
        {"digest_time": "off"},
        {"digest_time": "08:30"},
        {"quiet_hours": "off"},
        {"quiet_hours": "22:00-07:00"},
        {"urgent_bypass": False},
        {"alert_cap_per_hour": 30},
    ],
)
def test_valid_alert_settings_are_accepted(values: dict[str, Any]) -> None:
    parsed = UpdatePreferencesArgs.model_validate(BASE | values)
    for field, value in values.items():
        assert getattr(parsed, field) == value


@pytest.mark.parametrize(
    "values",
    [
        {"digest_time": "25:00"},
        {"digest_time": "9pm"},
        {"quiet_hours": "22:00"},
        {"alert_cap_per_hour": 0},
        {"alert_cap_per_hour": 31},
    ],
)
def test_invalid_alert_settings_are_refused(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        UpdatePreferencesArgs.model_validate(BASE | values)


def test_alert_fields_are_in_the_strict_schema_and_null_leaves_them() -> None:
    parsed = UpdatePreferencesArgs.model_validate(BASE | {"reply_tone": "formal"})
    assert all(getattr(parsed, field) is None for field in ALERT_FIELDS)
    schema = strict_schema(UpdatePreferencesArgs)
    assert set(ALERT_FIELDS) <= set(schema["required"])
    assert schema["additionalProperties"] is False


def test_an_alert_setting_alone_is_something_to_change() -> None:
    assert UpdatePreferencesArgs.model_validate(BASE | {"digest_time": "off"}).digest_time == "off"
    with pytest.raises(ValidationError):
        UpdatePreferencesArgs.model_validate(BASE)
