"""`search_messages` — multilingual search over retained managed messages
(ARCHITECTURE §8.2, §11.2; S03.1).

Read-only. Its results carry contact-authored text and names, so the tool
is offered only with `ai_managed` consent (`returns_managed`), every string
is wrapped as untrusted, and a result taints the run (invariant 3b). The
model is asked to send the question in several spellings (Latin, Cyrillic,
Russian, English); the service searches each and merges by best score.
"""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from assistant.accounts.models import Account
from assistant.agent.tools.common import (
    DIRECTION_LABELS,
    OptionalId,
    account_coverage,
    day_range,
    local_time,
    message_text,
    name,
    one,
    parse_local_date,
    require_contacts,
    require_conversations,
)
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRefs,
)
from assistant.conversations.models import Contact
from assistant.db.uow import transaction
from assistant.memory.retrieval import MAX_LIMIT, MAX_QUERIES, SearchFilters, search_messages

RELEASE_FLAG = "tool.search_messages"
SNIPPET_LIMIT = 400


def _queries(value: list[str]) -> list[str]:
    cleaned = [query.strip() for query in value if query.strip()]
    if not cleaned:
        raise ValueError("give at least one non-empty query")
    if len(cleaned) > MAX_QUERIES:
        raise ValueError(f"at most {MAX_QUERIES} query variants")
    if any(len(query) > 200 for query in cleaned):
        raise ValueError("a query is at most 200 characters")
    return cleaned


def _limit(value: int | None) -> int | None:
    if value is not None and not 1 <= value <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    return value


def _date(value: str | None) -> str | None:
    parse_local_date(value)
    return value


class SearchMessagesArgs(BaseModel):
    """Strict: unknown keys are refused; constraints are checked here, not in
    the JSON schema (see `agent/tools/common.py`)."""

    model_config = ConfigDict(extra="forbid")

    queries: Annotated[list[str], AfterValidator(_queries)] = Field(
        description=(
            "The question as 1-6 short search phrases: the same words in Uzbek Latin, "
            "Uzbek Cyrillic, Russian and English where they differ."
        )
    )
    contact_id: OptionalId = Field(
        description="Only messages with this contact (from resolve_contact), or null."
    )
    conversation_id: OptionalId = Field(description="Only this conversation, or null.")
    since: Annotated[str | None, AfterValidator(_date)] = Field(
        description="First day to include, YYYY-MM-DD in the owner's timezone, or null."
    )
    until: Annotated[str | None, AfterValidator(_date)] = Field(
        description="Last day to include, YYYY-MM-DD in the owner's timezone, or null."
    )
    limit: Annotated[int | None, AfterValidator(_limit)] = Field(
        description="How many results, 1-25 (default 10), or null."
    )

    @model_validator(mode="after")
    def _range(self) -> Self:
        since, until = parse_local_date(self.since), parse_local_date(self.until)
        if since and until and since > until:
            raise ValueError("since is after until")
        return self


async def search_messages_tool(ctx: ToolContext, args: SearchMessagesArgs) -> ToolOutcome:
    services = ctx.services
    async with transaction(services.session_factory) as uow:
        session = uow.session
        account = await session.get(Account, ctx.account_id)
        if account is None:
            raise LookupError("account")
        await require_contacts(session, ctx.account_id, one(args.contact_id))
        await require_conversations(session, ctx.account_id, one(args.conversation_id))
        start, end = day_range(
            parse_local_date(args.since), parse_local_date(args.until), account.timezone
        )
        result = await search_messages(
            session,
            ctx.account_id,
            args.queries,
            now=services.clock.now(),
            filters=SearchFilters(
                contact_ids=one(args.contact_id),
                conversation_ids=one(args.conversation_id),
                since=start,
                until=end,
            ),
            limit=args.limit or 10,
        )
        contact_ids = sorted({hit.contact_id for hit in result.hits})
        names: dict[int, str | None] = {
            int(contact_id): display_name
            for contact_id, display_name in (
                await session.execute(
                    select(Contact.id, Contact.display_name)
                    .where(Contact.account_id == ctx.account_id)
                    .where(Contact.id.in_(contact_ids))
                )
            ).all()
        }
        coverage = await account_coverage(session, ctx.account_id, account.timezone)
    results = [
        {
            "message_id": hit.message_id,
            "conversation_id": hit.conversation_id,
            "contact_id": hit.contact_id,
            "contact_name": name(names.get(hit.contact_id)),
            "from": DIRECTION_LABELS.get(hit.direction, hit.direction),
            "sent_at": local_time(hit.sent_at, account.timezone),
            "text": message_text(hit.text, hit.direction, limit=SNIPPET_LIMIT),
        }
        for hit in result.hits
    ]
    data = {
        "results": results,
        "result_count": len(results),
        "matched_any_word_only": result.relaxed,
        "timezone": account.timezone,
        "coverage": coverage,
    }
    if not results:
        data["note"] = (
            "Nothing found in the stored messages. That does not mean it was never "
            "said: it may predate coverage, be deleted or expired, or use other words."
        )
    return ToolOutcome(
        "ok",
        data,
        contains_managed=True,
        refs=ToolRefs(
            conversation_ids=frozenset(hit.conversation_id for hit in result.hits),
            contact_ids=frozenset(contact_ids),
        ),
    )


SEARCH_MESSAGES = ToolDefinition(
    name="search_messages",
    description=(
        "Search the stored messages of the owner's chats with other people. Send the "
        "question as several short phrases in different scripts and languages (Uzbek "
        "Latin, Uzbek Cyrillic, Russian, English). Optional filters: one contact "
        "(use resolve_contact first for a name), one conversation, a date range. "
        "Returns the best matches with contact, time and text, plus coverage limits."
    ),
    args_model=SearchMessagesArgs,
    capability=CapabilityClass.READ,
    handler=search_messages_tool,
    release_flag=RELEASE_FLAG,
    returns_managed=True,
)
