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

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def parse_local_date(value: str | None) -> date | None:
    """A `YYYY-MM-DD` string, or None. Raises ValueError otherwise."""
    if value is None:
        return None
    if len(value) != 10:
        raise ValueError("dates must be YYYY-MM-DD")
    return date.fromisoformat(value)


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
