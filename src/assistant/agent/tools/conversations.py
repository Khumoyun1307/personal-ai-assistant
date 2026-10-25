"""`list_conversations`, `get_conversation`, `resolve_contact` (ARCHITECTURE
§8.2; S03.1).

Read-only views of the owner's managed chats over `conversations/read.py`.
Each result carries contact-authored text or contact-chosen names, so:
offered only with `ai_managed` consent (`returns_managed`), every such string
wrapped as untrusted, the run tainted by the result (invariant 3b), and the
ids it drew on returned as `refs` for the reply turn. Every id an argument
names must belong to the run's account; otherwise "not found", the same as
an id that does not exist (§6 invariant 12).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account
from assistant.agent.tools.common import (
    COVERAGE_RULE,
    DIRECTION_LABELS,
    OptionalId,
    decode_cursor,
    encode_cursor,
    local_time,
    message_text,
    name,
    one,
    require_contacts,
    summary_view,
)
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRefs,
)
from assistant.conversations import read
from assistant.conversations.models import Conversation
from assistant.db.uow import transaction
from assistant.memory import summaries

LIST_FLAG = "tool.list_conversations"
GET_FLAG = "tool.get_conversation"
RESOLVE_FLAG = "tool.resolve_contact"
MESSAGE_LIMIT = 1500


def _bounded(low: int, high: int) -> AfterValidator:
    def check(value: int | None) -> int | None:
        if value is not None and not low <= value <= high:
            raise ValueError(f"must be between {low} and {high}")
        return value

    return AfterValidator(check)


def _text(max_length: int) -> AfterValidator:
    def check(value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped or len(stripped) > max_length:
            raise ValueError(f"must be 1-{max_length} characters")
        return stripped

    return AfterValidator(check)


def _cursor(value: str | None) -> str | None:
    decode_cursor(value)  # strict: only a next_cursor returned earlier
    return value


def _row(row: read.ConversationRow, timezone: str) -> dict[str, object]:
    return {
        "conversation_id": row.conversation_id,
        "contact_id": row.contact_id,
        "contact_name": name(row.display_name),
        "contact_username": name(row.username),
        "mode": row.mode,
        "last_activity": local_time(row.last_activity_at, timezone),
        "last_contact_message": local_time(row.last_contact_msg_at, timezone),
        "last_owner_message": local_time(row.last_owner_msg_at, timezone),
        "coverage_started_at": local_time(row.coverage_started_at, timezone),
        "stored_messages": row.retained_messages,
    }


async def _account(ctx: ToolContext, session: AsyncSession) -> Account:
    account = await session.get(Account, ctx.account_id)
    if account is None:
        raise LookupError("account")
    return account


# ------------------------------------------------------------ list_conversations
class ListConversationsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_within_days: Annotated[int | None, _bounded(1, 365)] = Field(
        description="Only chats with activity in the last N days (1-365), or null."
    )
    mode: Literal["observe", "assist", "ignore"] | None = Field(
        description="Only chats in this mode, or null."
    )
    contact_id: OptionalId = Field(description="Only this contact's chat, or null.")
    awaiting_owner_reply: bool | None = Field(
        description="true: only chats where the contact wrote last (owner has not replied)."
    )
    limit: Annotated[int | None, _bounded(1, read.MAX_LIST)] = Field(
        description="How many, 1-50 (default 20), or null."
    )


async def list_conversations_tool(ctx: ToolContext, args: ListConversationsArgs) -> ToolOutcome:
    services = ctx.services
    async with transaction(services.session_factory) as uow:
        account = await _account(ctx, uow.session)
        await require_contacts(uow.session, ctx.account_id, one(args.contact_id))
        since = (
            services.clock.now() - timedelta(days=args.active_within_days)
            if args.active_within_days is not None
            else None
        )
        rows = await read.list_conversations(
            uow.session,
            ctx.account_id,
            active_since=since,
            modes=(args.mode,) if args.mode is not None else (),
            contact_ids=one(args.contact_id),
            awaiting_owner_reply=bool(args.awaiting_owner_reply),
            limit=args.limit or 20,
        )
    return ToolOutcome(
        "ok",
        {
            "conversations": [_row(row, account.timezone) for row in rows],
            "count": len(rows),
            "timezone": account.timezone,
            "coverage_rule": COVERAGE_RULE,
        },
        contains_managed=True,
        refs=ToolRefs(
            conversation_ids=frozenset(r.conversation_id for r in rows),
            contact_ids=frozenset(r.contact_id for r in rows),
        ),
    )


LIST_CONVERSATIONS = ToolDefinition(
    name="list_conversations",
    description=(
        "List the owner's chats with other people that the assistant observes, most "
        "recent first: contact, mode, last activity, stored message count. Filters: "
        "recent days, mode, one contact, or only chats awaiting the owner's reply."
    ),
    args_model=ListConversationsArgs,
    capability=CapabilityClass.READ,
    handler=list_conversations_tool,
    release_flag=LIST_FLAG,
    returns_managed=True,
)


# ------------------------------------------------------------ get_conversation
class GetConversationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: OptionalId = Field(description="The conversation, or null to use contact_id.")
    contact_id: OptionalId = Field(description="The contact whose chat to read, or null.")
    cursor: Annotated[str | None, AfterValidator(_cursor)] = Field(
        description="For older messages: next_cursor from the previous page, or null."
    )
    limit: Annotated[int | None, _bounded(1, read.MAX_PAGE)] = Field(
        description="How many messages, 1-50 (default 30), or null."
    )

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        if (self.conversation_id is None) == (self.contact_id is None):
            raise ValueError("give exactly one of conversation_id or contact_id")
        return self


async def get_conversation_tool(ctx: ToolContext, args: GetConversationArgs) -> ToolOutcome:
    services = ctx.services
    async with transaction(services.session_factory) as uow:
        session = uow.session
        account = await _account(ctx, session)
        conversation_id = args.conversation_id
        if conversation_id is None:
            assert args.contact_id is not None
            await require_contacts(session, ctx.account_id, [args.contact_id])
            rows = await read.list_conversations(
                session, ctx.account_id, contact_ids=(args.contact_id,), limit=1
            )
            if not rows:
                raise LookupError("no conversation with this contact")
            conversation_id = rows[0].conversation_id
        page = await read.conversation_page(
            session,
            ctx.account_id,
            conversation_id,
            before=decode_cursor(args.cursor),
            limit=args.limit or 30,
        )
        stored = await summaries.rolling_summary(session, ctx.account_id, conversation_id)
        unseen = (
            await session.execute(
                select(Conversation.pending_ai_since)
                .where(Conversation.account_id == ctx.account_id)
                .where(Conversation.id == conversation_id)
            )
        ).scalar_one_or_none() is not None
    tz = account.timezone
    cov = page.coverage
    summary: dict[str, object] | None = None
    if stored is not None:
        # The chat's running summary (S03.2): model output, wrapped as
        # untrusted like the messages it was written from.
        summary = {
            **summary_view(stored.summary),
            "updated_at": local_time(stored.updated_at, tz),
            "up_to_date": not unseen,
        }
    return ToolOutcome(
        "ok",
        {
            "conversation": _row(page.conversation, tz),
            "messages": [
                {
                    "message_id": m.message_id,
                    "from": DIRECTION_LABELS.get(m.direction, m.direction),
                    "sent_at": local_time(m.sent_at, tz),
                    "edited": m.edited,
                    "type": m.content_type,
                    "media": m.media_kind,
                    "text": message_text(m.text, m.direction, limit=MESSAGE_LIMIT),
                }
                for m in page.messages
            ],
            "next_cursor": encode_cursor(page.next_cursor),
            "summary": summary,
            "timezone": tz,
            "coverage": {
                "rule": COVERAGE_RULE,
                "coverage_started_at": local_time(cov.coverage_started_at, tz),
                "older_messages_expired_before": local_time(cov.expired_before, tz),
                "deleted_messages": cov.deleted_messages,
                "expired_messages": cov.expired_messages,
                "unresolved_gaps_in_this_chat": cov.unresolved_gaps,
                "unresolved_account_wide_gaps": cov.account_gaps,
            },
        },
        contains_managed=True,
        refs=ToolRefs(
            conversation_ids=frozenset({page.conversation.conversation_id}),
            contact_ids=frozenset({page.conversation.contact_id}),
        ),
    )


GET_CONVERSATION = ToolDefinition(
    name="get_conversation",
    description=(
        "Read the stored messages of one chat with another person, newest page first "
        "(each page oldest to newest), by conversation_id or contact_id, with the chat's "
        "running summary when there is one. Use next_cursor as cursor for older "
        "messages. Includes what the record cannot contain (coverage start, "
        "deleted/expired messages, gaps)."
    ),
    args_model=GetConversationArgs,
    capability=CapabilityClass.READ,
    handler=get_conversation_tool,
    release_flag=GET_FLAG,
    returns_managed=True,
)


# ------------------------------------------------------------ resolve_contact
class ResolveContactArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, _text(100)] = Field(
        description="A name or @username as the owner wrote it, in any script."
    )
    limit: Annotated[int | None, _bounded(1, 10)] = Field(
        description="How many candidates, 1-10 (default 5), or null."
    )


async def resolve_contact_tool(ctx: ToolContext, args: ResolveContactArgs) -> ToolOutcome:
    services = ctx.services
    async with transaction(services.session_factory) as uow:
        account = await _account(ctx, uow.session)
        directory = await read.contact_directory(uow.session, ctx.account_id)
    matches = read.match_contacts(directory, args.name, limit=args.limit or 5)
    candidates = [
        {
            "contact_id": m.entry.contact_id,
            "conversation_id": m.entry.conversation_id,
            "name": name(m.entry.display_name),
            "username": name(m.entry.username),
            "match": m.kind,
            "last_activity": local_time(m.entry.last_activity_at, account.timezone),
        }
        for m in matches
    ]
    data: dict[str, object] = {"candidates": candidates, "count": len(candidates)}
    if len(candidates) > 1 and matches[0].kind == matches[1].kind:
        data["note"] = "Several contacts match equally: ask the owner which one they mean."
    if not candidates:
        data["note"] = "No stored contact matches that name."
    return ToolOutcome(
        "ok",
        data,
        contains_managed=True,
        refs=ToolRefs(
            conversation_ids=frozenset(
                m.entry.conversation_id for m in matches if m.entry.conversation_id is not None
            ),
            contact_ids=frozenset(m.entry.contact_id for m in matches),
        ),
    )


RESOLVE_CONTACT = ToolDefinition(
    name="resolve_contact",
    description=(
        "Find which of the owner's contacts a name refers to (any script, typos "
        "allowed). Returns ranked candidates with contact_id and conversation_id; when "
        "several match equally, ask the owner instead of guessing."
    ),
    args_model=ResolveContactArgs,
    capability=CapabilityClass.READ,
    handler=resolve_contact_tool,
    release_flag=RESOLVE_FLAG,
    returns_managed=True,
)
