"""Shared pieces of the conversation read tools (S03.1).

- **Argument validation** that the JSON schema does not express. Constraints
  are checked in Pydantic validators rather than as schema keywords, so the
  strict tool schema stays within what OpenAI strict mode accepts; dates are
  `YYYY-MM-DD` strings in the owner's timezone, validated here. Our rules
  raise `ArgumentRuleError`, whose fixed text the model is shown (§8.3).
- **Account checks**: an id that is not this account's raises
  `NotInAccountError`, which the executor answers "not found".
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

from assistant.agent import owner_texts
from assistant.agent.attribution import (
    ATTRIBUTION_RULE,
    check_attribution,
    effective_status,
    render_who,
)
from assistant.agent.locales import SupportedLocale
from assistant.agent.schemas import cited_ids
from assistant.agent.untrusted import Source, wrap
from assistant.conversations import coverage
from assistant.conversations.models import Contact, Conversation
from assistant.core.errors import NotInAccountError
from assistant.memory import catch_up, summaries

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


class ArgumentRuleError(ValueError):
    """One of our argument rules, in our own fixed words: the executor shows
    the text to the model so it can correct the call (§8.3). Never put the
    input in the text; any other `ValueError` is reported by field only."""


#: The chat tools that take one chat either way (get_conversation, the mode
#: and deletion tools).
ONE_TARGET_RULE = "give exactly one of conversation_id or contact_id; set the other to null"


def parse_local_moment(value: str | None) -> datetime | None:
    """`YYYY-MM-DD HH:MM` (or `YYYY-MM-DD` = midnight) as a naive local time,
    within `MIN_DATE`..`MAX_DATE`, or None. Raises ValueError otherwise."""
    if value is None:
        return None
    match = _MOMENT.fullmatch(value)
    if match is None:
        raise ArgumentRuleError("times must be 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'")
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
        raise ArgumentRuleError("dates must be YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if not MIN_DATE <= parsed <= MAX_DATE:
        raise ArgumentRuleError(f"dates must be between {MIN_DATE} and {MAX_DATE}")
    return parsed


def check_id(value: int | None) -> int | None:
    """An id argument: None, or 1..2^63-1 (0 is not "no filter")."""
    if value is not None and not 1 <= value <= MAX_ID:
        raise ArgumentRuleError("ids are positive 64-bit integers")
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
        raise ArgumentRuleError("cursor must be a next_cursor value returned earlier")
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


def summary_view(
    summary: Mapping[str, Any],
    *,
    contact_name: str | None,
    locale: SupportedLocale,
    authors: Mapping[int, str],
) -> dict[str, Any]:
    """A stored summary as the model sees it: every string is model output
    derived from contact text, so each is wrapped as untrusted (§6 inv. 3b);
    message ids let it check a point with `get_conversation`.

    S03.7: the summary is attribution-checked again here (`authors`: message
    id → direction of what it cites; v1-v4 rows included), and each question,
    decision and commitment gets a `who` line — fixed words for who asked,
    promised or agreed, in the owner's `locale`, rendered from the structured
    fields and the contact's name, never from model text. It holds a
    contact-chosen name, so it is wrapped as untrusted too.

    P9 (owner, 2026-10-27): `needs_reply` stays on while a question,
    decision or request to the owner is open in the checked summary
    (`catch_up.needs_reply`); the model's "no reply needed" reason is then
    replaced by our own words (`owner_texts.STILL_OPEN`)."""
    checked, _ = check_attribution(summary, authors)
    model_verdict = bool(checked.get("needs_reply"))
    still_open = not model_verdict and catch_up.needs_reply(checked)

    def who(key: str, item: Mapping[str, Any]) -> str | None:
        return wrap(render_who(key, item, contact_name, locale), Source.CONTACT_NAME)

    def points(key: str, *, attributed: bool) -> list[dict[str, Any]]:
        items = checked.get(key)
        return [
            {
                **({"who": who(key, item)} if attributed else {}),
                "text": _summary_text(item.get("text")),
                "message_ids": _ids(item.get("message_ids")),
            }
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, Mapping)
        ]

    commitments = checked.get("commitments")
    return {
        "overview": _summary_text(checked.get("overview")),
        "open_questions_to_owner": points("open_questions_to_owner", attributed=True),
        "decisions_requested": points("decisions_requested", attributed=True),
        "commitments": [
            {
                "who": who("commitments", item),
                "by": item.get("by"),
                "status": effective_status(item),
                "text": _summary_text(item.get("text")),
                "due": _summary_text(item.get("due")),
                "message_ids": _ids(item.get("message_ids")),
            }
            for item in (commitments if isinstance(commitments, list) else [])
            if isinstance(item, Mapping)
        ],
        "key_facts": points("key_facts", attributed=False),
        "needs_reply": model_verdict or still_open,
        "needs_reply_reason": (
            owner_texts.STILL_OPEN[locale]
            if still_open
            else _summary_text(checked.get("needs_reply_reason"))
        ),
        "attribution_rule": ATTRIBUTION_RULE,
        **_omitted(checked.get("items_omitted")),
    }


async def summary_authors(
    session: AsyncSession, account_id: int, conversation_id: int, summary: Mapping[str, Any]
) -> dict[int, str]:
    """Who wrote each message a stored summary cites (for `summary_view`)."""
    return await summaries.message_directions(
        session, account_id, conversation_id, cited_ids(dict(summary))
    )


async def summaries_authors(
    session: AsyncSession, account_id: int, by_conversation: Mapping[int, Mapping[str, Any]]
) -> dict[int, dict[int, str]]:
    """`summary_authors` for several chats' summaries, in one query."""
    return await summaries.message_directions_by_conversation(
        session,
        account_id,
        {
            conversation_id: cited_ids(dict(summary))
            for conversation_id, summary in by_conversation.items()
        },
    )


def _omitted(value: object) -> dict[str, Any]:
    """The list cap's count, said out loud when it cut anything."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return {
            "items_omitted": value,
            "note": f"{value} more item(s) were left out of these lists (each list is capped).",
        }
    return {}


async def require_contacts(session: AsyncSession, account_id: int, ids: Sequence[int]) -> None:
    """Every id must be a contact of this account (§8.3 step 2); else NotInAccountError."""
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
        raise NotInAccountError("contact not found in this account")


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
        raise NotInAccountError("conversation not found in this account")


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
