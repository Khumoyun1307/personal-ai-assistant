"""Retention sweeper (ARCHITECTURE §10.4, AGENTS §9, FR-PRV-03).

Every data class the privacy policy names has a rule here, and the hourly
`system.retention_sweep` applies all of them:

| Data | Kept | Then |
|---|---|---|
| `inbound_updates.payload` | 7 days (owner: 1-7) | payload cleared; the row stays |
| `inbound_updates` rows | 30 days (not pending) | rows deleted (metadata; S03.5) |
| `control_turns.text`, `quoted_text` | 90 days (owner: 14-90) | texts cleared; the row stays |
| `tool_confirmations.args` | until decided / 24 h | cleared (`expired` if undecided) |
| `llm_calls`, `agent_runs` (+ steps), `audit_log` | 1 year | rows deleted (they carry no content) |
| `business_connection_events` | 1 year | rows deleted (ids, rights, flags only) |
| `foreign_connections` | 30 days after last seen | rows deleted (metadata of non-owners) |
| `messages` content | 90 days (owner: 14-90) | content cleared, `purged_at` set |
| (text, media, search key, hash) | rollup tried 10 days before | gap noted if never summarized |
| `contacts` names | while message content is retained | names cleared afterwards |
| `pending_inputs` | until expiry | rows deleted (questions and cards) |
| `conversation_summaries` (period) | 30 days | rows deleted (catch-up caches, S03.2) |
| `conversation_summaries` (rolling) | until deleted | deletion cascades only (R-07) |
| `memory_items` | until deleted; forgotten 24 h | forgotten rows deleted (S03.3) |

**Owner overrides (S03.5).** The classes marked "owner" follow each
account's setting (`privacy/retention_policy.py`, `accounts.settings`),
read on every sweep — so a changed value takes effect at the next sweep.
Each such statement runs per group of accounts with the same period. Raw
updates not routed to an account (the control chat's, unknown senders')
use the shortest period any account chose, never longer than the default
(M03 D80).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import BigInteger, column, delete, func, select, text, update, values
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import (
    Account,
    AuditLogEntry,
    BusinessConnectionEvent,
    ControlTurn,
    ForeignConnection,
)
from assistant.accounts.service import sweep_expired_cards
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.core.jobs import JobScheduler
from assistant.db.uow import transaction
from assistant.inbox.models import InboundUpdate
from assistant.memory.items import sweep_forgotten
from assistant.memory.summaries import (
    covered_through,
    defer_refresh,
    rollup_candidates,
    sweep_period_summaries,
)
from assistant.observability.logging import get_logger
from assistant.privacy import retention_policy
from assistant.privacy.retention_policy import CONTROL_CHAT, MESSAGES, RAW_UPDATES

logger = get_logger(__name__)

#: The defaults (§10.4); an account may shorten them (S03.5).
INBOUND_PAYLOAD_RETENTION = timedelta(days=retention_policy.CLASSES[RAW_UPDATES].default_days)
CONTROL_TURN_TEXT_RETENTION = timedelta(days=retention_policy.CLASSES[CONTROL_CHAT].default_days)
MESSAGE_CONTENT_RETENTION = timedelta(days=retention_policy.CLASSES[MESSAGES].default_days)
#: An inbound row is kept as content-free metadata this long (§10.4).
INBOUND_ROW_RETENTION = timedelta(days=30)
ACCOUNTING_RETENTION = timedelta(days=365)
FOREIGN_CONNECTION_RETENTION = timedelta(days=30)
#: A held tool call waits as long as its confirmation card (`CARD_TTL`).
TOOL_CONFIRMATION_TTL = timedelta(hours=24)


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
    tool_confirmations: int = 0
    period_summaries: int = 0
    #: Forgotten memory items deleted after their Undo window (S03.3).
    forgotten_memories: int = 0
    #: Chats whose rollup was queued before the ceiling (S03.3; not a deletion).
    rollups_scheduled: int = 0
    #: Inbound metadata rows deleted after 30 days (S03.5).
    inbound_rows: int = 0

    @property
    def total(self) -> int:
        return (
            self.forgotten_memories
            + self.rollups_scheduled
            + self.tool_confirmations
            + self.inbound_payloads
            + self.control_turn_texts
            + self.llm_calls
            + self.agent_runs
            + self.audit_entries
            + self.connection_events
            + self.foreign_connections
            + self.message_contents
            + self.contact_names
            + self.expired_inputs
            + self.period_summaries
            + self.inbound_rows
        )


def _count(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


# ----------------------------------------------------------- owner overrides
async def account_settings(session: AsyncSession) -> dict[int, Mapping[str, Any]]:
    """Every account's settings, read once per sweep (ids and small enums)."""
    rows = await session.execute(select(Account.id, Account.settings))
    return {int(account_id): settings or {} for account_id, settings in rows.all()}


def periods_by_account(
    settings: Mapping[int, Mapping[str, Any]], key: str
) -> dict[timedelta, list[int]]:
    """Accounts grouped by their retention period of class `key`."""
    groups: dict[timedelta, list[int]] = defaultdict(list)
    for account_id, chosen in sorted(settings.items()):
        groups[retention_policy.period(chosen, key)].append(account_id)
    return dict(groups)


async def _groups(
    session_factory: async_sessionmaker[AsyncSession], key: str
) -> dict[timedelta, list[int]]:
    async with session_factory() as session:
        return periods_by_account(await account_settings(session), key)


# ----------------------------------------------------------- raw updates
async def sweep_inbound_payloads(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock
) -> int:
    """Clear payloads older than the retention period. Returns rows cleared."""
    now = clock.now()
    async with session_factory() as session:
        settings = await account_settings(session)
    groups = periods_by_account(settings, RAW_UPDATES)
    cleared = 0
    async with transaction(session_factory) as uow:

        def clear(cutoff: datetime) -> Any:
            return (
                update(InboundUpdate)
                .where(InboundUpdate.received_at < cutoff)
                .where(InboundUpdate.payload.is_not(None))
                # A pending row still needs its payload to be processed;
                # clearing it would turn a delay into data loss. It is swept
                # once handled.
                .where(InboundUpdate.status != "pending")
                .values(payload=None)
            )

        for period, account_ids in groups.items():
            cleared += _count(
                await uow.session.execute(
                    clear(now - period).where(InboundUpdate.account_id.in_(account_ids))
                )
            )
        # Rows with no account (the control chat's, unknown senders'): the
        # shortest period any owner chose, never longer than the default
        # (the dead-letter alert states the same date).
        shortest = retention_policy.unrouted_period(settings.values(), RAW_UPDATES)
        cleared += _count(
            await uow.session.execute(
                clear(now - shortest).where(InboundUpdate.account_id.is_(None))
            )
        )
    return cleared


async def sweep_inbound_rows(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock
) -> int:
    """Inbound rows past their 30 days as metadata (§10.4). A pending row is
    never dropped (it is still to be processed); messages and coverage gaps
    that point at a row keep a NULL link (ON DELETE SET NULL)."""
    async with transaction(session_factory) as uow:
        return _count(
            await uow.session.execute(
                delete(InboundUpdate)
                .where(InboundUpdate.received_at < clock.now() - INBOUND_ROW_RETENTION)
                .where(InboundUpdate.status != "pending")
            )
        )


# ----------------------------------------------------------- control chat
async def sweep_control_turns(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock
) -> int:
    """Control-chat texts past each account's period (both columns together)."""
    now = clock.now()
    cleared = 0
    for period, account_ids in (await _groups(session_factory, CONTROL_CHAT)).items():
        async with transaction(session_factory) as uow:
            cleared += _count(
                await uow.session.execute(
                    update(ControlTurn)
                    .where(ControlTurn.account_id.in_(account_ids))
                    .where(ControlTurn.created_at < now - period)
                    .where((ControlTurn.text.is_not(None)) | (ControlTurn.quoted_text.is_not(None)))
                    .values(text=None, quoted_text=None)
                )
            )
    return cleared


async def sweep_all(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    *,
    scheduler: JobScheduler | None = None,
    rollups_enabled: bool = False,
) -> SweepReport:
    """Apply every retention rule. Each class in its own short transaction.
    The rollup before the message ceiling runs only with background
    summaries on (flag `summary.refresh`, M03 D61) and a scheduler."""
    now = clock.now()
    inbound = await sweep_inbound_payloads(session_factory, clock)
    inbound_rows = await sweep_inbound_rows(session_factory, clock)
    turns = await sweep_control_turns(session_factory, clock)
    confirmations = await sweep_tool_confirmations(session_factory, clock)
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
        period_summaries = await sweep_period_summaries(uow.session, now)
    async with transaction(session_factory) as uow:
        expired = await sweep_expired_cards(uow.session, clock)
    async with transaction(session_factory) as uow:
        forgotten = await sweep_forgotten(uow.session, now)
    # The rollup (content 80-90 days old) runs last and on its own: the
    # purges never depend on it, and a failure here is logged, not raised
    # (review item 1). It retries at the next hourly sweep.
    rollups = 0
    if rollups_enabled and scheduler is not None:
        try:
            rollups = await schedule_rollups(session_factory, clock, scheduler)
        except Exception as error:
            logger.error("rollup_scheduling_failed", exc_type=type(error).__name__)
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
        confirmations,
        period_summaries,
        forgotten,
        rollups,
        inbound_rows,
    )


async def sweep_tool_confirmations(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock
) -> int:
    """Held tool calls (S03.1): a pending one expires with its card (24 h) and
    loses its arguments; arguments of decided calls are cleared if anything
    left them; rows go after a year with the run accounting. Named by table:
    the model belongs to the agent layer (import-linter)."""
    now = clock.now()
    async with transaction(session_factory) as uow:
        expired = _count(
            await uow.session.execute(
                text(
                    "UPDATE app.tool_confirmations SET status = 'expired', args = NULL, "
                    "decided_at = :now WHERE status = 'pending' AND created_at < :cutoff"
                ),
                {"now": now, "cutoff": now - TOOL_CONFIRMATION_TTL},
            )
        )
        # Confirmed but never applied for a day (the worker was down and the
        # reconciler's window passed): fail it rather than act on day-old
        # intent, and drop the arguments.
        expired += _count(
            await uow.session.execute(
                text(
                    "UPDATE app.tool_confirmations SET status = 'failed', args = NULL "
                    "WHERE status = 'confirmed' AND decided_at < :cutoff"
                ),
                {"cutoff": now - TOOL_CONFIRMATION_TTL},
            )
        )
        cleared = _count(
            await uow.session.execute(
                text(
                    "UPDATE app.tool_confirmations SET args = NULL "
                    "WHERE status IN ('applied', 'failed', 'rejected', 'expired') "
                    "AND args IS NOT NULL"
                )
            )
        )
        await uow.session.execute(
            text("DELETE FROM app.tool_confirmations WHERE created_at < :cutoff"),
            {"cutoff": now - ACCOUNTING_RETENTION},
        )
    return expired + cleared


#: The rollup starts this long before the retention ceiling (§10.4: from day
#: `retention_days - 10`), so a summary can still be written from the
#: messages before they go.
ROLLUP_LEAD = timedelta(days=10)


async def schedule_rollups(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock, scheduler: JobScheduler
) -> int:
    """Try the rolling summary before the ceiling (§10.4, S03.3).

    A chat whose retained content will expire within `ROLLUP_LEAD` (of its
    account's message retention) and that its rolling summary has not
    covered yet is flagged (`pending_ai_since`, no later than that content)
    and its `memory.refresh_summary` is queued for now — only for accounts
    with an active `ai_managed` consent, never for Ignore-mode or bot chats.
    The job re-checks consent, budget and back-off when it runs; the purge
    never waits for it. Returns the number of chats queued."""
    now = clock.now()
    flagged: list[int] = []
    # Per group of accounts with the same message retention (S03.5).
    for period, account_ids in (await _groups(session_factory, MESSAGES)).items():
        async with transaction(session_factory) as uow:
            due = await rollup_candidates(
                uow.session, now - (period - ROLLUP_LEAD), account_ids=account_ids
            )
            if not due:
                continue
            # One short transaction with no account lock and no network call
            # (AGENTS §13): the conversation rows are locked in id order
            # (§9.4), then flagged in one UPDATE; a chat set to Ignore
            # meanwhile is left alone and not queued (review item 8).
            ids = sorted(conversation_id for conversation_id, _ in due)
            await uow.session.execute(
                select(Conversation.id)
                .where(Conversation.id.in_(ids))
                .order_by(Conversation.id)
                .with_for_update()
            )
            batch = values(
                column("cid", BigInteger),
                column("oldest", TIMESTAMP(timezone=True)),
                name="due",
            ).data(due)
            returned = await uow.session.execute(
                update(Conversation)
                .where(Conversation.id == batch.c.cid)
                .where(Conversation.mode != "ignore")
                .values(
                    pending_ai_since=func.least(
                        func.coalesce(Conversation.pending_ai_since, batch.c.oldest),
                        batch.c.oldest,
                    )
                )
                .returning(Conversation.id)
            )
            flagged.extend(int(row) for row in returned.scalars().all())
    flagged.sort()
    queued = 0
    for conversation_id in flagged:
        # After the commit: a queueing-lock conflict (a refresh already
        # queued) must not roll the flag back; that queued job does the work.
        if await defer_refresh(scheduler, conversation_id, at=None) is not None:
            queued += 1
    return queued


async def sweep_message_content(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock
) -> int:
    """Purge message content past each account's retention ceiling (§10.4).

    Unconditional: the ceiling holds whether or not a summary exists (the
    rollup only *tries* to run first, `schedule_rollups`). When content goes
    that the chat's rolling summary never covered, the conversation remembers
    it in `summary_gap_before`, so later summaries can say that earlier
    messages expired unsummarised.
    """
    purged = 0
    for period, account_ids in (await _groups(session_factory, MESSAGES)).items():
        purged += await _purge_messages(session_factory, clock, period, account_ids)
    return purged


async def _purge_messages(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    period: timedelta,
    account_ids: list[int],
) -> int:
    now = clock.now()
    cutoff = now - period
    async with transaction(session_factory) as uow:
        due = (
            Message.account_id.in_(account_ids)
            & (func.coalesce(Message.edited_at, Message.sent_at) < cutoff)
            & ((Message.text.is_not(None)) | (Message.media.is_not(None)))
        )
        # §9.4 lock order: the conversations first, then their messages —
        # as ingestion and deletion do — so the sweep cannot deadlock with
        # them on the same chat.
        await uow.session.execute(
            select(Conversation.id)
            .where(Conversation.id.in_(select(Message.conversation_id).where(due)))
            .order_by(Conversation.id)
            .with_for_update()
        )
        purged = await uow.session.execute(
            update(Message)
            .where(due)
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
            .returning(Message.conversation_id, Message.id, Message.sent_at)
        )
        rows = purged.all()
        covers = await covered_through(uow.session, {int(row[0]) for row in rows})
        newest: dict[int, datetime] = {}
        for conversation_id, message_id, sent_at in rows:
            covered = covers.get(conversation_id)
            if covered is not None and message_id <= covered:
                continue  # the rolling summary keeps it (the rollup ran)
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
    """Clear names of contacts with no retained message content (§10.4),
    once the account's message retention has passed since a name was last
    written. A message in an Ignore chat writes none, so it does not keep an
    earlier name (M03 fix round C3, B2)."""
    cleared = 0
    for period, account_ids in (await _groups(session_factory, MESSAGES)).items():
        async with transaction(session_factory) as uow:
            retained = (
                select(Conversation.contact_id)
                .join(Message, Message.conversation_id == Conversation.id)
                .where((Message.text.is_not(None)) | (Message.media.is_not(None)))
            )
            cleared += _count(
                await uow.session.execute(
                    update(Contact)
                    .where(Contact.account_id.in_(account_ids))
                    .where((Contact.display_name.is_not(None)) | (Contact.username.is_not(None)))
                    .where(Contact.id.not_in(retained))
                    # Only names written longer ago than the period (`updated_at`
                    # moves with the names only): a contact named today whose
                    # message holds no content yet still keeps the name.
                    .where(Contact.updated_at < clock.now() - period)
                    .values(display_name=None, username=None, updated_at=clock.now())
                )
            )
    return cleared
