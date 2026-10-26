"""Deletion of stored managed-conversation data (FR-ONB-06, J1 step 5; §10.1).

`delete_managed_data` is the account-wide cascade; `purge_conversation` is
the same cascade for one chat (a recognised bot chat, FR-OBS-04). The
owner's own deletions (S03.5, FR-PRV-04) are `delete_conversation_data`,
`delete_contact_data` and `delete_everything`, at the end of this module.

Offered when the owner declines or revokes the `ai_managed` consent. It
cascades over every content-bearing column that managed chats reach
(`privacy/content_columns.py`): message rows, conversation summaries
(S03.2), contact names, the raw business payloads, and control turns that
quoted managed content. Ids and
tombstones stay where audit needs them; one content-free audit row records
the counts.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, func, or_, select, text, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, AuditLogEntry, ControlTurn
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.inbox.models import InboundUpdate
from assistant.memory import items
from assistant.memory.summaries import delete_account_summaries, delete_conversation_summaries

#: Raw updates that carry managed content, or the owner's name from the
#: connection payload (`business_connection.user`).
MANAGED_PAYLOAD_KINDS = (
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    "business_connection",
)


@dataclass(frozen=True, slots=True)
class DeletionReport:
    messages: int
    contacts_cleared: int
    payloads_cleared: int
    control_turns_cleared: int
    #: `conversation_summaries` rows deleted (S03.2).
    summaries: int = 0
    #: `memory_items` about a contact or chat deleted (S03.3).
    memory_items: int = 0
    #: Held tool calls whose arguments were cleared (S03.5).
    held_calls_cleared: int = 0

    @property
    def total(self) -> int:
        return (
            self.memory_items
            + self.messages
            + self.contacts_cleared
            + self.payloads_cleared
            + self.control_turns_cleared
            + self.summaries
            + self.held_calls_cleared
        )

    def counts(self) -> dict[str, int]:
        """For the content-free audit row and the owner's receipt."""
        return {
            "messages": self.messages,
            "contacts_cleared": self.contacts_cleared,
            "payloads_cleared": self.payloads_cleared,
            "control_turns_cleared": self.control_turns_cleared,
            "summaries_deleted": self.summaries,
            "memory_items_deleted": self.memory_items,
            "held_calls_cleared": self.held_calls_cleared,
        }


async def delete_managed_data(
    session: AsyncSession, *, account_id: int, clock: Clock
) -> DeletionReport:
    """Delete everything stored from the owner's managed chats, in one
    transaction (the caller's). Conversations and coverage gaps stay: they
    hold no content and keep the coverage record honest."""
    # FOR NO KEY UPDATE, not FOR UPDATE: ingestion holds a conversation and
    # then inserts a message, which takes a key share on the account through
    # the foreign key; FOR UPDATE would block that while this waits for the
    # conversation — a deadlock. Nothing here changes the account's key.
    account = await session.get(Account, account_id, with_for_update={"key_share": True})
    if account is None:
        raise ValueError("unknown account")
    # §9.4 lock order: account, then its conversations. A summary being
    # stored meanwhile waits on the conversation lock and then finds its
    # source gone (memory/summaries.py), so nothing deleted here is written
    # back.
    await session.execute(
        select(Conversation.id)
        .where(Conversation.account_id == account_id)
        .order_by(Conversation.id)
        .with_for_update()
    )

    def _count(result: object) -> int:
        return int(getattr(result, "rowcount", 0) or 0)

    messages = _count(
        await session.execute(delete(Message).where(Message.account_id == account_id))
    )
    summaries = await delete_account_summaries(session, account_id)
    # Notes about the people in those chats (S03.3, M03 D71); the owner's
    # own instructions, preferences and facts stay.
    notes = await items.delete_about_contacts(session, account_id)
    contacts = _count(
        await session.execute(
            update(Contact)
            .where(Contact.account_id == account_id)
            .where((Contact.display_name.is_not(None)) | (Contact.username.is_not(None)))
            .values(display_name=None, username=None, updated_at=clock.now())
        )
    )
    payloads = _count(
        await session.execute(
            update(InboundUpdate)
            .where(InboundUpdate.account_id == account_id)
            .where(InboundUpdate.kind.in_(MANAGED_PAYLOAD_KINDS))
            .where(InboundUpdate.payload.is_not(None))
            .values(payload=None)
        )
    )
    turns = _count(
        await session.execute(
            update(ControlTurn)
            .where(ControlTurn.account_id == account_id)
            .where(ControlTurn.contains_managed.is_(True))
            .where((ControlTurn.text.is_not(None)) | (ControlTurn.quoted_text.is_not(None)))
            .values(text=None, quoted_text=None)
        )
    )
    # Held tool calls may carry managed text in their arguments (S03.1).
    # Named by table: the model belongs to the agent layer (import-linter).
    await session.execute(
        text(
            "UPDATE app.tool_confirmations SET args = NULL, "
            "status = CASE WHEN status = 'pending' THEN 'expired' ELSE status END, "
            "decided_at = COALESCE(decided_at, :now) "
            "WHERE account_id = :account_id AND args IS NOT NULL"
        ),
        {"account_id": account_id, "now": clock.now()},
    )
    await session.execute(
        update(Conversation)
        .where(Conversation.account_id == account_id)
        .values(pending_ai_since=None, updated_at=clock.now())
    )
    remaining = (
        await session.execute(
            select(func.count()).select_from(Message).where(Message.account_id == account_id)
        )
    ).scalar_one()
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=clock.now(),
            actor="owner",
            event="managed_data_deleted",
            details={
                "messages": messages,
                "contacts_cleared": contacts,
                "payloads_cleared": payloads,
                "control_turns_cleared": turns,
                "summaries_deleted": summaries,
                "memory_items_deleted": notes,
                "messages_remaining": int(remaining),
            },
        )
    )
    await session.flush()
    return DeletionReport(messages, contacts, payloads, turns, summaries, notes)


#: Business update kinds whose payload names a chat (`<kind>.chat.id`).
_CHAT_PAYLOAD_KINDS = (
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
)


async def purge_conversation(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    business_chat_id: int,
    clock: Clock,
) -> DeletionReport:
    """Delete everything stored from ONE conversation, in the caller's
    transaction: its messages, its contact's names, and every raw update
    that names the chat (linked or not — a failed or Ignore-mode row holds
    content too). The conversation row stays, content-free, so later traffic
    is recognised. Used when a chat turns out to be a third-party bot
    (FR-OBS-04, §6.5). Raw updates are matched by jsonb containment, never by
    casting a value that may not be a number."""

    def _count(result: object) -> int:
        return int(getattr(result, "rowcount", 0) or 0)

    # The conversation row first (§9.4; the caller usually holds it already):
    # an in-flight summary store waits and then finds the chat purged.
    await session.execute(
        select(Conversation.id)
        .where(Conversation.account_id == account_id)
        .where(Conversation.id == conversation_id)
        .with_for_update()
    )
    message_ids = select(Message.id).where(Message.conversation_id == conversation_id)
    payloads = _count(
        await session.execute(
            update(InboundUpdate)
            .where(InboundUpdate.account_id == account_id)
            .where(InboundUpdate.message_id.in_(message_ids))
            .where(InboundUpdate.payload.is_not(None))
            .values(payload=None)
        )
    )
    for kind in _CHAT_PAYLOAD_KINDS:
        payloads += _count(
            await session.execute(
                update(InboundUpdate)
                .where(InboundUpdate.account_id == account_id)
                .where(InboundUpdate.kind == kind)
                .where(InboundUpdate.payload.is_not(None))
                .where(InboundUpdate.payload.contains({kind: {"chat": {"id": business_chat_id}}}))
                .values(payload=None)
            )
        )
    messages = _count(
        await session.execute(
            delete(Message)
            .where(Message.account_id == account_id)
            .where(Message.conversation_id == conversation_id)
        )
    )
    summaries = await delete_conversation_summaries(session, account_id, conversation_id)
    contact_id = (
        await session.execute(
            select(Conversation.contact_id)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    notes = await items.delete_for_conversation(
        session, account_id, conversation_id, [] if contact_id is None else [int(contact_id)]
    )
    turns = await redact_turns_referring_to(
        session, account_id=account_id, conversation_id=conversation_id, clock=clock
    )
    contacts = _count(
        await session.execute(
            update(Contact)
            .where(Contact.account_id == account_id)
            .where(
                Contact.id
                == select(Conversation.contact_id)
                .where(Conversation.id == conversation_id)
                .scalar_subquery()
            )
            .where((Contact.display_name.is_not(None)) | (Contact.username.is_not(None)))
            .values(display_name=None, username=None, updated_at=clock.now())
        )
    )
    await session.flush()
    return DeletionReport(messages, contacts, payloads, turns, summaries, notes)


async def redact_turns_referring_to(
    session: AsyncSession, *, account_id: int, conversation_id: int, clock: Clock
) -> int:
    """Clear control turns whose `refs` name this conversation or its contact
    (an answer that quoted it, ARCHITECTURE §10.4), and the arguments of tool
    calls held in the runs that wrote them. Returns the turns cleared.

    Matched by jsonb containment on the two id lists the agent writes
    (`{"conversation_ids": [...], "contact_ids": [...]}`)."""
    contact_id = (
        await session.execute(
            select(Conversation.contact_id)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    refers = ControlTurn.refs.contains({"conversation_ids": [conversation_id]})
    if contact_id is not None:
        refers = refers | ControlTurn.refs.contains({"contact_ids": [contact_id]})
    redacted = await session.execute(
        update(ControlTurn)
        .where(ControlTurn.account_id == account_id)
        .where(refers)
        .where((ControlTurn.text.is_not(None)) | (ControlTurn.quoted_text.is_not(None)))
        .values(text=None, quoted_text=None)
        .returning(ControlTurn.run_id)
    )
    rows = redacted.all()
    run_ids = sorted({run_id for (run_id,) in rows if run_id is not None})
    if run_ids:
        # Named by table: the model belongs to the agent layer (import-linter).
        await session.execute(
            text(
                "UPDATE app.tool_confirmations SET args = NULL, "
                "status = CASE WHEN status = 'pending' THEN 'expired' ELSE status END, "
                "decided_at = COALESCE(decided_at, :now) "
                "WHERE account_id = :account_id AND run_id = ANY(:run_ids) "
                "AND args IS NOT NULL"
            ),
            {"account_id": account_id, "run_ids": run_ids, "now": clock.now()},
        )
    return len(rows)


# ====================================================================== S03.5
# Owner-requested deletion per conversation, per contact, or of everything
# (FR-PRV-04; ARCHITECTURE §10.1, §10.4). Each cascades over every content
# column (`privacy/content_columns.py`) within its scope and writes exactly
# one content-free audit row. Rows that carry no content and keep the
# owner's choices stay: the conversation (its mode), the contact (ids, bot
# flag), consents, the connection, settings, coverage gaps, the audit log
# and the run accounting (M03 D78, D79).
EVENT_DATA_DELETED = "data_deleted"
SCOPE_CONVERSATION = "conversation"
SCOPE_CONTACT = "contact"
SCOPE_EVERYTHING = "everything"


@dataclass(frozen=True, slots=True)
class _Chat:
    conversation_id: int
    business_chat_id: int
    contact_id: int


def _rows(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


async def _lock_chats(session: AsyncSession, account_id: int, condition: Any) -> list[_Chat]:
    """The account's conversations matching `condition`, locked FOR UPDATE in
    id order (§9.4): a summary store waits for this deletion and then finds
    its source gone (S03.2 re-check), so no deleted text is written back."""
    rows = await session.execute(
        select(Conversation.id, Conversation.business_chat_id, Conversation.contact_id)
        .where(Conversation.account_id == account_id)
        .where(condition)
        .order_by(Conversation.id)
        .with_for_update()
    )
    return [_Chat(int(c), int(b), int(k)) for c, b, k in rows.all()]


async def _clear_chat_payloads(session: AsyncSession, account_id: int, chat: _Chat) -> int:
    """Raw updates of one chat: those linked to its messages and every
    business update naming the chat (jsonb containment, never a cast). A
    pending row is left to the inbox: it is still to be processed, and is
    then stored or cleared like any later update."""
    message_ids = select(Message.id).where(Message.conversation_id == chat.conversation_id)
    cleared = _rows(
        await session.execute(
            update(InboundUpdate)
            .where(InboundUpdate.account_id == account_id)
            .where(InboundUpdate.message_id.in_(message_ids))
            .where(InboundUpdate.payload.is_not(None))
            .where(InboundUpdate.status != "pending")
            .values(payload=None)
        )
    )
    for kind in _CHAT_PAYLOAD_KINDS:
        cleared += _rows(
            await session.execute(
                update(InboundUpdate)
                .where(InboundUpdate.account_id == account_id)
                .where(InboundUpdate.kind == kind)
                .where(InboundUpdate.payload.is_not(None))
                .where(InboundUpdate.status != "pending")
                .where(
                    InboundUpdate.payload.contains({kind: {"chat": {"id": chat.business_chat_id}}})
                )
                .values(payload=None)
            )
        )
    return cleared


async def _redact_turns(
    session: AsyncSession,
    account_id: int,
    *,
    conversation_ids: Sequence[int],
    contact_ids: Sequence[int],
) -> tuple[int, list[int]]:
    """Clear control turns whose `refs` name one of these conversations or
    contacts (§10.4). Returns (turns cleared, the runs that wrote them)."""
    conditions = [ControlTurn.refs.contains({"conversation_ids": [c]}) for c in conversation_ids]
    conditions += [ControlTurn.refs.contains({"contact_ids": [c]}) for c in contact_ids]
    if not conditions:
        return 0, []
    rows = (
        await session.execute(
            update(ControlTurn)
            .where(ControlTurn.account_id == account_id)
            .where(or_(*conditions))
            .where((ControlTurn.text.is_not(None)) | (ControlTurn.quoted_text.is_not(None)))
            .values(text=None, quoted_text=None)
            .returning(ControlTurn.run_id)
        )
    ).all()
    return len(rows), sorted({run_id for (run_id,) in rows if run_id is not None})


def _names_one_of(key: str, parameter: str) -> str:
    """SQL: the held call's argument `key` is one of the ids in `:parameter`
    (a number check first, so the cast never sees anything else)."""
    return (
        f"CASE WHEN jsonb_typeof(args->'{key}') = 'number' "
        f"THEN (args->>'{key}')::numeric = ANY(CAST(:{parameter} AS numeric[])) ELSE false END"
    )


async def _clear_held_args(
    session: AsyncSession,
    account_id: int,
    *,
    run_ids: Sequence[int] = (),
    conversation_ids: Sequence[int] = (),
    contact_ids: Sequence[int] = (),
    clock: Clock,
) -> int:
    """Held tool calls (S03.1) that may carry the scope's text: those of the
    runs whose answers were redacted, and those whose arguments name a
    deleted conversation or contact. Their arguments go; a pending one
    expires, so no tap can apply it. Named by table: the model belongs to the
    agent layer (import-linter)."""
    matches = []
    if run_ids:
        matches.append("run_id = ANY(CAST(:run_ids AS bigint[]))")
    if conversation_ids:
        matches.append(_names_one_of("conversation_id", "conversation_ids"))
    if contact_ids:
        matches.append(_names_one_of("contact_id", "contact_ids"))
    if not matches:
        return 0
    return _rows(
        await session.execute(
            # Only fixed fragments of this module are joined; every value is bound.
            text(
                "UPDATE app.tool_confirmations SET args = NULL, "  # noqa: S608
                "status = CASE WHEN status = 'pending' THEN 'expired' ELSE status END, "
                "decided_at = COALESCE(decided_at, :now) "
                "WHERE account_id = :account_id AND args IS NOT NULL "
                f"AND ({' OR '.join(matches)})"
            ),
            {
                "account_id": account_id,
                "now": clock.now(),
                "run_ids": list(run_ids),
                "conversation_ids": list(conversation_ids),
                "contact_ids": list(contact_ids),
            },
        )
    )


async def _clear_names(
    session: AsyncSession, account_id: int, contact_ids: Sequence[int], clock: Clock
) -> int:
    if not contact_ids:
        return 0
    return _rows(
        await session.execute(
            update(Contact)
            .where(Contact.account_id == account_id)
            .where(Contact.id.in_(list(contact_ids)))
            .where((Contact.display_name.is_not(None)) | (Contact.username.is_not(None)))
            .values(display_name=None, username=None, updated_at=clock.now())
        )
    )


async def _purge_chats(
    session: AsyncSession,
    account_id: int,
    chats: Sequence[_Chat],
    *,
    person_ids: Sequence[int],
    clock: Clock,
) -> DeletionReport:
    """Everything stored from these (already locked) chats: raw updates,
    messages, summaries, notes about the chats, the turns that drew on the
    chats or their contacts, the held calls of those runs or naming the
    chats, and the contacts' names. With `person_ids` (per-contact deletion)
    also the notes about those people and the held calls naming them. The
    conversation rows stay, content-free."""
    conversation_ids = [chat.conversation_id for chat in chats]
    contact_ids = sorted({chat.contact_id for chat in chats} | set(person_ids))
    payloads = 0
    for chat in chats:
        payloads += await _clear_chat_payloads(session, account_id, chat)
    messages = summaries = 0
    if conversation_ids:
        messages = _rows(
            await session.execute(
                delete(Message)
                .where(Message.account_id == account_id)
                .where(Message.conversation_id.in_(conversation_ids))
            )
        )
        for conversation_id in conversation_ids:
            summaries += await delete_conversation_summaries(session, account_id, conversation_id)
        await session.execute(
            update(Conversation)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id.in_(conversation_ids))
            .values(pending_ai_since=None, updated_at=clock.now())
        )
    notes = await items.delete_for_scope(
        session, account_id, conversation_ids=conversation_ids, contact_ids=person_ids
    )
    turns, run_ids = await _redact_turns(
        session, account_id, conversation_ids=conversation_ids, contact_ids=contact_ids
    )
    held = await _clear_held_args(
        session,
        account_id,
        run_ids=run_ids,
        conversation_ids=conversation_ids,
        contact_ids=person_ids,
        clock=clock,
    )
    names = await _clear_names(session, account_id, contact_ids, clock)
    return DeletionReport(messages, names, payloads, turns, summaries, notes, held)


@dataclass(frozen=True, slots=True)
class DeletionRequest:
    """The tool call that asked (S03.5), so a repeated call deletes once."""

    run_id: int
    tool_call_id: str


async def find_deletion(
    session: AsyncSession, account_id: int, request: DeletionRequest
) -> AuditLogEntry | None:
    """The audit row of a deletion this tool call already made, if any."""
    return (
        (
            await session.execute(
                select(AuditLogEntry)
                .where(AuditLogEntry.account_id == account_id)
                .where(AuditLogEntry.event == EVENT_DATA_DELETED)
                .where(
                    AuditLogEntry.details.contains(
                        {"run_id": request.run_id, "tool_call_id": request.tool_call_id}
                    )
                )
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


def _audit(
    session: AsyncSession,
    *,
    account_id: int,
    actor: str,
    scope: str,
    subject_type: str,
    subject_id: int,
    report: DeletionReport,
    request: DeletionRequest | None,
    clock: Clock,
) -> None:
    """The one content-free row of a deletion: scope, ids and counts."""
    details: dict[str, Any] = {"scope": scope, **report.counts()}
    if request is not None:
        details.update(run_id=request.run_id, tool_call_id=request.tool_call_id)
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=clock.now(),
            actor=actor,
            event=EVENT_DATA_DELETED,
            subject_type=subject_type,
            subject_id=subject_id,
            details=details,
        )
    )


async def delete_conversation_data(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    actor: str,
    clock: Clock,
    request: DeletionRequest | None = None,
) -> DeletionReport:
    """Everything stored from one chat (FR-PRV-04), in the caller's
    transaction: its messages, summaries, raw updates and notes about the
    chat, the answers that drew on it, the contact's names (known only from
    the chat). The owner's notes about the person (contact scope) stay;
    `delete_contact_data` takes those too (M03 D79). LookupError: not this
    account's conversation."""
    chats = await _lock_chats(session, account_id, Conversation.id == conversation_id)
    if not chats:
        raise LookupError("conversation not found in this account")
    report = await _purge_chats(session, account_id, chats, person_ids=(), clock=clock)
    _audit(
        session,
        account_id=account_id,
        actor=actor,
        scope=SCOPE_CONVERSATION,
        subject_type="conversation",
        subject_id=conversation_id,
        report=report,
        request=request,
        clock=clock,
    )
    await session.flush()
    return report


async def delete_contact_data(
    session: AsyncSession,
    *,
    account_id: int,
    contact_id: int,
    actor: str,
    clock: Clock,
    request: DeletionRequest | None = None,
) -> DeletionReport:
    """Everything stored about one person: all their chats (as
    `delete_conversation_data`), the owner's notes about them, held calls
    naming them, their names. The contact row stays, content-free (ids, bot
    flag), and so do their chats' modes (M03 D79). LookupError: not this
    account's contact."""
    found = (
        await session.execute(
            select(Contact.id)
            .where(Contact.account_id == account_id)
            .where(Contact.id == contact_id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise LookupError("contact not found in this account")
    chats = await _lock_chats(session, account_id, Conversation.contact_id == contact_id)
    report = await _purge_chats(session, account_id, chats, person_ids=[contact_id], clock=clock)
    _audit(
        session,
        account_id=account_id,
        actor=actor,
        scope=SCOPE_CONTACT,
        subject_type="contact",
        subject_id=contact_id,
        report=report,
        request=request,
        clock=clock,
    )
    await session.flush()
    return report


#: Control-chat update kinds and where their payload names the owner. These
#: rows are not routed to an account (`account_id` stays NULL), so they are
#: found by the owner's chat or user id (jsonb containment).
_CONTROL_PAYLOADS = (
    ("message", "chat"),
    ("edited_message", "chat"),
    ("callback_query", "from"),
    ("callback_query", "from_user"),
)


async def _clear_control_payloads(session: AsyncSession, account: Account) -> int:
    """The owner's own raw control-chat updates (their messages, and button
    presses whose payload repeats the card's text), not yet swept."""
    ids = {account.telegram_user_id}
    if account.control_chat_id is not None:
        ids.add(account.control_chat_id)
    cleared = 0
    for kind, key in _CONTROL_PAYLOADS:
        for owner_id in sorted(ids):
            cleared += _rows(
                await session.execute(
                    update(InboundUpdate)
                    .where(InboundUpdate.account_id.is_(None))
                    .where(InboundUpdate.kind == kind)
                    .where(InboundUpdate.payload.is_not(None))
                    .where(InboundUpdate.status != "pending")
                    .where(InboundUpdate.payload.contains({kind: {key: {"id": owner_id}}}))
                    .values(payload=None)
                )
            )
    return cleared


async def delete_everything(
    session: AsyncSession,
    *,
    account_id: int,
    actor: str,
    clock: Clock,
    request: DeletionRequest | None = None,
) -> DeletionReport:
    """All of the account's content (FR-PRV-04 "delete everything"), in the
    caller's transaction: every message, summary, note (the owner's own
    too), name, raw update, control-chat text and held call argument. The
    account stays usable: its settings, consents, connection, conversation
    modes, coverage record, audit log and run accounting are kept, and new
    messages are stored as before (M03 D78)."""
    # §9.4: the account first (FOR NO KEY UPDATE, as managed-data deletion:
    # compatible with ingestion's foreign-key share), then its conversations.
    account = await session.get(Account, account_id, with_for_update={"key_share": True})
    if account is None:
        raise LookupError("unknown account")
    await _lock_chats(session, account_id, true())
    payloads = _rows(
        await session.execute(
            update(InboundUpdate)
            .where(InboundUpdate.account_id == account_id)
            .where(InboundUpdate.payload.is_not(None))
            .where(InboundUpdate.status != "pending")
            .values(payload=None)
        )
    )
    payloads += await _clear_control_payloads(session, account)
    messages = _rows(await session.execute(delete(Message).where(Message.account_id == account_id)))
    summaries = await delete_account_summaries(session, account_id)
    notes = await items.delete_all(session, account_id)
    names = _rows(
        await session.execute(
            update(Contact)
            .where(Contact.account_id == account_id)
            .where((Contact.display_name.is_not(None)) | (Contact.username.is_not(None)))
            .values(display_name=None, username=None, updated_at=clock.now())
        )
    )
    turns = _rows(
        await session.execute(
            update(ControlTurn)
            .where(ControlTurn.account_id == account_id)
            .where((ControlTurn.text.is_not(None)) | (ControlTurn.quoted_text.is_not(None)))
            .values(text=None, quoted_text=None)
        )
    )
    held = _rows(
        await session.execute(
            text(
                "UPDATE app.tool_confirmations SET args = NULL, "
                "status = CASE WHEN status = 'pending' THEN 'expired' ELSE status END, "
                "decided_at = COALESCE(decided_at, :now) "
                "WHERE account_id = :account_id AND args IS NOT NULL"
            ),
            {"account_id": account_id, "now": clock.now()},
        )
    )
    await session.execute(
        update(Conversation)
        .where(Conversation.account_id == account_id)
        .values(pending_ai_since=None, updated_at=clock.now())
    )
    report = DeletionReport(messages, names, payloads, turns, summaries, notes, held)
    _audit(
        session,
        account_id=account_id,
        actor=actor,
        scope=SCOPE_EVERYTHING,
        subject_type="account",
        subject_id=account_id,
        report=report,
        request=request,
        clock=clock,
    )
    await session.flush()
    return report
