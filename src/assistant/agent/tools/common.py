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
from collections.abc import Sequence
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
