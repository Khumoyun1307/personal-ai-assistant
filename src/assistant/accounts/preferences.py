"""Owner preferences (FR-MEM-02, FR-CTL-10; S03.3).

The structured settings the owner can change by command, by the /settings
panel or through the `update_preferences` tool: preferred language, timezone
and the default mode of new chats. Every value is validated here, once, for
all three paths. A change writes one content-free audit row whose details
hold the old and new values (language codes, IANA names, mode names — no
content), so a one-tap Undo can restore exactly what it replaced.

Free-text style and tone ("short, polite replies to colleagues") are not
settings: they are owner instructions in `memory_items` (J11).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, AuditLogEntry
from assistant.core.clock import Clock

DEFAULT_MODE_KEY = "default_mode"
#: Modes a new chat may start in. `assist` arrives with drafts (M04).
SETTABLE_DEFAULT_MODES = ("observe", "ignore")
#: PRD R-03, Q-06.
FALLBACK_DEFAULT_MODE = "observe"

EVENT_PREFERENCES_CHANGED = "preferences_changed"

FIELD_LANGUAGE = "preferred_language"
FIELD_TIMEZONE = "timezone"
FIELD_DEFAULT_MODE = "default_mode"

_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,4})?$")
MAX_TIMEZONE_LENGTH = 64


class PreferenceError(ValueError):
    """A value that cannot be applied. The message is safe to show the owner."""


def check_language(value: str) -> str:
    """A language code such as `uz`, `ru`, `en`, `uz-Latn` (at most 8 chars)."""
    stripped = value.strip()
    if len(stripped) > 8 or not _LANGUAGE.fullmatch(stripped):
        raise PreferenceError("that does not look like a language code (e.g. uz, ru, en)")
    return stripped


def check_timezone(value: str) -> str:
    stripped = value.strip()
    if not stripped or len(stripped) > MAX_TIMEZONE_LENGTH:
        raise PreferenceError("unknown timezone")
    try:
        ZoneInfo(stripped)
    except ZoneInfoNotFoundError, ValueError, KeyError:
        raise PreferenceError("unknown timezone") from None
    return stripped


def check_default_mode(value: str) -> str:
    if value not in SETTABLE_DEFAULT_MODES:
        raise PreferenceError("new chats can start in observe or ignore mode")
    return value


def default_mode(account: Account) -> str:
    """The mode a new conversation starts in (PRD §11.1, R-03)."""
    value = (account.settings or {}).get(DEFAULT_MODE_KEY)
    return value if value in SETTABLE_DEFAULT_MODES else FALLBACK_DEFAULT_MODE


def current(account: Account) -> dict[str, Any]:
    return {
        FIELD_LANGUAGE: account.locale_pref,
        FIELD_TIMEZONE: account.timezone,
        FIELD_DEFAULT_MODE: default_mode(account),
    }


def _apply(account: Account, field: str, value: Any) -> None:
    if field == FIELD_LANGUAGE:
        account.locale_pref = value
    elif field == FIELD_TIMEZONE:
        account.timezone = value
    elif field == FIELD_DEFAULT_MODE:
        account.settings = {**(account.settings or {}), DEFAULT_MODE_KEY: value}
    else:  # pragma: no cover - fields come from this module
        raise PreferenceError(field)


@dataclass(frozen=True, slots=True)
class PreferenceChange:
    #: field → (before, after), only the fields that changed.
    changes: Mapping[str, tuple[Any, Any]]
    #: The audit row recording it (the Undo reference); None if nothing changed.
    audit_id: int | None


async def update(
    session: AsyncSession,
    account: Account,
    *,
    language: str | None = None,
    timezone: str | None = None,
    new_default_mode: str | None = None,
    actor: str,
    clock: Clock,
) -> PreferenceChange:
    """Validate and apply. Raises `PreferenceError` before changing anything."""
    wanted: dict[str, Any] = {}
    if language is not None:
        wanted[FIELD_LANGUAGE] = check_language(language)
    if timezone is not None:
        wanted[FIELD_TIMEZONE] = check_timezone(timezone)
    if new_default_mode is not None:
        wanted[FIELD_DEFAULT_MODE] = check_default_mode(new_default_mode)
    before = current(account)
    changes = {f: (before[f], v) for f, v in wanted.items() if before[f] != v}
    if not changes:
        return PreferenceChange({}, None)
    for field, (_, value) in changes.items():
        _apply(account, field, value)
    entry = AuditLogEntry(
        account_id=account.id,
        at=clock.now(),
        actor=actor,
        event=EVENT_PREFERENCES_CHANGED,
        subject_type="account",
        subject_id=account.id,
        details={"changes": {f: [old, new] for f, (old, new) in changes.items()}},
    )
    session.add(entry)
    await session.flush()
    return PreferenceChange(changes, entry.id)


def revert(account: Account, details: Mapping[str, Any]) -> list[str]:
    """Undo one recorded change: each field still holding the value the
    change set goes back to what it was. Returns the fields restored (none if
    every field was changed again since)."""
    restored: list[str] = []
    changes = details.get("changes")
    if not isinstance(changes, dict):
        return restored
    now = current(account)
    for field, pair in changes.items():
        if field not in now or not isinstance(pair, list) or len(pair) != 2:
            continue
        old, new = pair
        if now[field] == new:
            _apply(account, field, old)
            restored.append(field)
    return restored
