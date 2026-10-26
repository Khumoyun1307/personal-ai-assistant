"""Owner-configurable retention (PRD R-07, FR-PRV-03; ARCHITECTURE §10.4; S03.5).

Pure: no I/O. Which data classes the owner may change, their defaults and
the allowed ranges, and how a stored override is read. Overrides live in
`accounts.settings["retention_days"]` (`{"messages": 30, ...}`); the hourly
sweep reads them on every run, so a change takes effect at the next sweep.

Ranges only **shorten** (M03 D80): the privacy policy (v4) states the
defaults as the longest periods, so a longer period would need a policy
update first — the owner's call, not a setting. Classes not listed here
(summaries and notes until deleted, the activity log and run accounting one
year, metadata rows) keep their fixed rules.

An unset value reads as the default. A stored value that is not a whole
number inside its range — written by hand, or left over from a narrower
range — reads as the class's shortest period (fail safe: never longer than
the owner may have meant, never a crash in the sweep; M03 D85).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

SETTINGS_KEY = "retention_days"

MESSAGES = "messages"
CONTROL_CHAT = "control_chat"
RAW_UPDATES = "raw_updates"


@dataclass(frozen=True, slots=True)
class RetentionClass:
    key: str
    #: Owner-facing name (control chat).
    label: str
    default_days: int
    min_days: int
    max_days: int
    #: The values the /settings panel offers.
    choices: tuple[int, ...]

    def check(self, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{self.label}: a whole number of days")
        if not self.min_days <= value <= self.max_days:
            raise ValueError(f"{self.label}: between {self.min_days} and {self.max_days} days")
        return value


CLASSES: dict[str, RetentionClass] = {
    # Message text, captions, media facts, search keys (§10.4). The rollup
    # starts 10 days before the ceiling, so 14 days is the shortest period
    # that still leaves it a window.
    MESSAGES: RetentionClass(MESSAGES, "Messages", 90, 14, 90, (14, 30, 60, 90)),
    # The owner's conversation with the assistant (`control_turns`).
    CONTROL_CHAT: RetentionClass(CONTROL_CHAT, "This chat", 90, 14, 90, (14, 30, 60, 90)),
    # Raw Telegram updates (`inbound_updates.payload`).
    RAW_UPDATES: RetentionClass(RAW_UPDATES, "Raw updates", 7, 1, 7, (1, 3, 7)),
}


def days(settings: Mapping[str, Any] | None, key: str) -> int:
    """The retention of class `key` for an account with these settings."""
    cls = CLASSES[key]
    overrides = (settings or {}).get(SETTINGS_KEY)
    if overrides is None:
        return cls.default_days
    if not isinstance(overrides, Mapping):
        return cls.min_days  # set, but not readable: fail safe
    value: Any = overrides.get(key)
    if value is None:
        return cls.default_days
    try:
        return cls.check(value)
    except ValueError:
        # Set but invalid: the shortest period, never longer than meant
        # (safety review item 7, M03 D85).
        return cls.min_days


def period(settings: Mapping[str, Any] | None, key: str) -> timedelta:
    return timedelta(days=days(settings, key))


def unrouted_period(all_settings: Iterable[Mapping[str, Any] | None], key: str) -> timedelta:
    """The period of rows not routed to an account (the control chat's raw
    updates, unknown senders'): the shortest any account chose, never longer
    than the default (M03 D80). One rule for the sweep and for the
    dead-letter alert's purge date, so the alert never promises longer."""
    return timedelta(
        days=min([CLASSES[key].default_days, *(days(settings, key) for settings in all_settings)])
    )


def with_override(settings: Mapping[str, Any] | None, key: str, value: int) -> dict[str, Any]:
    """New settings with one override set (validated first)."""
    CLASSES[key].check(value)
    current = dict(settings or {})
    overrides = current.get(SETTINGS_KEY)
    merged = dict(overrides) if isinstance(overrides, Mapping) else {}
    merged[key] = value
    current[SETTINGS_KEY] = merged
    return current
