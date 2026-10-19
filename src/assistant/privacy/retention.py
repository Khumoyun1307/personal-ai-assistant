"""Retention sweeper (ARCHITECTURE §10.4, AGENTS §9, FR-PRV-03).

Every data class the privacy policy names has a rule here, and the hourly
`system.retention_sweep` applies all of them. What exists in M01:

| Data | Kept | Then |
|---|---|---|
| `inbound_updates.payload` | 7 days | payload cleared; the row stays as content-free metadata |
| `control_turns.text` | 90 days | text cleared; the row stays (role, time, run link) |
| `llm_calls`, `agent_runs` (+ steps), `audit_log` | 1 year | rows deleted (they carry no content) |

Other classes join as their tables arrive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import AuditLogEntry, ControlTurn
from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.inbox.models import InboundUpdate

INBOUND_PAYLOAD_RETENTION = timedelta(days=7)
CONTROL_TURN_TEXT_RETENTION = timedelta(days=90)
ACCOUNTING_RETENTION = timedelta(days=365)


@dataclass(frozen=True, slots=True)
class SweepReport:
    inbound_payloads: int = 0
    control_turn_texts: int = 0
    llm_calls: int = 0
    agent_runs: int = 0
    audit_entries: int = 0

    @property
    def total(self) -> int:
        return (
            self.inbound_payloads
            + self.control_turn_texts
            + self.llm_calls
            + self.agent_runs
            + self.audit_entries
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
    return SweepReport(inbound, turns, calls, runs, audit)
