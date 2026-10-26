"""`delete_conversation_data`, `delete_contact_data`, `delete_everything`
(ARCHITECTURE §8.2; FR-PRV-04; S03.5).

Class `configure`, and **always a confirmation card** (`always_confirm`):
never executed from a model call, tainted or not. The card says, in our own
words, whose data goes and that it cannot be undone (`describe`, which also
refuses an id that is not this account's before any card is shown). Only
the confirmation applier runs the handler, after the owner's tap; the
handler checks that too (`ToolContext.held`), so a misconfigured gate still
deletes nothing.

The domain cascades are `privacy/deletion.py`. Results are counts and ids,
never names or text (no `ai_managed` needed). A repeated call (the same run
and call id) deletes once: its audit row is found and reported.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.agent.tools.common import OptionalId, check_id, require_contacts
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
)
from assistant.conversations import modes
from assistant.conversations.models import Contact, Conversation, Message
from assistant.db.uow import transaction
from assistant.privacy import deletion
from assistant.privacy.deletion import DeletionReport, DeletionRequest

CONVERSATION_FLAG = "tool.delete_conversation_data"
CONTACT_FLAG = "tool.delete_contact_data"
EVERYTHING_FLAG = "tool.delete_everything"

NOT_CONFIRMED = ToolOutcome(
    "not_executed", {"reason": "deleting data needs the owner's tap on a confirmation card"}
)
CANNOT_UNDO = "This cannot be undone."


def _required_id(value: int) -> int:
    check_id(value)
    return value


# ------------------------------------------------------------ arguments
class DeleteConversationDataArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: OptionalId = Field(description="The chat, or null to use contact_id.")
    contact_id: OptionalId = Field(description="The contact whose chat it is, or null.")

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        if (self.conversation_id is None) == (self.contact_id is None):
            raise ValueError("give exactly one of conversation_id or contact_id")
        return self


class DeleteContactDataArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: Annotated[int, AfterValidator(_required_id)] = Field(
        description="The contact (from resolve_contact)."
    )


class DeleteEverythingArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["everything"] = Field(
        description="Must be 'everything': all stored content of the owner's account."
    )


# ------------------------------------------------------------ shared
async def _chat_of(session: AsyncSession, account_id: int, args: Mapping[str, Any]) -> int:
    conversation_id = args.get("conversation_id")
    contact_id = args.get("contact_id")
    if isinstance(contact_id, int):
        await require_contacts(session, account_id, [contact_id])
        return await modes.conversation_for_contact(session, account_id, contact_id)
    if not isinstance(conversation_id, int):
        raise LookupError("no chat given")
    found = (
        await session.execute(
            select(Conversation.id)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    if found is None:
        raise LookupError("conversation not found in this account")
    return int(found)


async def _name(session: AsyncSession, account_id: int, contact_id: int) -> str:
    """Owner-facing (the card in the control chat): the contact's name."""
    row = (
        await session.execute(
            select(Contact.display_name, Contact.username)
            .where(Contact.account_id == account_id)
            .where(Contact.id == contact_id)
        )
    ).one_or_none()
    if row is None:
        raise LookupError("contact not found in this account")
    name, username = row
    return name or (f"@{username}" if username else "a contact without a stored name")


async def _message_count(session: AsyncSession, account_id: int, condition: Any) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.account_id == account_id)
                .where(condition)
            )
        ).scalar_one()
    )


def _result(scope: str, report: DeletionReport, **ids: int) -> ToolOutcome:
    return ToolOutcome(
        "ok",
        {
            "scope": scope,
            **ids,
            "deleted": report.counts(),
            "note": "deleted for good; new messages are stored as before (chat modes unchanged)",
        },
    )


Deleter = Callable[[AsyncSession, DeletionRequest | None], Awaitable[DeletionReport]]


async def _run(ctx: ToolContext, scope: str, delete: Deleter, **ids: int) -> ToolOutcome:
    """One deletion, once per (run, call) — only after the owner's tap."""
    if not ctx.held:
        return NOT_CONFIRMED
    request = (
        DeletionRequest.tool_call(ctx.run_id, ctx.call_id)
        if ctx.run_id > 0 and ctx.call_id
        else None
    )
    async with transaction(ctx.services.session_factory) as uow:
        if request is not None:
            done = await deletion.find_deletion(uow.session, ctx.account_id, request)
            if done is not None:
                counts = {k: v for k, v in done.details.items() if isinstance(v, int)}
                counts.pop("run_id", None)
                return ToolOutcome(
                    "ok", {"scope": scope, **ids, "deleted": counts, "repeated": True}
                )
        report = await delete(uow.session, request)
    return _result(scope, report, **ids)


# ------------------------------------------------------------ the tools
async def delete_conversation_data_tool(
    ctx: ToolContext, args: DeleteConversationDataArgs
) -> ToolOutcome:
    if not ctx.held:
        return NOT_CONFIRMED
    async with transaction(ctx.services.session_factory) as uow:
        conversation_id = await _chat_of(uow.session, ctx.account_id, args.model_dump())

    async def delete(session: AsyncSession, request: DeletionRequest | None) -> DeletionReport:
        return await deletion.delete_conversation_data(
            session,
            account_id=ctx.account_id,
            conversation_id=conversation_id,
            actor="agent",
            clock=ctx.services.clock,
            request=request,
        )

    return await _run(ctx, deletion.SCOPE_CONVERSATION, delete, conversation_id=conversation_id)


async def describe_conversation_deletion(
    session: AsyncSession, account_id: int, args: Mapping[str, Any]
) -> str:
    conversation_id = await _chat_of(session, account_id, args)
    contact_id = (
        await session.execute(
            select(Conversation.contact_id).where(Conversation.id == conversation_id)
        )
    ).scalar_one()
    name = await _name(session, account_id, int(contact_id))
    count = await _message_count(session, account_id, Message.conversation_id == conversation_id)
    return (
        f"Delete everything stored from the chat with {name}: {count} message(s), its "
        "summary, raw updates, notes about this chat and my answers that drew on it. "
        f"Your notes about {name} stay. The chat keeps its mode. {CANNOT_UNDO}"
    )


async def delete_contact_data_tool(ctx: ToolContext, args: DeleteContactDataArgs) -> ToolOutcome:
    async def delete(session: AsyncSession, request: DeletionRequest | None) -> DeletionReport:
        return await deletion.delete_contact_data(
            session,
            account_id=ctx.account_id,
            contact_id=args.contact_id,
            actor="agent",
            clock=ctx.services.clock,
            request=request,
        )

    return await _run(ctx, deletion.SCOPE_CONTACT, delete, contact_id=args.contact_id)


async def describe_contact_deletion(
    session: AsyncSession, account_id: int, args: Mapping[str, Any]
) -> str:
    contact_id = args.get("contact_id")
    if not isinstance(contact_id, int):
        raise LookupError("no contact given")
    name = await _name(session, account_id, contact_id)
    chats = select(Conversation.id).where(
        Conversation.account_id == account_id, Conversation.contact_id == contact_id
    )
    count = await _message_count(session, account_id, Message.conversation_id.in_(chats))
    return (
        f"Delete everything stored about {name}: {count} message(s) from their chat, its "
        f"summary, raw updates, your notes about {name} and their chat, and my answers "
        f"that drew on them. Their chat keeps its mode. {CANNOT_UNDO}"
    )


async def delete_everything_tool(ctx: ToolContext, args: DeleteEverythingArgs) -> ToolOutcome:
    async def delete(session: AsyncSession, request: DeletionRequest | None) -> DeletionReport:
        return await deletion.delete_everything(
            session,
            account_id=ctx.account_id,
            actor="agent",
            clock=ctx.services.clock,
            request=request,
        )

    return await _run(ctx, deletion.SCOPE_EVERYTHING, delete)


EVERYTHING_TEXT = (
    "Delete ALL stored content: every message, name, summary and note (yours too), "
    "the history of this chat and the raw updates. Kept: your account, settings, "
    "consents and the connection, so I keep observing your chats from now on. "
    f"{CANNOT_UNDO}"
)


async def describe_everything(
    session: AsyncSession, account_id: int, args: Mapping[str, Any]
) -> str:
    return EVERYTHING_TEXT


DELETE_CONVERSATION_DATA = ToolDefinition(
    name="delete_conversation_data",
    description=(
        "Delete everything stored from one chat (messages, summary, raw updates, notes "
        "about the chat). Always shown to the owner as a confirmation card first; "
        "nothing is deleted until they tap Confirm."
    ),
    args_model=DeleteConversationDataArgs,
    capability=CapabilityClass.CONFIGURE,
    handler=delete_conversation_data_tool,
    release_flag=CONVERSATION_FLAG,
    always_confirm=True,
    describe=describe_conversation_deletion,
)

DELETE_CONTACT_DATA = ToolDefinition(
    name="delete_contact_data",
    description=(
        "Delete everything stored about one contact: their chat's data and the owner's "
        "notes about them. Always a confirmation card first."
    ),
    args_model=DeleteContactDataArgs,
    capability=CapabilityClass.CONFIGURE,
    handler=delete_contact_data_tool,
    release_flag=CONTACT_FLAG,
    always_confirm=True,
    describe=describe_contact_deletion,
)

DELETE_EVERYTHING = ToolDefinition(
    name="delete_everything",
    description=(
        "Delete all stored content of the owner's account (messages, names, summaries, "
        "notes, this chat's history). Settings, consents and the connection stay. "
        "Always a confirmation card first."
    ),
    args_model=DeleteEverythingArgs,
    capability=CapabilityClass.CONFIGURE,
    handler=delete_everything_tool,
    release_flag=EVERYTHING_FLAG,
    always_confirm=True,
    describe=describe_everything,
)
