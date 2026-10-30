"""Read models over managed conversations for the agent's read tools (S03.1).

Account-scoped by construction: every function takes the account id and
every query filters on it, so an id from another account behaves exactly
like an unknown one (`NotInAccountError` — §6 invariant 12; the tool executor
answers both with "not found").

The rows carry contact-authored text and contact-chosen names. They are for
the owner's own assistant only; the agent layer wraps them as untrusted
before they reach a model (§6 invariant 3b).

Coverage is part of every answer (§6 invariant 11): when the conversation's
coverage began, whether older content expired without a summary, how many
messages were deleted or expired, how many arrived while the chat was in
Ignore and were not stored (M03 fix round C3), and unresolved gaps — so the
model never implies it saw more than was retained.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, and_, func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.conversations.models import Contact, Conversation, CoverageGap, Message
from assistant.core.errors import NotInAccountError
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
    #: Messages that arrived while the chat was in Ignore (`not_stored_while_ignored`).
    not_stored_while_ignored: int = 0


@dataclass(frozen=True, slots=True)
class ConversationPage:
    conversation: ConversationRow
    #: Oldest first.
    messages: tuple[PageMessage, ...]
    coverage: Coverage
    #: Pass as `before` for the next (older) page; None at the start. The
    #: position of the oldest message shown: `(sent_at, message id)`.
    next_cursor: tuple[datetime, int] | None


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    contact_id: int
    conversation_id: int | None
    display_name: str | None
    username: str | None
    last_activity_at: datetime | None


def saved_message() -> ColumnElement[bool]:
    """A message whose content is stored now: the exact complement of
    `not_stored_while_ignored` among the rows that are neither deleted nor
    expired (a deletion or the retention purge sets `deleted_at` /
    `purged_at`)."""
    return and_(
        Message.direction.is_not(None),
        or_(
            Message.content_hash.is_not(None),
            Message.text.is_not(None),
            Message.media.is_not(None),
        ),
        Message.deleted_at.is_(None),
        Message.purged_at.is_(None),
    )


def not_stored_while_ignored() -> ColumnElement[bool]:
    """A message that arrived while its chat was in Ignore: the row is
    metadata only (§6.5). Observe always writes the content hash; every other
    row without one is marked — deleted or expired — or is a deletion
    tombstone, which has no direction. A row holding text or media is never
    one, whatever its hash (rows written outside ingestion, such as the eval
    corpus, may have none)."""
    return and_(
        Message.direction.is_not(None),
        Message.content_hash.is_(None),
        Message.text.is_(None),
        Message.media.is_(None),
        Message.deleted_at.is_(None),
        Message.purged_at.is_(None),
    )


async def not_stored_count(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    *,
    after_message_id: int | None = None,
) -> int:
    """How many of a chat's messages were not stored because it was in
    Ignore; with `after_message_id`, only those that arrived after that row."""
    statement = (
        select(func.count())
        .select_from(Message)
        .where(Message.account_id == account_id)
        .where(Message.conversation_id == conversation_id)
        .where(not_stored_while_ignored())
    )
    if after_message_id is not None:
        statement = statement.where(Message.id > after_message_id)
    return int((await session.execute(statement)).scalar_one())


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
            func.count().filter(not_stored_while_ignored()),
        )
        .where(Message.account_id == account_id)
        .where(Message.conversation_id == conversation.id)
    )
    deleted, expired, not_stored = counted.one()
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
        not_stored_while_ignored=int(not_stored),
    )


async def conversation_page(
    session: AsyncSession,
    account_id: int,
    conversation_id: int,
    *,
    before: tuple[datetime, int] | None = None,
    limit: int = 30,
) -> ConversationPage:
    """One page of a conversation's retained messages, newest page first.

    Keyset pagination on `(sent_at, id)`, the order the page is read in, so
    a message whose id is out of step with its date (a re-queued dead
    letter: a new id for an old message) is neither skipped nor repeated.
    Raises `NotInAccountError` for an id that is unknown or another account's.
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
        raise NotInAccountError("conversation not found in this account")
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
    if before is not None:
        statement = statement.where(tuple_(Message.sent_at, Message.id) < tuple_(*before))
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
        next_cursor=(rows[-1].sent_at, rows[-1].id)  # type: ignore[arg-type]  # NOT NULL above
        if more and rows
        else None,
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


@dataclass(frozen=True, slots=True)
class ManagedOrigin:
    """Which of the account's conversations and contacts a forwarded or
    quoted text came from. Empty = not managed content."""

    conversation_ids: tuple[int, ...] = ()
    contact_ids: tuple[int, ...] = ()

    @property
    def managed(self) -> bool:
        return bool(self.conversation_ids or self.contact_ids)

    def as_refs(self) -> dict[str, list[int]]:
        """The `control_turns.refs` shape, so deletion can find the turn."""
        return {
            "conversation_ids": sorted(self.conversation_ids),
            "contact_ids": sorted(self.contact_ids),
        }


async def managed_origin(
    session: AsyncSession,
    account_id: int,
    *,
    user_ids: Sequence[int] = (),
    chat_ids: Sequence[int] = (),
    names: Sequence[str] = (),
) -> ManagedOrigin:
    """The managed chats forwarded or quoted text came from: its sender is a
    known contact, its chat a known conversation, or a hidden sender's name
    equals a contact's name (§6 invariant 9). Unknown origins are not managed:
    there is nothing stored to match them with."""
    ids = list({*user_ids, *chat_ids})
    contacts: set[int] = set()
    if ids:
        contacts |= set(
            (
                await session.execute(
                    select(Contact.id)
                    .where(Contact.account_id == account_id)
                    .where(Contact.telegram_user_id.in_(ids))
                )
            ).scalars()
        )
    wanted = {normalize(name) for name in names if normalize(name)}
    if wanted:
        known = await session.execute(
            select(Contact.id, Contact.display_name)
            .where(Contact.account_id == account_id)
            .where(Contact.display_name.is_not(None))
        )
        contacts |= {
            int(contact_id) for contact_id, value in known.all() if normalize(str(value)) in wanted
        }
    conditions = []
    if ids:
        conditions.append(Conversation.business_chat_id.in_(ids))
    if contacts:
        conditions.append(Conversation.contact_id.in_(contacts))
    conversations: set[int] = set()
    if conditions:
        conversations = set(
            (
                await session.execute(
                    select(Conversation.id)
                    .where(Conversation.account_id == account_id)
                    .where(or_(*conditions))
                )
            ).scalars()
        )
        contacts |= (
            set(
                (
                    await session.execute(
                        select(Conversation.contact_id)
                        .where(Conversation.account_id == account_id)
                        .where(Conversation.id.in_(conversations))
                    )
                ).scalars()
            )
            if conversations
            else set()
        )
    return ManagedOrigin(tuple(sorted(conversations)), tuple(sorted(contacts)))


# ------------------------------------------------------------ name matching
@dataclass(frozen=True, slots=True)
class ContactMatch:
    entry: DirectoryEntry
    #: `exact` > `prefix` > `token_prefix` > `fuzzy`, then the weak kinds
    #: `short_form` and `not_sure` (see `WEAK_KINDS`).
    kind: str
    score: float

    @property
    def is_weak(self) -> bool:
        return self.kind in WEAK_KINDS


_KIND_RANK = {
    "exact": 0,
    "prefix": 1,
    "token_prefix": 2,
    "fuzzy": 3,
    "short_form": 4,
    "not_sure": 5,
}
#: Guesses, never a confirmed person: a nickname-style short form of a name
#: ("Shuxa" for "Shuhrat") and the nearest names when nothing matched. No
#: caller may pick one by itself; the owner confirms it (PF-03, FR-DRF-08).
WEAK_KINDS = frozenset({"short_form", "not_sure"})
FUZZY_THRESHOLD = 0.75
#: The shortest folded query a short form is tried for, and how many letters
#: of it must open a name word.
SHORT_FORM_MIN_LETTERS = 3
SHORT_FORM_MIN_GAP = 2
#: Nearest-name suggestions when nothing matched: at most this many, each at
#: least this similar, so a name that only occurs inside a chat finds none.
NOT_SURE_LIMIT = 3
NOT_SURE_FLOOR = 0.5


def _fold(text: str) -> str:
    """The matching form of a name: the search normal form (case, every
    apostrophe variant, o'/g', Cyrillic) plus x and h as one letter,
    since people write "Shuxrat" and "Shuhrat". Local to matching on purpose:
    `memory.search.normalize` is persisted and versioned, so it never changes
    for this."""
    return normalize(text).replace("x", "h")


def _keys(entry: DirectoryEntry) -> list[str]:
    keys = []
    if entry.display_name:
        keys.append(_fold(entry.display_name))
    if entry.username:
        keys.append(_fold(entry.username.lstrip("@")))
    return [key for key in keys if key]


def _similarity(wanted: str, key: str) -> float:
    return max(
        [difflib.SequenceMatcher(None, wanted, key).ratio()]
        + [difflib.SequenceMatcher(None, wanted, token).ratio() for token in key.split()]
    )


def _is_short_form(wanted: str, tokens: list[str]) -> bool:
    """A nickname-style short form: one word of at least three letters whose
    first three letters open a longer word of the name, by at least two
    letters ("shuha" for "shuhrat"). A shorter gap is a typo ("jassur"), which
    `fuzzy` already takes."""
    if " " in wanted or len(wanted) < SHORT_FORM_MIN_LETTERS:
        return False
    head = wanted[:SHORT_FORM_MIN_LETTERS]
    return any(
        token.startswith(head) and len(token) >= len(wanted) + SHORT_FORM_MIN_GAP
        for token in tokens
    )


def match_contacts(
    directory: Sequence[DirectoryEntry], query: str, *, limit: int = 5
) -> list[ContactMatch]:
    """Rank contacts for a name the owner typed, in any script.

    Names are compared in the matching form, so "Камола", "Kamola" and
    "kamola" are one name, and so are "Shuxrat" and "Shuhrat". Exact beats
    prefix ("kam") beats a prefix of any word of the name ("Aliyeva" in "Kamola
    Aliyeva") beats difflib similarity of at least 0.75 (typos). Below those
    come the weak kinds: `short_form` (a three-letter start shared with a word
    of the name) and, when nothing matched at all, the nearest few names as
    `not_sure`. A weak match is a question for the owner, never a pick.
    """
    wanted = _fold(query.lstrip("@"))
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
            elif _is_short_form(wanted, tokens):
                candidate = ContactMatch(entry, "short_form", _similarity(wanted, key))
            elif (ratio := _similarity(wanted, key)) >= FUZZY_THRESHOLD:
                candidate = ContactMatch(entry, "fuzzy", ratio)
            else:
                continue
            if best is None or (_KIND_RANK[candidate.kind], -candidate.score) < (
                _KIND_RANK[best.kind],
                -best.score,
            ):
                best = candidate
        if best is not None:
            found.append(best)
    if not found:
        return _nearest(directory, wanted)[: max(1, min(limit, NOT_SURE_LIMIT))]
    found.sort(key=_ranking)
    return found[: max(1, limit)]


def _ranking(match: ContactMatch) -> tuple[int, float, float, int]:
    last = match.entry.last_activity_at
    return (
        _KIND_RANK[match.kind],
        -match.score,
        -(last.timestamp() if last else 0.0),
        match.entry.contact_id,
    )


def _nearest(directory: Sequence[DirectoryEntry], wanted: str) -> list[ContactMatch]:
    """The most similar names when nothing matched, as `not_sure` guesses."""
    nearest = []
    for entry in directory:
        ratio = max((_similarity(wanted, key) for key in _keys(entry)), default=0.0)
        if ratio >= NOT_SURE_FLOOR:
            nearest.append(ContactMatch(entry, "not_sure", ratio))
    nearest.sort(key=_ranking)
    return nearest[:NOT_SURE_LIMIT]
