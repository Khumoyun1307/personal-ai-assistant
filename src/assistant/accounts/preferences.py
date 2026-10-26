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
from functools import cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

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
#: Owner-facing names of the fields.
FIELD_LABELS = {
    FIELD_LANGUAGE: "language",
    FIELD_TIMEZONE: "timezone",
    FIELD_DEFAULT_MODE: "mode for new chats",
}

_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,4})?$")
MAX_TIMEZONE_LENGTH = 64


class PreferenceError(ValueError):
    """A value that cannot be applied. The message is safe to show the owner."""


def check_language(value: str) -> str:
    """A language code such as `uz`, `ru`, `en`, `uz-Latn` (at most 8 chars),
    in its usual case: `EN` → `en`, `uz-latn` → `uz-Latn`, `ru-ru` → `ru-RU`."""
    stripped = value.strip()
    if len(stripped) > 8 or not _LANGUAGE.fullmatch(stripped):
        raise PreferenceError("that does not look like a language code (e.g. uz, ru, en)")
    primary, _, subtag = stripped.partition("-")
    if not subtag:
        return primary.lower()
    if len(subtag) == 4 and subtag.isalpha():
        return f"{primary.lower()}-{subtag.title()}"  # a script
    return f"{primary.lower()}-{subtag.upper()}"  # a region


@cache
def _iana_timezones() -> frozenset[str]:
    """IANA zone names, without the aliases the zoneinfo directory also holds
    (`localtime`, `Factory`, `posixrules`, the `posix/` and `right/` trees)."""
    return frozenset(
        name
        for name in available_timezones()
        if name not in {"localtime", "Factory", "posixrules"}
        and not name.startswith(("posix/", "right/"))
    )


def check_timezone(value: str) -> str:
    stripped = value.strip()
    if not stripped or len(stripped) > MAX_TIMEZONE_LENGTH or stripped not in _iana_timezones():
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
    """Validate, lock the account and apply. Raises `PreferenceError` before
    changing anything."""
    wanted: dict[str, Any] = {}
    if language is not None:
        wanted[FIELD_LANGUAGE] = check_language(language)
    if timezone is not None:
        wanted[FIELD_TIMEZONE] = check_timezone(timezone)
    if new_default_mode is not None:
        wanted[FIELD_DEFAULT_MODE] = check_default_mode(new_default_mode)
    await lock(session, account)
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


async def lock(session: AsyncSession, account: Account) -> None:
    """Re-read the account under FOR NO KEY UPDATE before a read-modify-write
    of its settings, so a concurrent change is seen, never overwritten. Not
    FOR UPDATE: ingestion's foreign-key KEY SHARE on the account must not
    wait for a settings change (review item 6)."""
    await session.refresh(account, with_for_update={"key_share": True})


@dataclass(frozen=True, slots=True)
class Reverted:
    #: Fields set back to their earlier value.
    restored: tuple[str, ...]
    #: Fields changed again since: left as they are.
    kept: tuple[str, ...]


async def revert(session: AsyncSession, account: Account, details: Mapping[str, Any]) -> Reverted:
    """Undo one recorded change, under the account lock: each field still
    holding the value the change set goes back to what it was; a field
    changed again since stays."""
    await lock(session, account)
    restored: list[str] = []
    kept: list[str] = []
    changes = details.get("changes")
    if not isinstance(changes, dict):
        return Reverted((), ())
    now = current(account)
    for name, pair in changes.items():
        if name not in now or not isinstance(pair, list) or len(pair) != 2:
            continue
        old, new = pair
        if now[name] == new:
            _apply(account, name, old)
            restored.append(name)
        else:
            kept.append(name)
    return Reverted(tuple(restored), tuple(kept))
