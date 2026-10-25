"""`remember`, `forget`, `list_memories` (ARCHITECTURE §8.2; FR-MEM-01/03/07; S03.3).

Owner instructions, preferences, facts and contact notes in `memory_items`
(`memory/items.py`). Following AGENTS §8:

- strict arguments; constraints checked in validators (M03 D52);
- `remember`/`forget` are `configure`: in a tainted run they are held for a
  confirmation card (the executor's gate); otherwise they execute and are
  echoed to the owner with a one-tap Undo (`agent/tools/echo.py`);
- every id must be this account's, or "not found" (§6 invariant 12);
- `remember` is idempotent on `(run_id, tool_call_id)`;
- results are factual ("ok" only when saved), never content in logs or steps.

The results carry the owner's own words, not managed content: no
`ai_managed` consent is needed, and no contact name is returned (names are
managed; the model finds ids with `resolve_contact`). An item saved from a
run that had other people's text in it (confirmed by the owner on a card) is
shown wrapped as untrusted, without tainting the run that reads it (M03 D70).
"""

from __future__ import annotations

import html
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account
from assistant.agent.models import AgentRun
from assistant.agent.tools.common import OptionalId, check_id, local_time
from assistant.agent.tools.echo import queue_echo
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
)
from assistant.agent.untrusted import Source, wrap
from assistant.conversations.models import Contact
from assistant.db.uow import transaction
from assistant.memory import items
from assistant.memory.items import MemoryView

REMEMBER_FLAG = "tool.remember"
FORGET_FLAG = "tool.forget"
LIST_FLAG = "tool.list_memories"

MemoryKind = Literal["instruction", "preference", "fact", "style"]


def _content(value: str) -> str:
    stripped = value.strip()
    if not stripped or len(stripped) > items.MAX_CONTENT:
        raise ValueError(f"must be 1-{items.MAX_CONTENT} characters")
    return stripped


def _required_id(value: int) -> int:
    check_id(value)
    return value


def _limit(value: int | None) -> int | None:
    if value is not None and not 1 <= value <= 50:
        raise ValueError("must be between 1 and 50")
    return value


# ------------------------------------------------------------ remember
class RememberArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Annotated[str, AfterValidator(_content)] = Field(
        description=(
            "What to remember, in the owner's own words (1-500 characters): drop only "
            "the 'remember that' framing; add nothing the owner did not say."
        )
    )
    kind: MemoryKind = Field(
        description=(
            "instruction (how the assistant should act), preference (what the owner "
            "likes), fact (something true), or style (how the owner writes)."
        )
    )
    contact_id: OptionalId = Field(
        description="A note about one contact (from resolve_contact), or null."
    )
    conversation_id: OptionalId = Field(description="A note about one chat, or null.")
    applies_to_replies: bool | None = Field(
        description="true only for an instruction about how replies to people should be written."
    )
    replaces_memory_id: OptionalId = Field(
        description="When correcting an item: its memory_id (it is forgotten), or null."
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.contact_id is not None and self.conversation_id is not None:
            raise ValueError("give contact_id or conversation_id, not both")
        if self.applies_to_replies and self.kind != "instruction":
            raise ValueError("applies_to_replies is only for instructions")
        return self


async def _run_tainted(session: AsyncSession, ctx: ToolContext) -> bool:
    """Whether the run that asked had other people's text in it — also when
    the call runs later, after the owner confirmed it on a card."""
    if ctx.tainted:
        return True
    if ctx.run_id <= 0:
        return False
    tainted = (
        await session.execute(
            select(AgentRun.tainted)
            .where(AgentRun.account_id == ctx.account_id)
            .where(AgentRun.id == ctx.run_id)
        )
    ).scalar_one_or_none()
    return bool(tainted)


async def _about(session: AsyncSession, account_id: int, item: MemoryView) -> str:
    """Owner-facing (control chat only): who a note is about."""
    if item.scope == "owner":
        return "for you"
    if item.contact_id is not None:
        name = (
            await session.execute(
                select(Contact.display_name)
                .where(Contact.account_id == account_id)
                .where(Contact.id == item.contact_id)
            )
        ).scalar_one_or_none()
        return f"about {html.escape(name)}" if name else "about a contact"
    return "about one chat"


async def remember_tool(ctx: ToolContext, args: RememberArgs) -> ToolOutcome:
    services = ctx.services
    async with transaction(services.session_factory) as uow:
        session = uow.session
        run_id = ctx.run_id if ctx.run_id > 0 else None
        saved = await items.save(
            session,
            account_id=ctx.account_id,
            content=args.content,
            kind=args.kind,
            contact_id=args.contact_id,
            conversation_id=args.conversation_id,
            applies_to_replies=bool(args.applies_to_replies),
            replaces_id=args.replaces_memory_id,
            run_id=run_id,
            tool_call_id=ctx.call_id or None,
            tainted=await _run_tainted(session, ctx),
            actor="agent",
            now=services.clock.now(),
        )
        echo = None
        if saved.audit_id is not None:
            text = (
                f"📝 Saved {await _about(session, ctx.account_id, saved.item)} "
                f"({saved.item.kind}): <i>{html.escape(saved.item.content)}</i>"
            )
            if saved.replaced_id is not None:
                text += "\n(It replaces an earlier note, which is forgotten.)"
            echo = await queue_echo(ctx, session, saved.audit_id, text)
    data: dict[str, object] = {
        "memory_id": saved.item.memory_id,
        "scope": saved.item.scope,
        "kind": saved.item.kind,
        "contact_id": saved.item.contact_id,
        "conversation_id": saved.item.conversation_id,
        "replaced_memory_id": saved.replaced_id,
        "echo": echo.note if echo is not None else "already saved by this same call",
    }
    if saved.repeated:
        data["repeated"] = True
    return ToolOutcome("ok", data)


REMEMBER = ToolDefinition(
    name="remember",
    description=(
        "Save something the owner asked you to remember: an instruction, a "
        "preference, a fact, or a note about one contact or chat. The owner sees it "
        "with an Undo button. To correct an item, pass its memory_id as "
        "replaces_memory_id."
    ),
    args_model=RememberArgs,
    capability=CapabilityClass.CONFIGURE,
    handler=remember_tool,
    release_flag=REMEMBER_FLAG,
)


# ------------------------------------------------------------ forget
class ForgetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: Annotated[int, AfterValidator(_required_id)] = Field(
        description="The memory_id to forget (from list_memories)."
    )


async def forget_tool(ctx: ToolContext, args: ForgetArgs) -> ToolOutcome:
    services = ctx.services
    async with transaction(services.session_factory) as uow:
        session = uow.session
        outcome = await items.forget(
            session,
            account_id=ctx.account_id,
            memory_id=args.memory_id,
            actor="agent",
            now=services.clock.now(),
        )
        echo = None
        if outcome.audit_id is not None:
            text = (
                f"🗑 Forgotten {await _about(session, ctx.account_id, outcome.item)}: "
                f"<i>{html.escape(outcome.item.content)}</i>"
            )
            echo = await queue_echo(ctx, session, outcome.audit_id, text)
    if not outcome.changed:
        return ToolOutcome(
            "unchanged", {"memory_id": args.memory_id, "note": "it was already forgotten"}
        )
    assert echo is not None
    return ToolOutcome("ok", {"memory_id": args.memory_id, "echo": echo.note})


FORGET = ToolDefinition(
    name="forget",
    description=(
        "Forget one saved item by memory_id: it is no longer used at once and is "
        "deleted after a day. The owner sees it with an Undo button."
    ),
    args_model=ForgetArgs,
    capability=CapabilityClass.CONFIGURE,
    handler=forget_tool,
    release_flag=FORGET_FLAG,
)


# ------------------------------------------------------------ list_memories
class ListMemoriesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: OptionalId = Field(
        description="Only notes about this contact (and their chat), or null."
    )
    conversation_id: OptionalId = Field(description="Only notes about this chat, or null.")
    scope: Literal["owner", "contact", "conversation"] | None = Field(
        description="owner: the owner's own instructions, preferences and facts; or null."
    )
    limit: Annotated[int | None, AfterValidator(_limit)] = Field(
        description="How many, 1-50 (default 20), or null."
    )

    @model_validator(mode="after")
    def _one_filter(self) -> Self:
        targets = [x for x in (self.contact_id, self.conversation_id) if x is not None]
        if len(targets) > 1:
            raise ValueError("give contact_id or conversation_id, not both")
        if targets and self.scope is not None:
            raise ValueError("scope cannot be combined with contact_id or conversation_id")
        return self


def memory_view(item: MemoryView, timezone: str) -> dict[str, object]:
    """One item as the model sees it. The owner's words; wrapped as
    untrusted when it was saved from a run with other people's text."""
    content = wrap(item.content, Source.MEMORY_FROM_TAINTED_RUN) if item.tainted else item.content
    return {
        "memory_id": item.memory_id,
        "scope": item.scope,
        "kind": item.kind,
        "contact_id": item.contact_id,
        "conversation_id": item.conversation_id,
        "content": content,
        "applies_to_replies": item.applies_to_replies,
        "saved_at": local_time(item.created_at, timezone),
        "source": item.source,
    }


async def list_memories_tool(ctx: ToolContext, args: ListMemoriesArgs) -> ToolOutcome:
    services = ctx.services
    async with transaction(services.session_factory) as uow:
        account = await uow.session.get(Account, ctx.account_id)
        if account is None:
            raise LookupError("account")
        found, total = await items.list_items(
            uow.session,
            ctx.account_id,
            contact_id=args.contact_id,
            conversation_id=args.conversation_id,
            scope=args.scope,
            limit=args.limit or 20,
        )
    data: dict[str, object] = {
        "items": [memory_view(item, account.timezone) for item in found],
        "count": len(found),
        "total": total,
        "rule": (
            "These are only the notes the owner saved. They are not everything known "
            "about a person: their chat's summary comes from get_conversation."
        ),
    }
    if total > len(found):
        data["note"] = f"{total - len(found)} older item(s) not shown (limit)."
    return ToolOutcome("ok", data)


LIST_MEMORIES = ToolDefinition(
    name="list_memories",
    description=(
        "List what the owner asked you to remember: their instructions, preferences "
        "and facts, and notes about contacts or chats, newest first, with memory_id."
    ),
    args_model=ListMemoriesArgs,
    capability=CapabilityClass.READ,
    handler=list_memories_tool,
    release_flag=LIST_FLAG,
)
