"""structlog processors that keep content and secrets out of the logs.

AGENTS §9 / ARCHITECTURE §14.4: logs never contain message text, captions,
contact names or usernames, phone numbers, tokens, API keys or Bot API URLs
(the URL embeds the token). Log identifiers are internal ids.

Two independent defences, because either alone is incomplete:

1. `drop_sensitive_keys` removes event-dict entries whose *key* names content or
   a secret. This catches structured values of any type.
2. `scrub_sensitive_values` rewrites *string* values that match a secret shape,
   catching secrets that arrive inside an otherwise innocuous field.

Exceptions are never rendered: `redact_exception` replaces the traceback with
typed fields, because an exception's string form routinely quotes the offending
row, parameter or request body.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, MutableMapping, Sequence
from types import TracebackType
from typing import Any, Final

REDACTED: Final = "[redacted]"

#: Event-dict keys dropped outright. Compared case-insensitively.
SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        # Message content
        "text",
        "message_text",
        "caption",
        "content",
        "body",
        "draft",
        "draft_text",
        "summary",
        "query",
        "prompt",
        # Identities
        "first_name",
        "last_name",
        "full_name",
        "name",
        "contact_name",
        "display_name",
        "username",
        "usernames",
        "title",
        "chat_title",
        "bio",
        "about",
        "phone",
        "phone_number",
        # Secrets
        "token",
        "bot_token",
        "api_key",
        "openai_api_key",
        "authorization",
        "password",
        "secret",
        "database_url",
        "dsn",
    }
)

#: Value patterns replaced wherever they appear in a string.
#: The phone pattern deliberately requires a leading `+`: a bare run of digits is
#: indistinguishable from an ISO date or an internal id, and over-redacting those
#: would make the logs useless.
_VALUE_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}"),  # Telegram bot token
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),  # OpenAI API key (sk-… / sk-proj-…)
    re.compile(r"https?://\S*?/bot\S*"),  # Bot API URL (embeds the token)
    re.compile(r"postgres(?:ql)?(?:\+\w+)?://\S+"),  # DB URL (embeds the password)
    re.compile(r"(?<![\w-])\+\d[\d\s().-]{7,}\d(?![\w-])"),  # international phone number
)

_MAX_DEPTH: Final = 6


def scrub_text(value: str) -> str:
    """Replace every known secret shape in `value`."""
    for pattern in _VALUE_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def _scrub(value: Any, depth: int = 0) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if depth >= _MAX_DEPTH:
        return value
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_sensitive(key) else _scrub(item, depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item, depth + 1) for item in value)
    if isinstance(value, (set, frozenset)):
        return type(value)(_scrub(item, depth + 1) for item in value)
    return value


def _is_sensitive(key: object) -> bool:
    return isinstance(key, str) and key.lower() in SENSITIVE_KEYS


def drop_sensitive_keys(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace values of keys that name content or a secret."""
    for key in list(event_dict):
        if _is_sensitive(key):
            event_dict[key] = REDACTED
    return event_dict


def scrub_sensitive_values(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Rewrite string values (at any nesting depth) that match a secret shape."""
    for key, value in event_dict.items():
        if value is not REDACTED:
            event_dict[key] = _scrub(value)
    return event_dict


def _resolve_exception(exc_info: object) -> BaseException | None:
    if isinstance(exc_info, BaseException):
        return exc_info
    if isinstance(exc_info, Sequence) and len(exc_info) == 3:  # (type, value, traceback)
        candidate = exc_info[1]
        if isinstance(candidate, BaseException):
            return candidate
    if exc_info is True:
        import sys

        return sys.exc_info()[1]
    return None


def redact_exception(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Replace exception info with typed fields; never `str(exc)` (§14.4)."""
    exc_info = event_dict.pop("exc_info", None)
    event_dict.pop("exception", None)
    if exc_info is None or exc_info is False:
        return event_dict

    exc = _resolve_exception(exc_info)
    if exc is None:
        return event_dict

    event_dict["exc_type"] = type(exc).__qualname__
    event_dict["exc_module"] = type(exc).__module__

    # Typed diagnostics, where the SDK or driver exposes them.
    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if isinstance(sqlstate, str):
        event_dict["sqlstate"] = sqlstate
    diag = getattr(exc, "diag", None)
    constraint = getattr(diag, "constraint_name", None) if diag is not None else None
    if isinstance(constraint, str):
        event_dict["constraint"] = constraint
    for attr, field in (("status_code", "status_code"), ("error_code", "error_code")):
        code = getattr(exc, attr, None)
        if isinstance(code, (int, str)):
            event_dict[field] = code

    # Where the traceback was raised, without the exception's message.
    tb: TracebackType | None = exc.__traceback__
    if tb is not None:
        while tb.tb_next is not None:
            tb = tb.tb_next
        event_dict["exc_origin"] = f"{tb.tb_frame.f_code.co_qualname}:{tb.tb_lineno}"
    return event_dict
