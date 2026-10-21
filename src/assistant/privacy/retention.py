"""Retention sweeper (ARCHITECTURE §10.4, AGENTS §9, FR-PRV-03).

Every data class the privacy policy names has a rule here, and the hourly
`system.retention_sweep` applies all of them. What exists in M01:

| Data | Kept | Then |
|---|---|---|
| `inbound_updates.payload` | 7 days | payload cleared; the row stays as content-free metadata |
| `control_turns.text` | 90 days | text cleared; the row stays (role, time, run link) |
| `llm_calls`, `agent_runs` (+ steps), `audit_log` | 1 year | rows deleted (they carry no content) |
| `business_connection_events` | 1 year | rows deleted (ids, rights, flags only) |
| `foreign_connections` | 30 days after last seen | rows deleted (metadata of non-owners) |
| `messages` content | 90 days from `sent_at`/last edit | content cleared, `purged_at` set, kept |
| (text, media, search key, hash) | | `summary_gap_before` advanced (rollup: M03) |
| `contacts` names | while message content is retained | names cleared afterwards |
| `pending_inputs` | until expiry | rows deleted (questions and cards) |

Other classes join as their tables arrive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import (
    AuditLogEntry,
    BusinessConnectionEvent,
    ControlTurn,
    ForeignConnection,
)
from assistant.accounts.service import sweep_expired_cards
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.inbox.models import InboundUpdate

INBOUND_PAYLOAD_RETENTION = timedelta(days=7)
CONTROL_TURN_TEXT_RETENTION = timedelta(days=90)
MESSAGE_CONTENT_RETENTION = timedelta(days=90)
ACCOUNTING_RETENTION = timedelta(days=365)
FOREIGN_CONNECTION_RETENTION = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class SweepReport:
    inbound_payloads: int = 0
    control_turn_texts: int = 0
    llm_calls: int = 0
    agent_runs: int = 0
    audit_entries: int = 0
    connection_events: int = 0
    foreign_connections: int = 0
    message_contents: int = 0
    contact_names: int = 0
    expired_inputs: int = 0

    @property
    def total(self) -> int:
        return (
            self.inbound_payloads
            + self.control_turn_texts
            + self.llm_calls
            + self.agent_runs
            + self.audit_entries
            + self.connection_events
            + self.foreign_connections
            + self.message_contents
            + self.contact_names
            + self.expired_inputs
        )


def _count(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


async def sweep_inbound_payloads(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock
) -> int:
    """Clear payloads older than the retention period. Returns rows cleared."""
    cutoff = clock.now() - INBOUND_PAYLOAD_RETENTION
    async with transaction(session_factory) as uow:
        result = await uow.session.execute(
            update(InboundUpdate)
            .where(InboundUpdate.received_at < cutoff)
            .where(InboundUpdate.payload.is_not(None))
            # A pending row still needs its payload to be processed; clearing
            # it would turn a delay into data loss. It is swept once handled.
            .where(InboundUpdate.status != "pending")
            .values(payload=None)
        )
        return _count(result)


async def sweep_all(session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> SweepReport:
    """Apply every retention rule. Each class in its own short transaction."""
    now = clock.now()
    inbound = await sweep_inbound_payloads(session_factory, clock)
    async with transaction(session_factory) as uow:
        turns = _count(
            await uow.session.execute(
                update(ControlTurn)
                .where(ControlTurn.created_at < now - CONTROL_TURN_TEXT_RETENTION)
                .where(ControlTurn.text.is_not(None))
                .values(text=None)
            )
        )
    accounting_cutoff = now - ACCOUNTING_RETENTION
    async with transaction(session_factory) as uow:
        # `llm_calls` and `agent_runs` belong to the agent layer, which a domain
        # package may not import (import-linter); they are named by table here
        # so the retention policy stays in one place.
        calls = _count(
            await uow.session.execute(
                text("DELETE FROM app.llm_calls WHERE created_at < :cutoff"),
                {"cutoff": accounting_cutoff},
            )
        )
        # Steps go with their run (ON DELETE CASCADE); turns keep a NULL run link.
        runs = _count(
            await uow.session.execute(
                text("DELETE FROM app.agent_runs WHERE started_at < :cutoff"),
                {"cutoff": accounting_cutoff},
            )
        )
        audit = _count(
            await uow.session.execute(
                delete(AuditLogEntry).where(AuditLogEntry.at < accounting_cutoff)
            )
        )
        events = _count(
            await uow.session.execute(
                delete(BusinessConnectionEvent).where(
                    BusinessConnectionEvent.observed_at < accounting_cutoff
                )
            )
        )
    async with transaction(session_factory) as uow:
        foreign = _count(
            await uow.session.execute(
                delete(ForeignConnection).where(
                    ForeignConnection.last_seen_at < now - FOREIGN_CONNECTION_RETENTION
                )
            )
        )
    message_contents = await sweep_message_content(session_factory, clock)
    contact_names = await sweep_contact_names(session_factory, clock)
    async with transaction(session_factory) as uow:
        expired = await sweep_expired_cards(uow.session, clock)
    return SweepReport(
        inbound,
        turns,
        calls,
        runs,
        audit,
        events,
        foreign,
        message_contents,
        contact_names,
        expired,
    )


async def sweep_message_content(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock
) -> int:
    """Purge message content past the retention ceiling (§10.4).

    Unconditional: the ceiling holds whether or not a summary exists (M03
    adds the rollup that *tries* to run first). When content goes without a
    summary the conversation remembers it in `summary_gap_before`, so later
    summaries can say that earlier messages expired unsummarised.
    """
    now = clock.now()
    cutoff = now - MESSAGE_CONTENT_RETENTION
    async with transaction(session_factory) as uow:
        purged = await uow.session.execute(
            update(Message)
            .where(func.coalesce(Message.edited_at, Message.sent_at) < cutoff)
            .where((Message.text.is_not(None)) | (Message.media.is_not(None)))
            .values(
                text=None,
                media=None,
                search_key=None,
                script=None,
                lang_guess=None,
                content_hash=None,
                purged_at=now,
                updated_at=now,
            )
            .returning(Message.conversation_id, Message.sent_at)
        )
        rows = purged.all()
        newest: dict[int, datetime] = {}
        for conversation_id, sent_at in rows:
            if sent_at is not None and (
                conversation_id not in newest or sent_at > newest[conversation_id]
            ):
                newest[conversation_id] = sent_at
        for conversation_id, sent_at in newest.items():
            await uow.session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .where(
                    (Conversation.summary_gap_before.is_(None))
                    | (Conversation.summary_gap_before < sent_at)
                )
                .values(summary_gap_before=sent_at, updated_at=now)
            )
        return len(rows)


async def sweep_contact_names(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock
) -> int:
    """Clear names of contacts with no retained message content (§10.4)."""
    async with transaction(session_factory) as uow:
        retained = (
            select(Conversation.contact_id)
            .join(Message, Message.conversation_id == Conversation.id)
            .where((Message.text.is_not(None)) | (Message.media.is_not(None)))
        )
        result = await uow.session.execute(
            update(Contact)
            .where((Contact.display_name.is_not(None)) | (Contact.username.is_not(None)))
            .where(Contact.id.not_in(retained))
            # Only contacts whose conversations have aged out: a contact seen
            # today without content yet still gets to keep their name.
            .where(Contact.updated_at < clock.now() - MESSAGE_CONTENT_RETENTION)
            .values(display_name=None, username=None, updated_at=clock.now())
        )
        return _count(result)
