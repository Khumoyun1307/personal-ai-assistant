"""`set_contact_priority` (ARCHITECTURE §8.2; FR-TRI-04; S05.2 §3.6).

A `configure` tool: held for a confirmation card in a tainted run, otherwise
executed and echoed with a one-tap Undo (S03.3 pattern). The domain setter
(`conversations/priority.py`) validates, scopes by account and audits; this
handler translates, and is idempotent on `(run_id, tool_call_id)` through
that audit row: a retried call finds its earlier change and does nothing
again. The model gets ids and enums only.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.agent.tools.common import require_contacts
from assistant.agent.tools.echo import queue_echo
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
)
from assistant.conversations import priority
from assistant.conversations.models import Contact
from assistant.db.uow import transaction
from assistant.memory.summaries import preferred_name

PRIORITY_FLAG = "tool.set_contact_priority"

#: What each priority does to the contact's alerts (S05.2 §3.1 rule 2), for
#: the echo, the confirmation card and the model alike.
PRIORITY_EFFECT = {
    "vip": "their alerts are raised one step, so more of them reach you right away",
    "normal": "their alerts follow the usual rules",
    "low": "their alerts are lowered one step, so most wait for your daily digest",
}
_LABELS = {"vip": "VIP", "normal": "normal priority", "low": "low priority"}


class SetContactPriorityArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: int = Field(description="The contact (from resolve_contact or another result).")
    priority: Literal["vip", "normal", "low"] = Field(
        description="vip: raise their alerts one step; low: lower them one step; normal: neither."
    )


async def _name(session: AsyncSession, account_id: int, contact_id: int) -> str:
    row = (
        await session.execute(
            select(Contact.display_name, Contact.username)
            .where(Contact.account_id == account_id)
            .where(Contact.id == contact_id)
        )
    ).first()
    name = None if row is None else preferred_name(row[0], row[1])
    return html.escape(name or "this contact")


async def _already_applied(session: AsyncSession, ctx: ToolContext) -> bool:
    """This call (`run_id`, `call_id`) changed the priority before (a retry)."""
    if not ctx.call_id:
        return False
    found = await session.scalar(
        select(AuditLogEntry.id)
        .where(AuditLogEntry.account_id == ctx.account_id)
        .where(AuditLogEntry.event == priority.EVENT_PRIORITY_CHANGED)
        .where(AuditLogEntry.details["run_id"].astext == str(ctx.run_id))
        .where(AuditLogEntry.details["call_id"].astext == ctx.call_id)
        .limit(1)
    )
    return found is not None


async def set_contact_priority_tool(ctx: ToolContext, args: SetContactPriorityArgs) -> ToolOutcome:
    services = ctx.services
    async with transaction(services.session_factory) as uow:
        session = uow.session
        await require_contacts(session, ctx.account_id, [args.contact_id])
        data: dict[str, object] = {
            "contact_id": args.contact_id,
            "priority": args.priority,
            "effect": PRIORITY_EFFECT[args.priority],
        }
        if await _already_applied(session, ctx):
            return ToolOutcome("ok", {**data, "changed": False, "note": "already applied"})
        change = await priority.set_priority(
            session,
            account_id=ctx.account_id,
            contact_id=args.contact_id,
            priority=args.priority,
            actor="agent",
            clock=services.clock,
            details={"run_id": ctx.run_id, "call_id": ctx.call_id},
        )
        if change.audit_id is None:
            return ToolOutcome(
                "ok", {**data, "changed": False, "note": "the contact already had this priority"}
            )
        name = await _name(session, ctx.account_id, args.contact_id)
        text = f"🔧 {name} is now {_LABELS[args.priority]}: {PRIORITY_EFFECT[args.priority]}."
        echo = await queue_echo(ctx, session, change.audit_id, text)
    return ToolOutcome(
        "ok", {**data, "previous_priority": change.previous, "changed": True, "echo": echo.note}
    )


async def describe_priority(
    session: AsyncSession, account_id: int, args: Mapping[str, Any]
) -> str | None:
    """For the confirmation card (owner-facing): whose priority, and what it does."""
    contact_id, value = args.get("contact_id"), args.get("priority")
    if not isinstance(contact_id, int) or value not in PRIORITY_EFFECT:
        return None
    await require_contacts(session, account_id, [contact_id])
    name = await _name(session, account_id, contact_id)
    return f"{name} becomes {_LABELS[str(value)]}: {PRIORITY_EFFECT[str(value)]}"


SET_CONTACT_PRIORITY = ToolDefinition(
    name="set_contact_priority",
    description=(
        "Set how the owner's alerts treat one contact: vip raises their alerts one step, "
        "low lowers them one step (most then wait for the daily digest), normal neither. "
        "The owner sees it with an Undo button."
    ),
    args_model=SetContactPriorityArgs,
    capability=CapabilityClass.CONFIGURE,
    handler=set_contact_priority_tool,
    release_flag=PRIORITY_FLAG,
    describe=describe_priority,
)
