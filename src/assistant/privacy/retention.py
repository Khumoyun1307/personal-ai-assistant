"""Retention sweeper (ARCHITECTURE §10.4, AGENTS §9).

M01 sweeps the one content class it has a rule for: the raw Telegram payload
in `inbound_updates`, kept 7 days for replay and debugging, then cleared.
The row stays (ids, status, timestamps) so idempotency and the inbox history
survive; only content goes. Other classes join as their tables arrive.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.inbox.models import InboundUpdate

INBOUND_PAYLOAD_RETENTION = timedelta(days=7)


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
        return int(getattr(result, "rowcount", 0) or 0)
