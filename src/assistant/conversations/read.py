"""Read models over managed conversations for the agent's read tools (S03.1).

Account-scoped by construction: every function takes the account id and
every query filters on it, so an id from another account behaves exactly
like an unknown one (`LookupError` — §6 invariant 12; the tool executor
answers both with "not found").

The rows carry contact-authored text and contact-chosen names. They are for
the owner's own assistant only; the agent layer wraps them as untrusted
before they reach a model (§6 invariant 3b).

Coverage is part of every answer (§6 invariant 11): when the conversation's
coverage began, whether older content expired without a summary, how many
messages were deleted or expired, and unresolved gaps — so the model never
implies it saw more than was retained.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.conversations.models import Contact, Conversation, CoverageGap, Message
from assistant.memory.search import normalize

MAX_PAGE = 50
MAX_LIST = 50


@dataclass(frozen=True, slots=True)
class ConversationRow:
    conversation_id: int
    contact_id: int
    display_name: str | None
    username: str | None
    mode: str
    last_activity_at: datetime | None
    last_contact_msg_at: datetime | None
    last_owner_msg_at: datetime | None
    coverage_started_at: datetime | None
    retained_messages: int


@dataclass(frozen=True, slots=True)
class PageMessage:
    message_id: int
    direction: str
    sent_at: datetime
    edited: bool
    content_type: str
    text: str | None
    media_kind: str | None


@dataclass(frozen=True, slots=True)
class Coverage:
    """What this conversation's record does and does not contain."""

    coverage_started_at: datetime | None
    #: Content before this expired without a summary (§10.4).
    expired_before: datetime | None
    deleted_messages: int
    expired_messages: int
    unresolved_gaps: int
    #: Gaps recorded for the whole account (outages), not one chat.
    account_gaps: int


@dataclass(frozen=True, slots=True)
class ConversationPage:
    conversation: ConversationRow
    #: Oldest first.
    messages: tuple[PageMessage, ...]
    coverage: Coverage
    #: Pass as `before_message_id` for the next (older) page; None at the start.
    next_cursor: int | None


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    contact_id: int
    conversation_id: int | None
    display_name: str | None
    username: str | None
    last_activity_at: datetime | None


def _last_activity() -> ColumnElement[datetime]:
    return func.greatest(
        Conversation.last_contact_msg_at,
        Conversation.last_owner_msg_at,
        Conversation.last_assistant_msg_at,
    )


async def _retained_counts(
    session: AsyncSession, account_id: int, conversation_ids: Sequence[int]
) -> dict[int, int]:
    if not conversation_ids:
        return {}
    rows = await session.execute(
        select(Message.conversation_id, func.count())
        .where(Message.account_id == account_id)
        .where(Message.conversation_id.in_(conversation_ids))
        .where(Message.deleted_at.is_(None))
        .where(Message.purged_at.is_(None))
        .where((Message.text.is_not(None)) | (Message.media.is_not(None)))
        .group_by(Message.conversation_id)
    )
    return {int(conversation_id): int(count) for conversation_id, count in rows.all()}


def _row(conversation: Conversation, contact: Contact, retained: int) -> ConversationRow:
    activity = [
        moment
        for moment in (
            conversation.last_contact_msg_at,
            conversation.last_owner_msg_at,
            conversation.last_assistant_msg_at,
        )
        if moment is not None
    ]
    return ConversationRow(
        conversation_id=conversation.id,
        contact_id=contact.id,
        display_name=contact.display_name,
        username=contact.username,
        mode=conversation.mode,
        last_activity_at=max(activity) if activity else None,
        last_contact_msg_at=conversation.last_contact_msg_at,
        last_owner_msg_at=conversation.last_owner_msg_at,
        coverage_started_at=conversation.coverage_started_at,
        retained_messages=retained,
    )


async def list_conversations(
    session: AsyncSession,
    account_id: int,
    *,
    active_since: datetime | None = None,
    modes: Sequence[str] = (),
    contact_ids: Sequence[int] = (),
    awaiting_owner_reply: bool = False,
    limit: int = 20,
) -> list[ConversationRow]:
    """Conversations by most recent activity. Third-party bot chats are left out."""
    limit = max(1, min(limit, MAX_LIST))
    activity = _last_activity()
    statement = (
        select(Conversation, Contact)
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.account_id == account_id)
        .where(Contact.account_id == account_id)
        .where(Contact.is_bot.is_(False))
    )
    if active_since is not None:
        statement = statement.where(activity >= active_since)
    if modes:
        statement = statement.where(Conversation.mode.in_(modes))
    if contact_ids:
        statement = statement.where(Conversation.contact_id.in_(contact_ids))
    if awaiting_owner_reply:
        # The contact spoke last: the owner has not answered since.
        statement = statement.where(Conversation.last_contact_msg_at.is_not(None)).where(
            (Conversation.last_owner_msg_at.is_(None))
            | (Conversation.last_owner_msg_at < Conversation.last_contact_msg_at)
        )
    statement = statement.order_by(
        activity.desc().nulls_last(),
        Conversation.id.desc(),
    ).limit(limit)
    pairs = (await session.execute(statement)).all()
    counts = await _retained_counts(session, account_id, [c.id for c, _ in pairs])
    return [
        _row(conversation, contact, counts.get(conversation.id, 0))
        for conversation, contact in pairs
    ]


async def _coverage(session: AsyncSession, account_id: int, conversation: Conversation) -> Coverage:
    counted = await session.execute(
        select(
            func.count().filter(Message.deleted_at.is_not(None)),
            func.count().filter(Message.purged_at.is_not(None)),
        )
        .where(Message.account_id == account_id)
        .where(Message.conversation_id == conversation.id)
    )
    deleted, expired = counted.one()
    gaps = await session.execute(
        select(CoverageGap.conversation_id.is_(None), func.count())
        .where(CoverageGap.account_id == account_id)
        .where(
            (CoverageGap.conversation_id == conversation.id)
            | (CoverageGap.conversation_id.is_(None))
        )
        .where(CoverageGap.resolved_at.is_(None))
        .where(CoverageGap.certainty.in_(["possible", "definite"]))
        .group_by(CoverageGap.conversation_id.is_(None))
    )
    by_scope = {bool(account_wide): int(n) for account_wide, n in gaps.all()}
    return Coverage(
        coverage_started_at=conversation.coverage_started_at,
        expired_before=conversation.summary_gap_before,
        deleted_messages=int(deleted),
        expired_messages=int(expired),
        unresolved_gaps=by_scope.get(False, 0),
        account_gaps=by_scope.get(True, 0),
    )


async def conversation_page(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    *,
    before_message_id: int | None = None,
    limit: int = 30,
) -> ConversationPage:
    """One page of a conversation's retained messages, newest page first.

    Raises `LookupError` for an id that is unknown or another account's.
    """
    limit = max(1, min(limit, MAX_PAGE))
    pair = (
        await session.execute(
            select(Conversation, Contact)
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id == conversation_id)
        )
    ).first()
    if pair is None:
        raise LookupError("conversation not found in this account")
    conversation, contact = pair
    statement = (
        select(Message)
        .where(Message.account_id == account_id)
        .where(Message.conversation_id == conversation.id)
        .where(Message.deleted_at.is_(None))
        .where(Message.purged_at.is_(None))
        .where(Message.direction.is_not(None))
        .where(Message.sent_at.is_not(None))
        .where((Message.text.is_not(None)) | (Message.media.is_not(None)))
    )
    if before_message_id is not None:
        statement = statement.where(Message.id < before_message_id)
    rows = list(
        (
            await session.execute(
                statement.order_by(Message.sent_at.desc(), Message.id.desc()).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    messages = tuple(
        PageMessage(
            message_id=row.id,
            direction=str(row.direction),
            sent_at=row.sent_at,  # type: ignore[arg-type]  # filtered NOT NULL above
            edited=row.edited_at is not None,
            content_type=row.content_type,
            text=row.text,
            media_kind=(row.media or {}).get("kind") if row.media else None,
        )
        for row in reversed(rows)
    )
    counts = await _retained_counts(session, account_id, [conversation.id])
    return ConversationPage(
        conversation=_row(conversation, contact, counts.get(conversation.id, 0)),
        messages=messages,
        coverage=await _coverage(session, account_id, conversation),
        next_cursor=rows[-1].id if more and rows else None,
    )


async def contact_directory(session: AsyncSession, account_id: int) -> list[DirectoryEntry]:
    """Every non-bot contact of the account with a name, and its conversation."""
    rows = await session.execute(
        select(Contact, Conversation)
        .outerjoin(
            Conversation,
            (Conversation.contact_id == Contact.id) & (Conversation.account_id == account_id),
        )
        .where(Contact.account_id == account_id)
        .where(Contact.is_bot.is_(False))
        .where((Contact.display_name.is_not(None)) | (Contact.username.is_not(None)))
        .order_by(Contact.id)
    )
    entries: list[DirectoryEntry] = []
    for contact, conversation in rows.all():
        activity = None
        if conversation is not None:
            moments = [
                m
                for m in (
                    conversation.last_contact_msg_at,
                    conversation.last_owner_msg_at,
                    conversation.last_assistant_msg_at,
                )
                if m is not None
            ]
            activity = max(moments) if moments else None
        entries.append(
            DirectoryEntry(
                contact_id=contact.id,
                conversation_id=conversation.id if conversation is not None else None,
                display_name=contact.display_name,
                username=contact.username,
                last_activity_at=activity,
            )
        )
    return entries


# ------------------------------------------------------------ name matching
@dataclass(frozen=True, slots=True)
class ContactMatch:
    entry: DirectoryEntry
    #: `exact` > `prefix` > `token_prefix` > `fuzzy`.
    kind: str
    score: float


_KIND_RANK = {"exact": 0, "prefix": 1, "token_prefix": 2, "fuzzy": 3}
FUZZY_THRESHOLD = 0.75


def _keys(entry: DirectoryEntry) -> list[str]:
    keys = []
    if entry.display_name:
        keys.append(normalize(entry.display_name))
    if entry.username:
        keys.append(normalize(entry.username.lstrip("@")))
    return [key for key in keys if key]


def match_contacts(
    directory: Sequence[DirectoryEntry], query: str, *, limit: int = 5
) -> list[ContactMatch]:
    """Rank contacts for a name the owner typed, in any script.

    Names are compared in the search normal form, so "Камола", "Kamola" and
    "kamola" are one name. Exact beats prefix ("kam") beats a prefix of any
    word of the name ("Aliyeva" in "Kamola Aliyeva") beats difflib similarity
    of at least 0.75 (typos).
    """
    wanted = normalize(query.lstrip("@"))
    if not wanted:
        return []
    found: list[ContactMatch] = []
    for entry in directory:
        best: ContactMatch | None = None
        for key in _keys(entry):
            tokens = key.split()
            if key == wanted:
                candidate = ContactMatch(entry, "exact", 1.0)
            elif key.startswith(wanted):
                candidate = ContactMatch(entry, "prefix", len(wanted) / len(key))
            elif any(token.startswith(wanted) for token in tokens) or (
                " " in wanted and all(any(t.startswith(w) for t in tokens) for w in wanted.split())
            ):
                candidate = ContactMatch(entry, "token_prefix", len(wanted) / len(key))
            else:
                ratio = max(
                    [difflib.SequenceMatcher(None, wanted, key).ratio()]
                    + [difflib.SequenceMatcher(None, wanted, token).ratio() for token in tokens]
                )
                if ratio < FUZZY_THRESHOLD:
                    continue
                candidate = ContactMatch(entry, "fuzzy", ratio)
            if best is None or (_KIND_RANK[candidate.kind], -candidate.score) < (
                _KIND_RANK[best.kind],
                -best.score,
            ):
                best = candidate
        if best is not None:
            found.append(best)
    found.sort(
        key=lambda m: (
            _KIND_RANK[m.kind],
            -m.score,
            -(m.entry.last_activity_at.timestamp() if m.entry.last_activity_at else 0.0),
            m.entry.contact_id,
        )
    )
    return found[: max(1, limit)]
