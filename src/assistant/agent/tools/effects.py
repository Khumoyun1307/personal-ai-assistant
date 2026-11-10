"""Shared durable proposal/revision identities, checked under the account lock.

Only ids and semantic digests are retained; effects and their identity are
committed together behind the existing unique audit-effect index.
Identity rows use their own subject type, outside action lifecycle history.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.agent.tools.registry import ToolContext


async def find_effect(session: AsyncSession, ctx: ToolContext) -> AuditLogEntry | None:
    if not ctx.call_id:
        return None
    return await session.scalar(
        select(AuditLogEntry).where(
            AuditLogEntry.account_id == ctx.account_id,
            AuditLogEntry.event == "tool_effect_recorded",
            # Retain lookup compatibility with previously recorded identities.
            AuditLogEntry.subject_type.in_(["tool_effect", "action"]),
            AuditLogEntry.details["run_id"].astext == str(ctx.run_id),
            AuditLogEntry.details["tool_call_id"].astext == ctx.call_id,
        )
    )


async def record_effect(
    session: AsyncSession,
    ctx: ToolContext,
    *,
    action_id: int,
    version: int,
    semantic_digest: str,
) -> None:
    session.add(
        AuditLogEntry(
            account_id=ctx.account_id,
            at=ctx.services.clock.now(),
            actor="system",
            event="tool_effect_recorded",
            subject_type="tool_effect",
            subject_id=action_id,
            details={
                "effect_action_id": action_id,
                "effect_version": version,
                "run_id": ctx.run_id,
                "tool_call_id": ctx.call_id,
                "semantic_digest": semantic_digest,
            },
        )
    )
    await session.flush()
