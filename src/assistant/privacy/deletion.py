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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, delete, func, or_, select, text, true, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, AuditLogEntry, ControlTurn
from assistant.conversations.models import Contact, Conversation, CoverageGap, Message
from assistant.core.clock import Clock
from assistant.core.errors import NotInAccountError
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

#: The audit event of `delete_managed_data` (S02.4); the owner's scoped
#: deletions write `EVENT_DATA_DELETED`.
EVENT_MANAGED_DATA_DELETED = "managed_data_deleted"

#: Said with every deletion (privacy policy v4: encrypted daily backups, kept
#: up to 35 days; safety review item 6).
BACKUP_NOTE = (
    "Encrypted backups made before now still contain it until they are rotated out (up to 35 days)."
)
#: Said on every deletion card: a pending inbound row is left to the inbox
#: (M03 D79), so a message arriving at that moment may be stored after the
#: deletion (safety review item 4).
ARRIVING_NOTE = "A message arriving at the moment of deletion may still be stored."


def receipt_text(counts: Mapping[str, Any]) -> str:
    """What the owner is told after a deletion (from `DeletionReport.counts`,
    or a deletion tool's result): counts only, and the backups that still
    hold the data for a while (safety review items 5 and 6)."""

    def n(key: str) -> int:
        value = counts.get(key, 0)
        return value if isinstance(value, int) else 0

    return (
        f"🗑 Deleted: {n('messages')} message(s), {n('contacts_cleared')} contact name(s), "
        f"{n('payloads_cleared')} raw update(s), {n('summaries_deleted')} summary(ies), "
        f"{n('memory_items_deleted')} note(s), {n('control_turns_cleared')} turn(s) of our "
        f"conversation here, {n('held_calls_cleared')} held change(s).\n" + BACKUP_NOTE
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

    def receipt(self) -> str:
        """What the owner is told after a deletion (`receipt_text`)."""
        return receipt_text(self.counts())

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


def _owner_turns_up_to(cutoff: int | None) -> ColumnElement[bool]:
    """Turns a deletion may clear: every assistant turn, and the owner's own
    turns up to `cutoff` (the newest turn id when the owner tapped Delete).
    A question sent after the tap waits behind the deletion (same lock) and
    must keep its text, or its run answers a blank (fix round 2, item 2).
    Answers are never newer than the tap's queue position: the runs that
    write them are serialized with the deletion."""
    if cutoff is None:
        return true()
    return or_(ControlTurn.role != "owner", ControlTurn.id <= cutoff)


async def latest_turn_id(session: AsyncSession, account_id: int) -> int:
    """The `cutoff` for `_owner_turns_up_to`: the account's newest control
    turn (0 if none). An id, never content, so it can be a job argument."""
    newest = (
        await session.execute(
            select(func.max(ControlTurn.id)).where(ControlTurn.account_id == account_id)
        )
    ).scalar_one_or_none()
    return int(newest or 0)


async def delete_managed_data(
    session: AsyncSession,
    *,
    account_id: int,
    clock: Clock,
    actor: str = "owner",
    request: DeletionRequest | None = None,
    owner_turns_up_to: int | None = None,
) -> DeletionReport:
    """Delete everything stored from the owner's managed chats, in one
    transaction (the caller's). Conversations and coverage gaps stay: they
    hold no content and keep the coverage record honest. `request` keys the
    audit row so a repeated job deletes once (the revocation card's Delete
    runs as `privacy.delete_data`, M03 D83)."""
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
    # The owner's raw control-chat updates may repeat a managed chat's text
    # (a panel press carries the rendered summary, a forward): as for every
    # other scope (fix round 2, item 6).
    payloads += await _clear_control_payloads(session, account)
    await _settle_dead_letters(session, account_id, None, clock)
    turns = _count(
        await session.execute(
            update(ControlTurn)
            .where(ControlTurn.account_id == account_id)
            .where(ControlTurn.contains_managed.is_(True))
            .where(_owner_turns_up_to(owner_turns_up_to))
            .where((ControlTurn.text.is_not(None)) | (ControlTurn.quoted_text.is_not(None)))
            .values(text=None, quoted_text=None)
        )
    )
    # Held tool calls may carry managed text in their arguments (S03.1).
    # Named by table: the model belongs to the agent layer (import-linter).
    held = _count(
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
    remaining = (
        await session.execute(
            select(func.count()).select_from(Message).where(Message.account_id == account_id)
        )
    ).scalar_one()
    report = DeletionReport(messages, contacts, payloads, turns, summaries, notes, held)
    details: dict[str, Any] = {**report.counts(), "messages_remaining": int(remaining)}
    if request is not None:
        details.update(request.key)
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=clock.now(),
            actor=actor,
            event=EVENT_MANAGED_DATA_DELETED,
            details=details,
        )
    )
    await session.flush()
    return report


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
#: The revocation card's Delete (`delete_managed_data`), queued like the rest.
SCOPE_MANAGED = "managed"


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


async def _clear_chat_payloads(
    session: AsyncSession, account_id: int, chats: Sequence[_Chat]
) -> int:
    """Raw updates of these chats, in one statement: those linked to their
    messages and every business update naming one of them (jsonb
    containment, never a cast). A pending row is left to the inbox: it is
    still to be processed, and is then stored or cleared like any later
    update (the cards say so)."""
    if not chats:
        return 0
    message_ids = select(Message.id).where(
        Message.conversation_id.in_([chat.conversation_id for chat in chats])
    )
    named = [
        (InboundUpdate.kind == kind)
        & InboundUpdate.payload.contains({kind: {"chat": {"id": chat.business_chat_id}}})
        for chat in chats
        for kind in _CHAT_PAYLOAD_KINDS
    ]
    return _rows(
        await session.execute(
            update(InboundUpdate)
            .where(InboundUpdate.account_id == account_id)
            .where(InboundUpdate.payload.is_not(None))
            .where(InboundUpdate.status != "pending")
            .where(or_(InboundUpdate.message_id.in_(message_ids), *named))
            .values(payload=None)
        )
    )


#: `conversations.autonomy_paused_reason` of a chat whose update was
#: dead-lettered (`conversations/service.py`; not imported: conversations
#: imports this module).
_PAUSED_BY_INGEST_ERROR = "ingest_error"


async def _settle_dead_letters(
    session: AsyncSession, account_id: int, conversation_ids: Sequence[int] | None, clock: Clock
) -> None:
    """Dead-lettered updates of these chats (None: the whole account) whose
    payload is now gone can never be requeued (M03 D89). Their definite gaps
    are resolved and a chat's ingest pause is lifted once no open dead letter
    remains — what `resolve_ingest_failure` does after a requeue. The gap
    stood for a message that is not stored; after the owner's deletion
    nothing from before it is, and the deletion's audit row says so to every
    catch-up over that time (D86; fix round 3, F)."""
    unrecoverable = or_(
        CoverageGap.inbound_update_id.is_(None),  # its row aged out already
        CoverageGap.inbound_update_id.in_(
            select(InboundUpdate.id).where(InboundUpdate.payload.is_(None))
        ),
    )
    statement = (
        update(CoverageGap)
        .where(CoverageGap.account_id == account_id)
        .where(CoverageGap.kind == "dead_letter")
        .where(CoverageGap.resolved_at.is_(None))
        .where(unrecoverable)
    )
    if conversation_ids is not None:
        statement = statement.where(CoverageGap.conversation_id.in_(list(conversation_ids)))
    settled = (
        (
            await session.execute(
                statement.values(resolved_at=clock.now()).returning(CoverageGap.conversation_id)
            )
        )
        .scalars()
        .all()
    )
    chats = sorted({int(chat) for chat in settled if chat is not None})
    if not chats:
        return
    still_open = (
        select(CoverageGap.conversation_id)
        .where(CoverageGap.kind == "dead_letter")
        .where(CoverageGap.resolved_at.is_(None))
        .where(CoverageGap.conversation_id.is_not(None))
    )
    await session.execute(
        update(Conversation)
        .where(Conversation.account_id == account_id)
        .where(Conversation.id.in_(chats))
        .where(Conversation.autonomy_paused_reason == _PAUSED_BY_INGEST_ERROR)
        .where(Conversation.id.not_in(still_open))
        .values(autonomy_paused_reason=None, updated_at=clock.now())
    )


async def _redact_turns(
    session: AsyncSession,
    account_id: int,
    *,
    conversation_ids: Sequence[int],
    contact_ids: Sequence[int],
    owner_turns_up_to: int | None = None,
) -> tuple[int, list[int]]:
    """Clear control turns whose `refs` name one of these conversations or
    contacts (§10.4), the owner's own only up to the tap
    (`_owner_turns_up_to`). Returns (turns cleared, the runs that wrote
    them)."""
    conditions = [ControlTurn.refs.contains({"conversation_ids": [c]}) for c in conversation_ids]
    conditions += [ControlTurn.refs.contains({"contact_ids": [c]}) for c in contact_ids]
    if not conditions:
        return 0, []
    rows = (
        await session.execute(
            update(ControlTurn)
            .where(ControlTurn.account_id == account_id)
            .where(or_(*conditions))
            .where(_owner_turns_up_to(owner_turns_up_to))
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


#: SQL: the held call's run never recorded its answer — it was cut off (a
#: crash, a shutdown) — so the chats it drew on are unknown (an answer's
#: `refs` are the only record of them). A held call is confirmable for a day,
#: so a deletion clears such calls too (fail safe; fix round 3). Deletions
#: run under the owner's control lock, like owner runs, so no run is in
#: flight then: a run without an answer is one that was cut off.
_RUN_WITHOUT_ANSWER = (
    "(run_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM app.control_turns AS t "
    "WHERE t.run_id = app.tool_confirmations.run_id AND t.role = 'assistant'))"
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
    runs whose answers were redacted, those whose arguments name a deleted
    conversation or contact, and those of runs cut off before their answer
    (`_RUN_WITHOUT_ANSWER`). Their arguments go; a pending one expires, so no
    tap can apply it. Named by table: the model belongs to the agent layer
    (import-linter)."""
    matches = [_RUN_WITHOUT_ANSWER]
    if run_ids:
        matches.append("run_id = ANY(CAST(:run_ids AS bigint[]))")
    if conversation_ids:
        matches.append(_names_one_of("conversation_id", "conversation_ids"))
    if contact_ids:
        matches.append(_names_one_of("contact_id", "contact_ids"))
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
    owner_turns_up_to: int | None = None,
) -> DeletionReport:
    """Everything stored from these (already locked) chats: raw updates,
    messages, summaries, notes about the chats, the turns that drew on the
    chats or their contacts, the held calls of those runs or naming the
    chats, and the contacts' names. With `person_ids` (per-contact deletion)
    also the notes about those people and the held calls naming them. The
    conversation rows stay, content-free."""
    conversation_ids = [chat.conversation_id for chat in chats]
    contact_ids = sorted({chat.contact_id for chat in chats} | set(person_ids))
    payloads = await _clear_chat_payloads(session, account_id, chats)
    await _settle_dead_letters(session, account_id, conversation_ids, clock)
    # The owner's own raw control-chat updates may quote the chat (a forward,
    # a press whose message is the rendered panel, a confirmed card); they
    # exist only for recovery, so all of them go (safety review item 2).
    account = await session.get(Account, account_id)
    if account is not None:
        payloads += await _clear_control_payloads(session, account)
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
    notes = 0
    for person_id in person_ids:
        # Their notes, their chats' notes, items saved from runs that drew on them.
        notes += await items.delete_for_contact(session, account_id, person_id, conversation_ids)
    notes += await items.delete_for_chats(session, account_id, conversation_ids, contact_ids)
    turns, run_ids = await _redact_turns(
        session,
        account_id,
        conversation_ids=conversation_ids,
        contact_ids=contact_ids,
        owner_turns_up_to=owner_turns_up_to,
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
    """What asked for a deletion (S03.5), so a repeated request deletes once:
    a tool call (`run_id`, `tool_call_id`) or an owner's card (`card_id`).
    The key goes into the deletion's audit row (ids only)."""

    key: Mapping[str, int | str]

    @classmethod
    def tool_call(cls, run_id: int, tool_call_id: str) -> DeletionRequest:
        return cls({"run_id": run_id, "tool_call_id": tool_call_id})

    @classmethod
    def card(cls, card_id: int) -> DeletionRequest:
        return cls({"card_id": card_id})


async def find_deletion(
    session: AsyncSession, account_id: int, request: DeletionRequest
) -> AuditLogEntry | None:
    """The audit row of a deletion this request already made, if any."""
    return (
        (
            await session.execute(
                select(AuditLogEntry)
                .where(AuditLogEntry.account_id == account_id)
                # The revocation card's Delete writes the managed-data event.
                .where(AuditLogEntry.event.in_((EVENT_DATA_DELETED, EVENT_MANAGED_DATA_DELETED)))
                .where(AuditLogEntry.details.contains(dict(request.key)))
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
        details.update(request.key)
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
    owner_turns_up_to: int | None = None,
) -> DeletionReport:
    """Everything stored from one chat (FR-PRV-04), in the caller's
    transaction: its messages, summaries, raw updates and notes about the
    chat, the answers that drew on it, the contact's names (known only from
    the chat). The owner's notes about the person (contact scope) stay;
    `delete_contact_data` takes those too (M03 D79). NotInAccountError: not this
    account's conversation."""
    chats = await _lock_chats(session, account_id, Conversation.id == conversation_id)
    if not chats:
        raise NotInAccountError("conversation not found in this account")
    report = await _purge_chats(
        session,
        account_id,
        chats,
        person_ids=(),
        clock=clock,
        owner_turns_up_to=owner_turns_up_to,
    )
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
    owner_turns_up_to: int | None = None,
) -> DeletionReport:
    """Everything stored about one person: all their chats (as
    `delete_conversation_data`), the owner's notes about them, held calls
    naming them, their names. The contact row stays, content-free (ids, bot
    flag), and so do their chats' modes (M03 D79). NotInAccountError: not this
    account's contact."""
    found = (
        await session.execute(
            select(Contact.id)
            .where(Contact.account_id == account_id)
            .where(Contact.id == contact_id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotInAccountError("contact not found in this account")
    chats = await _lock_chats(session, account_id, Conversation.contact_id == contact_id)
    report = await _purge_chats(
        session,
        account_id,
        chats,
        person_ids=[contact_id],
        clock=clock,
        owner_turns_up_to=owner_turns_up_to,
    )
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
    owners = [
        (InboundUpdate.kind == kind) & InboundUpdate.payload.contains({kind: {key: {"id": owner}}})
        for kind, key in _CONTROL_PAYLOADS
        for owner in sorted(ids)
    ]
    # One statement (general review nit), as for the chats' payloads.
    return _rows(
        await session.execute(
            update(InboundUpdate)
            .where(InboundUpdate.account_id.is_(None))
            .where(InboundUpdate.payload.is_not(None))
            .where(InboundUpdate.status != "pending")
            .where(or_(*owners))
            .values(payload=None)
        )
    )


async def delete_everything(
    session: AsyncSession,
    *,
    account_id: int,
    actor: str,
    clock: Clock,
    request: DeletionRequest | None = None,
    owner_turns_up_to: int | None = None,
) -> DeletionReport:
    """All of the account's content (FR-PRV-04 "delete everything"), in the
    caller's transaction: every message, summary, note (the owner's own
    too), name, raw update, control-chat text (the owner's own turns up to
    `owner_turns_up_to`, the tap) and held call argument. The account stays
    usable: its settings, consents, connection, conversation
    modes, coverage record, audit log and run accounting are kept, and new
    messages are stored as before (M03 D78)."""
    # §9.4: the account first (FOR NO KEY UPDATE, as managed-data deletion:
    # compatible with ingestion's foreign-key share), then its conversations.
    account = await session.get(Account, account_id, with_for_update={"key_share": True})
    if account is None:
        raise NotInAccountError("unknown account")
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
    await _settle_dead_letters(session, account_id, None, clock)
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
            .where(_owner_turns_up_to(owner_turns_up_to))
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
