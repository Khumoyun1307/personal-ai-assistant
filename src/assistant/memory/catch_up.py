"""Catch-up and attention read models, and the coverage caveats renderer
(PRD FR-SUM-01, FR-SUM-03, FR-SUM-06; ARCHITECTURE §6.6; S03.2).

Account-scoped queries plus pure functions:

- `active_in_period` — which conversations had retained messages in a range;
- `period_coverage` + `caveats` — what a catch-up over the range cannot
  contain, rendered as plain sentences the answer must carry: possible and
  definite gaps overlapping the range (a `delayed` gap only delayed
  processing and is never reported, §6.6), the period before the business
  connection, chats in Ignore mode with activity, chats that may be paused,
  expired and deleted messages;
- `unanswered` — attention-list candidates: the contact spoke last and
  neither the owner nor the assistant has answered since;
- `importance` — the deterministic grouping of a catch-up.

Caveat sentences carry times and counts only — no names, no message text —
so they are the application's own words, not untrusted content.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry, BusinessConnection, BusinessConnectionEvent
from assistant.conversations.models import Contact, Conversation, CoverageGap, Message

#: Gap certainties a summary reports. `delayed` means the messages arrived
#: late, nothing is missing (ARCHITECTURE §6.6, §10.3 `coverage_gaps`).
REPORTED_CERTAINTIES = ("possible", "definite")


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


def _local(moment: datetime, timezone: str) -> str:
    return moment.astimezone(_zone(timezone)).strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------------ period activity
@dataclass(frozen=True, slots=True)
class ActiveConversation:
    conversation_id: int
    contact_id: int
    display_name: str | None
    username: str | None
    messages_in_period: int
    #: The contact spoke last overall and nobody answered since.
    awaiting_owner_reply: bool


def _awaiting(conversation: Conversation) -> bool:
    if conversation.last_contact_msg_at is None:
        return False
    answers = [
        moment
        for moment in (conversation.last_owner_msg_at, conversation.last_assistant_msg_at)
        if moment is not None
    ]
    return not answers or max(answers) < conversation.last_contact_msg_at


async def active_in_period(
    session: AsyncSession,
    account_id: int,
    *,
    start: datetime,
    end: datetime,
    conversation_ids: Sequence[int] = (),
    contact_ids: Sequence[int] = (),
) -> list[ActiveConversation]:
    """Conversations (not Ignore mode, not bot chats) with retained messages
    sent in `[start, end)`, busiest first."""
    counts = (
        select(Message.conversation_id, func.count().label("n"))
        .where(Message.account_id == account_id)
        .where(Message.sent_at >= start)
        .where(Message.sent_at < end)
        .where(Message.deleted_at.is_(None))
        .where(Message.purged_at.is_(None))
        .where(Message.direction.is_not(None))
        .where((Message.text.is_not(None)) | (Message.media.is_not(None)))
        .group_by(Message.conversation_id)
        .subquery()
    )
    statement = (
        select(Conversation, Contact, counts.c.n)
        .join(counts, counts.c.conversation_id == Conversation.id)
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(Conversation.account_id == account_id)
        .where(Contact.account_id == account_id)
        .where(Contact.is_bot.is_(False))
        .where(Conversation.mode != "ignore")
    )
    if conversation_ids:
        statement = statement.where(Conversation.id.in_(list(conversation_ids)))
    if contact_ids:
        statement = statement.where(Conversation.contact_id.in_(list(contact_ids)))
    rows = (await session.execute(statement.order_by(counts.c.n.desc(), Conversation.id))).all()
    return [
        ActiveConversation(
            conversation_id=conversation.id,
            contact_id=contact.id,
            display_name=contact.display_name,
            username=contact.username,
            messages_in_period=int(n),
            awaiting_owner_reply=_awaiting(conversation),
        )
        for conversation, contact, n in rows
    ]


# ------------------------------------------------------------------ coverage
@dataclass(frozen=True, slots=True)
class GapFact:
    kind: str
    certainty: str
    at_risk_from: datetime
    at_risk_to: datetime
    #: None: the whole account (an outage), else one chat.
    conversation_id: int | None


@dataclass(frozen=True, slots=True)
class PeriodCoverage:
    """What a catch-up over `[start, end)` cannot contain (content-free)."""

    start: datetime
    end: datetime
    #: The earliest moment anything was observed: the first connection event,
    #: the connection's date or the first stored message, whichever is
    #: earliest — never the latest reconnect. None: no business connection.
    coverage_began: datetime | None
    gaps: tuple[GapFact, ...] = ()
    ignored_with_activity: int = 0
    possibly_paused: int = 0
    expired_in_period: int = 0
    deleted_in_period: int = 0
    #: One conversation asked for: account-wide chat counts do not apply.
    single_conversation: bool = False
    #: The connection exists but is not active now (`disabled`, `stale`,
    #: `pending_owner`, or not enabled), and since when.
    connection_inactive: str | None = None
    connection_inactive_since: datetime | None = None
    #: Chats asked about (a scoped catch-up) that are in Ignore mode.
    ignored_in_scope: int = 0
    #: Closed intervals in which the connection was off (disabled, then
    #: reconnected), overlapping the period — from the connection events.
    off_intervals: tuple[tuple[datetime, datetime], ...] = ()
    #: Deletions the owner asked for after the period began that covered the
    #: chats asked about, and the latest one's time: the rows are gone without
    #: tombstones, so the audit row is the marker (M03 D86).
    deleted_on_request: int = 0
    deleted_on_request_last: datetime | None = None


async def period_coverage(
    session: AsyncSession,
    account_id: int,
    *,
    start: datetime,
    end: datetime,
    conversation_ids: Sequence[int] | None = None,
) -> PeriodCoverage:
    """The coverage facts for a range; `conversation_ids` narrows them to the
    chats asked about (None = the whole account)."""
    connection = (
        await session.execute(
            select(
                BusinessConnection.status,
                BusinessConnection.is_enabled,
                BusinessConnection.connected_at,
                BusinessConnection.updated_at,
            ).where(BusinessConnection.account_id == account_id)
        )
    ).first()
    coverage_began: datetime | None = None
    inactive: str | None = None
    inactive_since: datetime | None = None
    off_intervals: tuple[tuple[datetime, datetime], ...] = ()
    if connection is not None:
        status, enabled, connected_at, changed_at = connection
        # `connected_at` moves to the latest reconnect; coverage began at the
        # earliest sign of the connection.
        first_event = (
            await session.execute(
                select(func.min(BusinessConnectionEvent.observed_at)).where(
                    BusinessConnectionEvent.account_id == account_id
                )
            )
        ).scalar_one_or_none()
        first_message = (
            await session.execute(
                select(func.min(Conversation.coverage_started_at)).where(
                    Conversation.account_id == account_id
                )
            )
        ).scalar_one_or_none()
        coverage_began = min(m for m in (connected_at, first_event, first_message) if m is not None)
        closed, open_since = await _off_intervals(session, account_id)
        off_intervals = tuple((off, on) for off, on in closed if off < end and on > start)
        if status != "active" or not enabled:
            inactive = "disabled" if not enabled else str(status)
            # Dated by the event that switched it off, not `updated_at`
            # (a rights change moves that too).
            inactive_since = open_since or changed_at

    gap_query = (
        select(CoverageGap)
        .where(CoverageGap.account_id == account_id)
        .where(CoverageGap.resolved_at.is_(None))
        .where(CoverageGap.certainty.in_(REPORTED_CERTAINTIES))
        .where(CoverageGap.at_risk_from < end)
        .where(CoverageGap.at_risk_to >= start)
    )
    if conversation_ids is not None:
        gap_query = gap_query.where(
            CoverageGap.conversation_id.is_(None)
            | CoverageGap.conversation_id.in_(list(conversation_ids))
        )
    gaps = tuple(
        GapFact(g.kind, g.certainty, g.at_risk_from, g.at_risk_to, g.conversation_id)
        for g in (
            await session.execute(gap_query.order_by(CoverageGap.at_risk_from, CoverageGap.id))
        )
        .scalars()
        .all()
    )

    in_range = [
        Message.account_id == account_id,
        Message.sent_at >= start,
        Message.sent_at < end,
    ]
    if conversation_ids is not None:
        in_range.append(Message.conversation_id.in_(list(conversation_ids)))
    expired, deleted = (
        await session.execute(
            select(
                func.count().filter(Message.purged_at.is_not(None)),
                func.count().filter(Message.deleted_at.is_not(None)),
            ).where(*in_range)
        )
    ).one()

    single = conversation_ids is not None
    ignored = paused = ignored_in_scope = 0
    if single:
        ignored_in_scope = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Conversation)
                    .where(Conversation.account_id == account_id)
                    .where(Conversation.id.in_(list(conversation_ids or ())))
                    .where(Conversation.mode == "ignore")
                )
            ).scalar_one()
        )
    else:
        activity = func.greatest(
            Conversation.last_contact_msg_at,
            Conversation.last_owner_msg_at,
            Conversation.last_assistant_msg_at,
        )
        ignored = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Conversation)
                    .join(Contact, Contact.id == Conversation.contact_id)
                    .where(Conversation.account_id == account_id)
                    .where(Conversation.mode == "ignore")
                    .where(Contact.is_bot.is_(False))
                    .where(activity >= start)
                )
            ).scalar_one()
        )
    paused_query = (
        select(func.count())
        .select_from(Conversation)
        .where(Conversation.account_id == account_id)
        .where(Conversation.availability == "possibly_paused")
    )
    if conversation_ids is not None:
        paused_query = paused_query.where(Conversation.id.in_(list(conversation_ids)))
    paused = int((await session.execute(paused_query)).scalar_one())
    on_request = await _deletions_on_request(session, account_id, start, conversation_ids)
    return PeriodCoverage(
        start=start,
        end=end,
        coverage_began=coverage_began,
        gaps=gaps,
        ignored_with_activity=ignored,
        possibly_paused=paused,
        expired_in_period=int(expired),
        deleted_in_period=int(deleted),
        single_conversation=single,
        connection_inactive=inactive,
        connection_inactive_since=inactive_since,
        ignored_in_scope=ignored_in_scope,
        off_intervals=off_intervals,
        deleted_on_request=len(on_request),
        deleted_on_request_last=max(on_request, default=None),
    )


#: Audit events of deletions the owner asked for (`privacy/deletion.py`;
#: named here, not imported, because privacy depends on memory). Owner
#: deletions remove rows without tombstones, so these rows are the marker
#: that earlier messages existed (M03 D86).
DELETION_EVENTS = ("data_deleted", "managed_data_deleted")


async def _deletions_on_request(
    session: AsyncSession,
    account_id: int,
    start: datetime,
    conversation_ids: Sequence[int] | None,
) -> list[datetime]:
    """When the owner's deletions after `start` that covered the chats asked
    about happened: a deletion at T removed what was sent before T."""
    rows = (
        await session.execute(
            select(
                AuditLogEntry.at,
                AuditLogEntry.event,
                AuditLogEntry.subject_type,
                AuditLogEntry.subject_id,
            )
            .where(AuditLogEntry.account_id == account_id)
            .where(AuditLogEntry.event.in_(DELETION_EVENTS))
            .where(AuditLogEntry.at > start)
        )
    ).all()
    if conversation_ids is None:
        return [at for at, *_ in rows]
    chats = set(conversation_ids)
    contacts = set(
        (
            await session.execute(
                select(Conversation.contact_id)
                .where(Conversation.account_id == account_id)
                .where(Conversation.id.in_(list(chats)))
            )
        )
        .scalars()
        .all()
    )
    return [
        at
        for at, event, subject_type, subject_id in rows
        if event != "data_deleted"  # managed-data deletion: every chat
        or subject_type == "account"
        or (subject_type == "conversation" and subject_id in chats)
        or (subject_type == "contact" and subject_id in contacts)
    ]


#: Connection events that switch observation off, and back on (accounts/
#: connections.py `Change`). An event with `is_enabled = false` is off too.
_OFF_CHANGES = frozenset({"disabled"})
_ON_CHANGES = frozenset({"connected", "reconnected", "activated"})


async def _off_intervals(
    session: AsyncSession, account_id: int
) -> tuple[list[tuple[datetime, datetime]], datetime | None]:
    """The account's off intervals from `business_connection_events`, by
    event time: closed ones `(off, back on)`, and the start of one still
    open (None if observation is on)."""
    rows = (
        await session.execute(
            select(
                BusinessConnectionEvent.change,
                BusinessConnectionEvent.is_enabled,
                BusinessConnectionEvent.observed_at,
            )
            .where(BusinessConnectionEvent.account_id == account_id)
            .order_by(BusinessConnectionEvent.observed_at, BusinessConnectionEvent.id)
        )
    ).all()
    closed: list[tuple[datetime, datetime]] = []
    off_since: datetime | None = None
    for change, enabled, at in rows:
        if change in _OFF_CHANGES or (not enabled and change not in ("stale_event", "foreign")):
            if off_since is None:
                off_since = at
        elif change in _ON_CHANGES and enabled and off_since is not None:
            closed.append((off_since, at))
            off_since = None
    return closed, off_since


def caveats(coverage: PeriodCoverage, timezone: str) -> list[str]:
    """Coverage caveats for a catch-up, in a fixed order. Possible and
    definite gaps only: a `delayed` gap is never reported (FR-SUM-06, §6.6)."""
    lines: list[str] = []
    if coverage.coverage_began is None:
        lines.append(
            "There is no business connection: none of the owner's chats were observed "
            "in this period."
        )
    elif coverage.start < coverage.coverage_began:
        lines.append(
            f"Coverage began at {_local(coverage.coverage_began, timezone)}, when the "
            "assistant was connected: messages before that were never seen."
        )
    for off, on in coverage.off_intervals:
        lines.append(
            f"The business connection was off from {_local(off, timezone)} to "
            f"{_local(on, timezone)}: messages sent in between were not observed and are missing."
        )
    if coverage.connection_inactive is not None:
        since = (
            f" since {_local(coverage.connection_inactive_since, timezone)}"
            if coverage.connection_inactive_since is not None
            else ""
        )
        lines.append(
            f"The business connection is not active ({coverage.connection_inactive}){since}: "
            "new messages are not being observed, so anything sent meanwhile is missing."
        )
    if coverage.ignored_in_scope:
        lines.append(
            "The chat asked about is in Ignore mode: its messages are not stored or summarized."
        )
    for gap in coverage.gaps:
        if gap.certainty not in REPORTED_CERTAINTIES:
            continue
        lines.append(_gap_line(gap, timezone))
    if coverage.possibly_paused:
        lines.append(
            f"{coverage.possibly_paused} chat(s) may be paused or excluded in Telegram's Chat "
            "Automation settings: messages there are not delivered to the assistant."
        )
    if coverage.ignored_with_activity:
        lines.append(
            f"{coverage.ignored_with_activity} chat(s) in Ignore mode had activity since "
            f"{_local(coverage.start, timezone)}; they are not stored or summarized."
        )
    if coverage.expired_in_period:
        lines.append(
            f"{coverage.expired_in_period} message(s) from this period expired under the "
            "retention setting and could not be included."
        )
    if coverage.deleted_in_period:
        lines.append(
            f"{coverage.deleted_in_period} message(s) from this period were deleted and are "
            "not included."
        )
    if coverage.deleted_on_request and coverage.deleted_on_request_last is not None:
        when = _local(coverage.deleted_on_request_last, timezone)
        times = (
            f"at {when}"
            if coverage.deleted_on_request == 1
            else f"{coverage.deleted_on_request} times (the latest at {when})"
        )
        lines.append(
            f"Stored chat data was deleted at the owner's request {times}: messages sent "
            "before a deletion, in the chats it covered, are no longer stored and are not "
            "included."
        )
    return lines


def _gap_line(gap: GapFact, timezone: str) -> str:
    start, end = _local(gap.at_risk_from, timezone), _local(gap.at_risk_to, timezone)
    where = (
        f"in the chat with conversation_id {gap.conversation_id}"
        if gap.conversation_id is not None
        else "in any chat"
    )
    if gap.certainty == "definite":
        if gap.kind == "dead_letter":
            return (
                f"Definite gap: at least one message around {start} {where} could not be "
                "processed and is missing."
            )
        return f"Definite gap: messages sent between {start} and {end} {where} are missing."
    reason = (
        "the assistant was offline for more than 24 hours, and Telegram keeps undelivered "
        "messages only that long"
        if gap.kind == "outage"
        else "the assistant's data was restored from a backup"
        if gap.kind == "restore"
        else "they could not be processed"
    )
    return (
        f"Possible gap: messages sent between {start} and {end} {where} may be missing ({reason})."
    )


# ---------------------------------------------------------------- importance
NEEDS_ATTENTION = "needs_attention"
COMMITMENTS = "commitments"
OTHER = "other"
IMPORTANCE_ORDER = (NEEDS_ATTENTION, COMMITMENTS, OTHER)


def importance(summary: Mapping[str, Any] | None, *, awaiting_owner_reply: bool) -> str:
    """Grouping for a catch-up (FR-SUM-01): questions or decisions for the
    owner, or a reply owed, first; then commitments; then the rest."""
    if summary is None:
        return NEEDS_ATTENTION if awaiting_owner_reply else OTHER
    if (
        summary.get("open_questions_to_owner")
        or summary.get("decisions_requested")
        or (awaiting_owner_reply and summary.get("needs_reply"))
    ):
        return NEEDS_ATTENTION
    if summary.get("commitments"):
        return COMMITMENTS
    return OTHER


# ------------------------------------------------------------------ attention
@dataclass(frozen=True, slots=True)
class Unanswered:
    conversation_id: int
    contact_id: int
    display_name: str | None
    username: str | None
    last_contact_msg_at: datetime
    #: The owner's or assistant's last message, if any.
    last_answer_at: datetime | None
    #: Content arrived that no summary has seen yet (`pending_ai_since`).
    summary_stale: bool


async def unanswered(
    session: AsyncSession, account_id: int, *, since: datetime, limit: int
) -> list[Unanswered]:
    """Attention-list candidates (FR-SUM-03 v1): Observe/Assist chats whose
    last message is the contact's, sent since `since`, with no answer from
    the owner or the assistant after it. Waiting longest first."""
    answered = func.greatest(Conversation.last_owner_msg_at, Conversation.last_assistant_msg_at)
    rows = (
        await session.execute(
            select(Conversation, Contact)
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(Conversation.account_id == account_id)
            .where(Contact.account_id == account_id)
            .where(Contact.is_bot.is_(False))
            .where(Conversation.mode.in_(("observe", "assist")))
            .where(Conversation.last_contact_msg_at.is_not(None))
            .where(Conversation.last_contact_msg_at >= since)
            .where(answered.is_(None) | (answered < Conversation.last_contact_msg_at))
            .order_by(Conversation.last_contact_msg_at, Conversation.id)
            .limit(limit)
        )
    ).all()
    result: list[Unanswered] = []
    for conversation, contact in rows:
        answers = [
            moment
            for moment in (conversation.last_owner_msg_at, conversation.last_assistant_msg_at)
            if moment is not None
        ]
        result.append(
            Unanswered(
                conversation_id=conversation.id,
                contact_id=contact.id,
                display_name=contact.display_name,
                username=contact.username,
                last_contact_msg_at=conversation.last_contact_msg_at,  # type: ignore[arg-type]
                last_answer_at=max(answers) if answers else None,
                summary_stale=conversation.pending_ai_since is not None,
            )
        )
    return result
