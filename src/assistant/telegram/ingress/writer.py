"""Durable inbox writes (ARCHITECTURE §6.1).

One batch, one transaction, `ON CONFLICT DO NOTHING`. Telegram re-delivers
anything we have not confirmed with a higher offset, so duplicates are normal
and the unique key is what makes them harmless.

The offset is advanced by the caller **only after this commits** — that is the
whole durability argument.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.db.models import SystemState
from assistant.inbox.models import InboundUpdate
from assistant.telegram.routing import update_kind


async def write_batch(session: AsyncSession, *, bot_id: int, updates: list[dict[str, Any]]) -> int:
    """Insert a batch, ignoring updates already stored. Returns rows written.

    Caller commits. Nothing here advances any offset.
    """
    return len(await write_new_updates(session, bot_id=bot_id, updates=updates))


async def write_new_updates(
    session: AsyncSession, *, bot_id: int, updates: list[dict[str, Any]]
) -> frozenset[int]:
    """`write_batch`, returning the `update_id`s this call actually stored.

    A replayed update (already stored, perhaps already processed) is not
    among them, so the poller's kill-switch fast lane never re-applies an
    old /pause over a later /resume (S04.4; Antony on #107).
    """
    if not updates:
        return frozenset()

    rows = [
        {
            "bot_id": bot_id,
            "update_id": update["update_id"],
            "kind": update_kind(update),
            "payload": update,
            "status": "pending",
        }
        for update in updates
        if "update_id" in update
    ]
    if not rows:
        return frozenset()

    statement = (
        insert(InboundUpdate)
        .values(rows)
        # The redelivery guarantee: a duplicate is a no-op, not an error.
        .on_conflict_do_nothing(index_elements=["bot_id", "update_id"])
        .returning(InboundUpdate.update_id)
    )
    result = await session.execute(statement)
    return frozenset(int(update_id) for update_id in result.scalars().all())


async def read_state(session: AsyncSession, key: str) -> dict[str, Any] | None:
    row = await session.execute(select(SystemState).where(SystemState.key == key))
    state = row.scalar_one_or_none()
    return state.value if state is not None else None


async def write_state(session: AsyncSession, key: str, value: dict[str, Any]) -> None:
    """Upsert a `system_state` row."""
    statement = (
        insert(SystemState)
        .values(key=key, value=value)
        .on_conflict_do_update(index_elements=["key"], set_={"value": value})
    )
    await session.execute(statement)
