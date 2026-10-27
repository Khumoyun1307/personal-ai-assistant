"""`remember`, `forget`, `list_memories` (ARCHITECTURE §8.2; FR-MEM-01/03/07; S03.3).

Owner instructions, preferences, facts and contact notes in `memory_items`
(`memory/items.py`). Following AGENTS §8:

- strict arguments; constraints checked in validators (M03 D52), and a
  broken rule is told to the model in our fixed words (`ArgumentRuleError`);
- the ids a model copies from `resolve_contact` are accepted: a
  `contact_id` with that contact's `conversation_id` means the contact
  (whose notes include their chat's), in `list_memories` also with a scope
  matching either id; normalised in the validator before the confirmation
  gate, so a card shows what is applied (M03 fix round C2);
- `remember`/`forget` are `configure`: in a tainted run they are held for a
  confirmation card (the executor's gate); otherwise they execute and are
  echoed to the owner with a one-tap Undo (`agent/tools/echo.py`);
- every id must be this account's, or "not found" (`NotInAccountError`, §6
  invariant 12);
- `remember` is idempotent on `(run_id, tool_call_id)`;
- results are factual ("ok" only when saved), never content in logs or steps.

No contact name is returned (names are managed; the model finds ids with
`resolve_contact`). An item saved by a run that had other people's text
anywhere in its context (`run_origin`) is stored tainted and managed (M03
D70, as revised by the safety review): it reaches a model only through
`list_memories`, wrapped as untrusted; a result holding one taints the run
(invariant 3b) and is marked managed, so without `ai_managed` it is withheld
and the consent wrapper refuses it anyway (invariant 9).
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, ControlTurn
from assistant.agent.context import OWNER_HISTORY_TURNS
from assistant.agent.models import AgentRun
from assistant.agent.tools.common import ArgumentRuleError, OptionalId, check_id, local_time
from assistant.agent.tools.echo import queue_echo
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRefs,
)
from assistant.agent.untrusted import Source, wrap
from assistant.conversations.models import Contact, Conversation
from assistant.core.errors import NotInAccountError
from assistant.db.uow import transaction
from assistant.memory import items
from assistant.memory.items import MemoryView

REMEMBER_FLAG = "tool.remember"
FORGET_FLAG = "tool.forget"
LIST_FLAG = "tool.list_memories"

MemoryKind = Literal["instruction", "preference", "fact", "style"]

#: Our rules, in the fixed words the model is shown (§8.3).
RULE_APPLIES_TO_REPLIES = (
    "applies_to_replies is only for kind instruction; set it to null for other kinds"
)
RULE_SCOPE_WITH_ID = (
    "with contact_id or conversation_id, set scope to null (or to the matching "
    "'contact' or 'conversation'): the id already chooses the notes"
)


def _content(value: str) -> str:
    stripped = value.strip()
    if not stripped or len(stripped) > items.MAX_CONTENT:
        raise ArgumentRuleError(f"must be 1-{items.MAX_CONTENT} characters")
    return stripped


def _required_id(value: int) -> int:
    check_id(value)
    return value


def _limit(value: int | None) -> int | None:
    if value is not None and not 1 <= value <= 50:
        raise ArgumentRuleError("must be between 1 and 50")
    return value


# ------------------------------------------------------------ remember
class RememberArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Annotated[str, AfterValidator(_content)] = Field(
        description=(
            "What to remember, in the owner's own words (1-400 characters): drop only "
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
    conversation_id: OptionalId = Field(
        description=(
            "A note about one chat, or null (null when contact_id is given: a note "
            "about a contact covers their chat)."
        )
    )
    applies_to_replies: bool | None = Field(
        description="true only for an instruction about how replies to people should be written."
    )
    replaces_memory_id: OptionalId = Field(
        description="When correcting an item: its memory_id (it is forgotten), or null."
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.contact_id is not None and self.conversation_id is not None:
            # `resolve_contact` returns both and a model copies both (C2): a
            # note about the contact, whose notes include their chat's. Here,
            # pure and before the confirmation gate, so a card shows exactly
            # what is applied; the contact id is still checked when used.
            self.conversation_id = None
        if self.applies_to_replies and self.kind != "instruction":
            raise ArgumentRuleError(RULE_APPLIES_TO_REPLIES)
        return self


@dataclass(frozen=True, slots=True)
class Origin:
    """Where a note's words may come from (safety review V1, V2, S3)."""

    #: The run saw other people's text — taint, or the untrusted context a
    #: tainted-run reply or a managed turn brings even when D56 does not taint.
    tainted: bool
    refs: ToolRefs


def _turn_untrusted(turn: ControlTurn) -> bool:
    return bool(
        turn.third_party
        or turn.from_tainted_run
        or turn.contains_managed
        or turn.quoted_text is not None
    )


async def run_origin(session: AsyncSession, ctx: ToolContext) -> Origin:
    """Whether the run that asks to save had other people's text anywhere in
    its context, and which chats and contacts it drew on. Fail closed: a held
    call the owner confirmed always counts (N1), and so does a run whose row
    is gone."""
    refs = ctx.refs
    if ctx.run_id <= 0:
        return Origin(ctx.tainted or ctx.held, refs)
    run = (
        await session.execute(
            select(AgentRun)
            .where(AgentRun.account_id == ctx.account_id)
            .where(AgentRun.id == ctx.run_id)
        )
    ).scalar_one_or_none()
    if run is None:
        return Origin(True, refs)
    tainted = ctx.tainted or ctx.held or run.tainted
    # The turns this run's context was built from: the trigger and the
    # history window before it (the same window as the context builder).
    turn_id = _trigger_turn(run.trigger_ref)
    if turn_id is not None:
        turns = (
            (
                await session.execute(
                    select(ControlTurn)
                    .where(ControlTurn.account_id == ctx.account_id)
                    .where(ControlTurn.id <= turn_id)
                    .order_by(ControlTurn.id.desc())
                    .limit(OWNER_HISTORY_TURNS + 1)
                )
            )
            .scalars()
            .all()
        )
        for turn in turns:
            if _turn_untrusted(turn):
                tainted = True
                refs = refs | ToolRefs.from_json(turn.refs)
    # The run's own reply (a held call applied after the run finished)
    # records the chats its tool results drew on.
    replies = (
        await session.execute(
            select(ControlTurn.refs)
            .where(ControlTurn.account_id == ctx.account_id)
            .where(ControlTurn.run_id == ctx.run_id)
        )
    ).scalars()
    for reply_refs in replies:
        refs = refs | ToolRefs.from_json(reply_refs)
    return Origin(tainted, refs)


def _trigger_turn(trigger_ref: str | None) -> int | None:
    if trigger_ref is None or not trigger_ref.startswith("turn:"):
        return None
    value = trigger_ref.removeprefix("turn:")
    return int(value) if value.isdigit() else None


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
        origin = await run_origin(session, ctx)
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
            # Every item from other people's text is treated as managed too
            # (the conservative rule; safety review V2, M03 D70).
            tainted=origin.tainted,
            managed=origin.tainted,
            refs=origin.refs.as_json() if origin.tainted else None,
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


async def _contact_name(session: AsyncSession, account_id: int, contact_id: int) -> str:
    name = (
        await session.execute(
            select(Contact.display_name)
            .where(Contact.account_id == account_id)
            .where(Contact.id == contact_id)
        )
    ).one_or_none()
    if name is None:
        raise NotInAccountError("contact not found in this account")
    return name[0] or "(a contact without a stored name)"


async def _note(session: AsyncSession, account_id: int, memory_id: int) -> str:
    item = await items.get(session, account_id, memory_id)
    if item is None:
        raise NotInAccountError("memory item not found in this account")
    return f'"{item.content}"'


async def describe_remember(
    session: AsyncSession, account_id: int, args: Mapping[str, Any]
) -> str | None:
    """For the confirmation card (owner-facing): who the note is about and
    the note it replaces (safety review S1)."""
    parts: list[str] = []
    if isinstance(args.get("contact_id"), int):
        parts.append(f"a note about {await _contact_name(session, account_id, args['contact_id'])}")
    if isinstance(args.get("conversation_id"), int):
        contact = await contact_of_chat(session, account_id, args["conversation_id"])
        parts.append(f"a note about the chat with {contact}")
    if isinstance(args.get("replaces_memory_id"), int):
        parts.append(
            f"it replaces the note {await _note(session, account_id, args['replaces_memory_id'])}"
        )
    return "; ".join(parts) or None


async def describe_forget(
    session: AsyncSession, account_id: int, args: Mapping[str, Any]
) -> str | None:
    """For the confirmation card: the words of the note to forget (S1)."""
    memory_id = args.get("memory_id")
    if not isinstance(memory_id, int):
        return None
    return f"the note {await _note(session, account_id, memory_id)}"


async def contact_of_chat(session: AsyncSession, account_id: int, conversation_id: int) -> str:
    """The contact name of one of the account's chats (owner-facing)."""
    row = (
        await session.execute(
            select(Contact.display_name)
            .join(Conversation, Conversation.contact_id == Contact.id)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id == conversation_id)
        )
    ).one_or_none()
    if row is None:
        raise NotInAccountError("conversation not found in this account")
    return row[0] or "(a contact without a stored name)"


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
    describe=describe_remember,
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
        # Already so: the owner's wish holds; nothing changed (review item 3).
        return ToolOutcome(
            "ok",
            {"memory_id": args.memory_id, "changed": False, "note": "it was already forgotten"},
        )
    assert echo is not None
    return ToolOutcome("ok", {"memory_id": args.memory_id, "changed": True, "echo": echo.note})


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
    describe=describe_forget,
)


# ------------------------------------------------------------ list_memories
class ListMemoriesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: OptionalId = Field(
        description="Only notes about this contact (and their chat), or null."
    )
    conversation_id: OptionalId = Field(
        description=(
            "Only notes about this chat, or null (null when contact_id is given: the "
            "contact's notes include their chat's)."
        )
    )
    scope: Literal["owner", "contact", "conversation"] | None = Field(
        description=(
            "owner: the owner's own instructions, preferences and facts; contact: notes "
            "about contacts; conversation: notes about one chat; or null for all. With "
            "contact_id or conversation_id, null: the id already chooses the notes."
        )
    )
    limit: Annotated[int | None, AfterValidator(_limit)] = Field(
        description="How many, 1-50 (default 20), or null."
    )

    @model_validator(mode="after")
    def _one_filter(self) -> Self:
        """One filter (C2). Both ids, as `resolve_contact` returns them: the
        contact filter, which already includes their chat's notes — never
        both filters, whose AND would hide the contact's own notes. A scope
        that matches an id the model sent (with both ids, either one) is
        dropped, so the id alone chooses the notes (a contact's include their
        chat's); any other scope with an id is a contradiction."""
        given = (("contact", self.contact_id), ("conversation", self.conversation_id))
        matching = {scope for scope, value in given if value is not None}
        if not matching:
            return self
        if self.scope is not None and self.scope not in matching:
            raise ArgumentRuleError(RULE_SCOPE_WITH_ID)
        if self.contact_id is not None:
            self.conversation_id = None
        self.scope = None
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
            raise NotInAccountError("account")
        # Items that may hold managed-chat content reach a model only with
        # `ai_managed` naming this run's processor — the runtime's own check
        # (verification item 3), so a consent for another processor
        # withholds them instead of failing the whole run in the wrapper.
        managed_ok = ctx.managed_ok
        found, total = await items.list_items(
            uow.session,
            ctx.account_id,
            contact_id=args.contact_id,
            conversation_id=args.conversation_id,
            scope=args.scope,
            limit=args.limit or 20,
            include_managed=managed_ok,
        )
        withheld = 0
        if not managed_ok:
            _, everything = await items.list_items(
                uow.session,
                ctx.account_id,
                contact_id=args.contact_id,
                conversation_id=args.conversation_id,
                scope=args.scope,
                limit=1,
            )
            withheld = everything - total
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
    if withheld:
        data["withheld"] = withheld
        data["withheld_note"] = (
            f"{withheld} item(s) saved from the owner's chats with other people are withheld: "
            "AI for those chats is off (/privacy managed)."
        )
    return ToolOutcome(
        "ok",
        data,
        contains_managed=any(item.managed for item in found),
        # Text that may be someone else's taints the run: a configure call
        # after it needs the owner's tap (invariant 3b, safety review V1).
        untrusted=any(item.tainted for item in found),
        # The chats and contacts the shown items are about or came from: the
        # reply and anything this run saves keep them, so deleting one of
        # those chats finds them (verification item 1).
        refs=ToolRefs(
            conversation_ids=frozenset(c for item in found for c in item.conversation_refs),
            contact_ids=frozenset(c for item in found for c in item.contact_refs),
        ),
    )


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
