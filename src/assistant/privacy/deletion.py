"""Deletion of stored managed-conversation data (FR-ONB-06, J1 step 5; §10.1).

`delete_managed_data` is the account-wide cascade; `purge_conversation` is
the same cascade for one chat (a recognised bot chat, FR-OBS-04).

Offered when the owner declines or revokes the `ai_managed` consent. It
cascades over every content-bearing column that managed chats reach
(`privacy/content_columns.py`): message rows, conversation summaries
(S03.2), contact names, the raw business payloads, and control turns that
quoted managed content. Ids and
tombstones stay where audit needs them; one content-free audit row records
the counts.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, AuditLogEntry, ControlTurn
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.inbox.models import InboundUpdate
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

    @property
    def total(self) -> int:
        return (
            self.messages
            + self.contacts_cleared
            + self.payloads_cleared
            + self.control_turns_cleared
            + self.summaries
        )


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
                "messages_remaining": int(remaining),
            },
        )
    )
    await session.flush()
    return DeletionReport(messages, contacts, payloads, turns, summaries)


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
    return DeletionReport(messages, contacts, payloads, turns, summaries)


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
