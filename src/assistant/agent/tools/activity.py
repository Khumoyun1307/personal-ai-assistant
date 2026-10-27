"""`get_activity` tool (S04.5; PRD FR-ACT-01, FR-ACT-02; ARCHITECTURE §8.2).

Read tool over the owner's actions and drafts:
- Lists what the assistant sent, drafted or failed to send on the owner's behalf
  in a period (default today 00:00 -> now).
- Filters: show ('all', 'sent', 'drafts', 'problems'), conversation_id or contact_id.
- Untrusted rendering: contact names and message excerpts are wrapped as untrusted
  (taints the run; AGENTS §6 invariant 3b).
- Returns managed: needs `ai_managed` consent (invariant 9); blocked stub without consent.
- Released under flag `tool.get_activity`.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from assistant.accounts.models import Account
from assistant.actions.activity import activity
from assistant.agent.tools.common import (
    ArgumentRuleError,
    OptionalId,
    local_time,
    local_to_utc,
    name,
    one,
    parse_local_moment,
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
from assistant.agent.untrusted import Source, wrap
from assistant.conversations.models import Conversation
from assistant.core.errors import NotInAccountError
from assistant.db.uow import transaction

GET_ACTIVITY_FLAG = "tool.get_activity"
MAX_PERIOD = timedelta(days=31)
STATUS_RULE = "Report each entry's group exactly; never call a draft or an uncertain send 'sent'."


def _moment(value: str | None) -> str | None:
    if value is not None:
        parse_local_moment(value)
    return value


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


class GetActivityArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    since: Annotated[str | None, AfterValidator(_moment)] = Field(
        description=(
            "Start of the period: 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD' in the owner's timezone, "
            "or null for today 00:00."
        ),
    )
    until: Annotated[str | None, AfterValidator(_moment)] = Field(
        description="End of the period, same format, or null for now.",
    )
    conversation_id: OptionalId = Field(description="Only this conversation, or null.")
    contact_id: OptionalId = Field(description="Only the chat with this contact, or null.")
    show: Literal["all", "sent", "drafts", "problems"] | None = Field(
        description=(
            "Which entries to show: all, sent, drafts (awaiting/not sent), "
            "problems (failed/uncertain/blocked), or null for all."
        ),
    )
    limit: int | None = Field(description="Max entries to return (1-50), or null for 30.")

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.contact_id is not None and self.conversation_id is not None:
            raise ArgumentRuleError(
                "give at most one of contact_id or conversation_id; set the other to null"
            )
        since_m = parse_local_moment(self.since)
        until_m = parse_local_moment(self.until)
        if since_m is not None and until_m is not None:
            if until_m < since_m:
                raise ArgumentRuleError("since must be before until")
            if until_m - since_m > MAX_PERIOD:
                raise ArgumentRuleError("a period is at most 31 days")
        if self.limit is not None and not 1 <= self.limit <= 50:
            raise ArgumentRuleError("limit must be between 1 and 50")
        return self


async def get_activity_tool(ctx: ToolContext, args: GetActivityArgs) -> ToolOutcome:
    services = ctx.services
    now_utc = services.clock.now()

    limit = args.limit if args.limit is not None else 30
    show = args.show if args.show is not None else "all"

    groups: list[str] | None = None
    if show == "sent":
        groups = ["sent"]
    elif show == "drafts":
        groups = ["awaiting", "not_sent"]
    elif show == "problems":
        groups = ["failed", "uncertain", "blocked"]

    async with transaction(services.session_factory) as uow:
        session = uow.session
        account = await session.get(Account, ctx.account_id)
        if account is None:
            raise NotInAccountError("account")

        tz = account.timezone
        tzinfo = _zone(tz)
        now_local = now_utc.astimezone(tzinfo)

        if args.since is not None:
            since_dt = parse_local_moment(args.since)
            assert since_dt is not None
            start = local_to_utc(since_dt, tz)
        else:
            start_local = datetime.combine(now_local.date(), time.min, tzinfo=tzinfo)
            start = start_local.astimezone(now_utc.tzinfo)

        if args.until is not None:
            until_dt = parse_local_moment(args.until)
            assert until_dt is not None
            end = local_to_utc(until_dt, tz)
        else:
            end = now_utc

        if start >= end:
            return ToolOutcome("error", {"reason": "the period has not started yet"})
        if end - start > MAX_PERIOD:
            return ToolOutcome("error", {"reason": "a period is at most 31 days"})

        await require_contacts(session, ctx.account_id, one(args.contact_id))
        await require_conversations(session, ctx.account_id, one(args.conversation_id))

        conv_scope: list[int] | None = None
        if args.conversation_id is not None:
            conv_scope = [args.conversation_id]
        elif args.contact_id is not None:
            conv_scope = [
                int(i)
                for i in (
                    await session.execute(
                        select(Conversation.id)
                        .where(Conversation.account_id == ctx.account_id)
                        .where(Conversation.contact_id == args.contact_id)
                    )
                ).scalars()
            ]

        entries = await activity(
            session,
            account_id=ctx.account_id,
            since=start,
            until=end,
            conversation_ids=conv_scope,
            groups=groups,
            limit=limit,
        )

    rendered_entries: list[dict[str, Any]] = []
    for entry in entries:
        contact_name = entry.contact_display_name or (
            f"@{entry.contact_username}" if entry.contact_username else None
        )
        rendered_entries.append(
            {
                "action_id": entry.action_id,
                "conversation_id": entry.conversation_id,
                "contact_id": entry.contact_id,
                "contact_name": name(contact_name),
                "group": entry.group,
                "state_reason": entry.state_reason,
                "time": local_time(entry.executed_at or entry.decided_at or entry.created_at, tz),
                "excerpt": (
                    wrap(entry.excerpt, Source.OWNER_IN_BUSINESS_CHAT)
                    if entry.excerpt is not None
                    else None
                ),
                "authorized": (
                    {
                        "approved_at": local_time(entry.authorization.approved_at, tz),
                        "version": entry.authorization.version,
                    }
                    if entry.authorization is not None
                    else None
                ),
            }
        )

    data = {
        "period": {"from": local_time(start, tz), "to": local_time(end, tz), "timezone": tz},
        "entries": rendered_entries,
        "count": len(rendered_entries),
        "status_rule": STATUS_RULE,
    }

    return ToolOutcome(
        "ok",
        data,
        contains_managed=True,
        untrusted=True,
        refs=ToolRefs(
            conversation_ids=frozenset(e.conversation_id for e in entries),
            contact_ids=frozenset(e.contact_id for e in entries),
        ),
    )


GET_ACTIVITY = ToolDefinition(
    name="get_activity",
    description=(
        "List what the assistant sent, drafted or failed to send on the owner's behalf "
        "in a period (today by default, or all / sent / drafts / problems)."
    ),
    args_model=GetActivityArgs,
    capability=CapabilityClass.READ,
    handler=get_activity_tool,
    release_flag=GET_ACTIVITY_FLAG,
    returns_managed=True,
)
