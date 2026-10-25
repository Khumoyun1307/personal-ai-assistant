"""Shared pieces of the conversation read tools (S03.1).

- **Argument validation** that the JSON schema does not express. Constraints
  are checked in Pydantic validators rather than as schema keywords, so the
  strict tool schema stays within what OpenAI strict mode accepts; dates are
  `YYYY-MM-DD` strings in the owner's timezone, validated here.
- **Untrusted rendering**: every contact-authored string and contact-chosen
  name goes through `agent/untrusted.py` (§6 invariant 3b), including the
  owner's own messages in business chats.
- **Coverage notes** (§6 invariant 11): every result says what the record
  cannot contain.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.agent.untrusted import Source, wrap
from assistant.conversations import coverage
from assistant.conversations.models import Contact, Conversation

DIRECTION_LABELS = {
    "contact": "contact",
    "owner_manual": "owner",
    "owner_automated": "owner (automated)",
    "assistant_sent": "assistant",
}

COVERAGE_RULE = (
    "Only messages received since coverage began, in chats included under Chat "
    "Automation, are stored; deleted and expired messages are gone, and there is "
    "no access to older history. Never imply you saw anything beyond these results."
)


#: Dates a question can sensibly name; outside them the UTC conversion could
#: overflow (`9999-12-31` + 1 day).
MIN_DATE = date(2000, 1, 1)
MAX_DATE = date(2100, 12, 31)
#: Ids are Postgres `bigint`: anything outside would make the query fail.
MAX_ID = 2**63 - 1

_CURSOR = re.compile(r"^(\d{1,17})\.(\d{1,19})$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


_MOMENT = re.compile(r"^(\d{4}-\d{2}-\d{2})(?: (\d{2}):(\d{2}))?$")


def parse_local_moment(value: str | None) -> datetime | None:
    """`YYYY-MM-DD HH:MM` (or `YYYY-MM-DD` = midnight) as a naive local time,
    within `MIN_DATE`..`MAX_DATE`, or None. Raises ValueError otherwise."""
    if value is None:
        return None
    match = _MOMENT.fullmatch(value)
    if match is None:
        raise ValueError("times must be 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'")
    day = parse_local_date(match.group(1))
    assert day is not None
    hour, minute = int(match.group(2) or 0), int(match.group(3) or 0)
    return datetime.combine(day, time(hour, minute))


def local_to_utc(moment: datetime, timezone: str) -> datetime:
    """A naive owner-local time as UTC (DST gaps resolve forward, folds to
    the first occurrence — zoneinfo's defaults)."""
    return moment.replace(tzinfo=zone(timezone)).astimezone(UTC)


def parse_local_date(value: str | None) -> date | None:
    """A `YYYY-MM-DD` string within `MIN_DATE`..`MAX_DATE`, or None.
    Raises ValueError otherwise."""
    if value is None:
        return None
    if len(value) != 10:
        raise ValueError("dates must be YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if not MIN_DATE <= parsed <= MAX_DATE:
        raise ValueError(f"dates must be between {MIN_DATE} and {MAX_DATE}")
    return parsed


def check_id(value: int | None) -> int | None:
    """An id argument: None, or 1..2^63-1 (0 is not "no filter")."""
    if value is not None and not 1 <= value <= MAX_ID:
        raise ValueError("ids are positive 64-bit integers")
    return value


#: An optional id argument, validated by `check_id`.
OptionalId = Annotated[int | None, AfterValidator(check_id)]


def one(value: int | None) -> tuple[int, ...]:
    """An optional id as a filter tuple (`()` = no filter; 0 is refused earlier)."""
    return () if value is None else (value,)


def encode_cursor(position: tuple[datetime, int] | None) -> str | None:
    """An opaque page cursor: the oldest shown message's `(sent_at, id)`."""
    if position is None:
        return None
    moment, message_id = position
    micros = (moment - _EPOCH) // timedelta(microseconds=1)
    return f"{micros}.{message_id}"


def decode_cursor(value: str | None) -> tuple[datetime, int] | None:
    """Strict inverse of `encode_cursor`; ValueError for anything else."""
    if value is None:
        return None
    match = _CURSOR.fullmatch(value)
    if match is None:
        raise ValueError("cursor must be a next_cursor value returned earlier")
    message_id = int(match.group(2))
    check_id(message_id)
    return _EPOCH + timedelta(microseconds=int(match.group(1))), message_id


def zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


def day_range(
    since: date | None, until: date | None, timezone: str
) -> tuple[datetime | None, datetime | None]:
    """Inclusive local dates → a UTC `[start, end)` range."""
    tz = zone(timezone)
    start = datetime.combine(since, time.min, tzinfo=tz).astimezone(UTC) if since else None
    end = (
        datetime.combine(until + timedelta(days=1), time.min, tzinfo=tz).astimezone(UTC)
        if until
        else None
    )
    return start, end


def local_time(moment: datetime | None, timezone: str) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(zone(timezone)).strftime("%Y-%m-%d %H:%M")


def name(value: str | None) -> str | None:
    return wrap(value, Source.CONTACT_NAME)


def message_text(value: str | None, direction: str, *, limit: int) -> str | None:
    if value is None:
        return None
    clipped = value if len(value) <= limit else value[:limit] + "…"
    source = Source.CONTACT if direction == "contact" else Source.OWNER_IN_BUSINESS_CHAT
    return wrap(clipped, source)


def _summary_text(value: object) -> str | None:
    return wrap(value, Source.SUMMARY) if isinstance(value, str) and value else None


def _ids(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [i for i in value if isinstance(i, int) and not isinstance(i, bool)]


def summary_view(summary: Mapping[str, Any]) -> dict[str, Any]:
    """A stored summary as the model sees it: every string is model output
    derived from contact text, so each is wrapped as untrusted (§6 inv. 3b);
    message ids let it check a point with `get_conversation`."""

    def points(key: str) -> list[dict[str, Any]]:
        items = summary.get(key)
        return [
            {"text": _summary_text(item.get("text")), "message_ids": _ids(item.get("message_ids"))}
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, dict)
        ]

    commitments = summary.get("commitments")
    return {
        "overview": _summary_text(summary.get("overview")),
        "open_questions_to_owner": points("open_questions_to_owner"),
        "decisions_requested": points("decisions_requested"),
        "commitments": [
            {
                "by": item.get("by") if item.get("by") in ("owner", "contact") else None,
                "text": _summary_text(item.get("text")),
                "due": _summary_text(item.get("due")),
                "message_ids": _ids(item.get("message_ids")),
            }
            for item in (commitments if isinstance(commitments, list) else [])
            if isinstance(item, dict)
        ],
        "key_facts": points("key_facts"),
        "needs_reply": bool(summary.get("needs_reply")),
        "needs_reply_reason": _summary_text(summary.get("needs_reply_reason")),
        **_omitted(summary.get("items_omitted")),
    }


def _omitted(value: object) -> dict[str, Any]:
    """The list cap's count, said out loud when it cut anything."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return {"items_omitted": value, "note": f"{value} older item(s) were left out (list cap)."}
    return {}


async def require_contacts(session: AsyncSession, account_id: int, ids: Sequence[int]) -> None:
    """Every id must be a contact of this account (§8.3 step 2); else LookupError."""
    if not ids:
        return
    found = (
        await session.execute(
            select(func.count())
            .select_from(Contact)
            .where(Contact.account_id == account_id)
            .where(Contact.id.in_(list(ids)))
        )
    ).scalar_one()
    if int(found) != len(set(ids)):
        raise LookupError("contact not found in this account")


async def require_conversations(session: AsyncSession, account_id: int, ids: Sequence[int]) -> None:
    if not ids:
        return
    found = (
        await session.execute(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id.in_(list(ids)))
        )
    ).scalar_one()
    if int(found) != len(set(ids)):
        raise LookupError("conversation not found in this account")


async def account_coverage(session: AsyncSession, account_id: int, timezone: str) -> dict[str, Any]:
    """Account-wide coverage facts for a search answer (content-free)."""
    summary = await coverage.summarize(session, account_id)
    earliest = (
        await session.execute(
            select(func.min(Conversation.coverage_started_at)).where(
                Conversation.account_id == account_id
            )
        )
    ).scalar_one_or_none()
    return {
        "rule": COVERAGE_RULE,
        "coverage_started_at": local_time(earliest, timezone),
        "caveat": summary.caveat,
    }
