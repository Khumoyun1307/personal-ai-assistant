"""Owner task controls, using the normal configure confirmation/echo path."""

from __future__ import annotations

import html
from collections.abc import Mapping
from datetime import timedelta
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account
from assistant.agent.tools.common import (
    ArgumentRuleError,
    OptionalId,
    check_id,
    local_time,
    local_to_utc,
    parse_local_moment,
    require_conversations,
)
from assistant.agent.tools.echo import queue_echo
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRefs,
)
from assistant.agent.untrusted import Source, wrap
from assistant.core.errors import NotInAccountError
from assistant.db.uow import transaction
from assistant.inbox import task_controls
from assistant.inbox.models import Task

LIST_FLAG = "tool.list_tasks"
UPDATE_FLAG = "tool.update_task"


def _positive_id(value: int) -> int:
    check_id(value)
    return value


def _days(value: int) -> int:
    if not 1 <= value <= 3650:
        raise ArgumentRuleError("stale_after_days must be between 1 and 3650")
    return value


def _limit(value: int) -> int:
    if not 1 <= value <= 50:
        raise ArgumentRuleError("limit must be between 1 and 50")
    return value


class ListTasksArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: OptionalId = Field(description="One chat, or null for all open tasks.")
    stale_after_days: Annotated[int, AfterValidator(_days)] = Field(
        description="Mark tasks this many days old as needing review; normally 30."
    )
    stale_only: bool = Field(description="Show only tasks needing review, without closing them.")
    after_task_id: OptionalId = Field(description="next_after_task_id from the last page, or null.")
    limit: Annotated[int, AfterValidator(_limit)] = Field(description="Page size, 1-50.")


class UpdateTaskArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: Annotated[int, AfterValidator(_positive_id)] = Field(description="Existing task id.")
    operation: task_controls.Operation = Field(
        description="complete when the owner explicitly says it is fulfilled; cancel; or correct."
    )
    title: str | None = Field(description="Corrected title, 1-400 characters, or null to keep it.")
    due_at: str | None = Field(
        description="Corrected owner-local YYYY-MM-DD HH:MM (or YYYY-MM-DD); null clears/keeps it."
    )
    change_due: bool = Field(description="true changes/clears the due date; false leaves it alone.")

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.title is not None:
            self.title = self.title.strip()
            if not self.title or len(self.title) > 400:
                raise ArgumentRuleError("title must be 1-400 characters")
        parse_local_moment(self.due_at)
        if self.due_at is not None and not self.change_due:
            raise ArgumentRuleError("set change_due to true to change the due date")
        if self.operation != "correct" and (self.title is not None or self.change_due):
            raise ArgumentRuleError("title and due date changes require operation correct")
        if self.operation == "correct" and self.title is None and not self.change_due:
            raise ArgumentRuleError("a correction needs a title or a due date change")
        return self


async def list_tasks_tool(ctx: ToolContext, args: ListTasksArgs) -> ToolOutcome:
    now = ctx.services.clock.now()
    stale_before = now - timedelta(days=args.stale_after_days)
    async with ctx.services.session_factory() as session:
        if args.conversation_id is not None:
            await require_conversations(session, ctx.account_id, [args.conversation_id])
        account = await session.get(Account, ctx.account_id)
        if account is None:
            raise NotInAccountError("account not found")
        stmt = select(Task).where(Task.account_id == ctx.account_id, Task.status == "open")
        if args.conversation_id is not None:
            stmt = stmt.where(Task.conversation_id == args.conversation_id)
        if args.after_task_id is not None:
            await task_controls.get(session, account_id=ctx.account_id, task_id=args.after_task_id)
            stmt = stmt.where(Task.id > args.after_task_id)
        if args.stale_only:
            stmt = stmt.where(Task.created_at <= stale_before)
        rows = list((await session.scalars(stmt.order_by(Task.id).limit(args.limit + 1))).all())
        more = len(rows) > args.limit
        rows = rows[: args.limit]
        data = [
            {
                "task_id": row.id,
                "conversation_id": row.conversation_id,
                "kind": row.kind,
                "title": wrap(row.title, Source.TASK_TITLE),
                "status": row.status,
                "created_at": local_time(row.created_at, account.timezone),
                "source_message_ids": list(row.source_message_ids or ()),
                "due_at": local_time(row.due_at, account.timezone),
                "due_state": (
                    "no_due_date"
                    if row.due_at is None
                    else "overdue"
                    if row.due_at < now
                    else "scheduled"
                ),
                "needs_review": row.created_at <= stale_before,
            }
            for row in rows
        ]
    return ToolOutcome(
        "ok",
        {"tasks": data, "next_after_task_id": rows[-1].id if more else None},
        contains_managed=bool(rows),
        refs=ToolRefs(
            conversation_ids=frozenset(
                row.conversation_id for row in rows if row.conversation_id is not None
            )
        ),
    )


async def update_task_tool(ctx: ToolContext, args: UpdateTaskArgs) -> ToolOutcome:
    async with transaction(ctx.services.session_factory) as uow:
        account = await uow.session.get(Account, ctx.account_id)
        if account is None:
            raise NotInAccountError("account not found")
        local = parse_local_moment(args.due_at)
        result, audit_id = await task_controls.change(
            uow.session,
            account_id=ctx.account_id,
            task_id=args.task_id,
            operation=args.operation,
            title=args.title,
            due_at=None if local is None else local_to_utc(local, account.timezone),
            change_due=args.change_due,
            run_id=ctx.run_id,
            call_id=ctx.call_id,
            clock=ctx.services.clock,
        )
        if audit_id is None:
            return ToolOutcome(
                "error" if result == "not_open" else "ok",
                {"task_id": args.task_id, "changed": False, "reason": result},
            )
        row = await task_controls.get(uow.session, account_id=ctx.account_id, task_id=args.task_id)
        due = local_time(row.due_at, account.timezone) or "no due date"
        echo = await queue_echo(
            ctx,
            uow.session,
            audit_id,
            f"Task #{row.id}: {html.escape(row.title)} — {row.status}; {html.escape(due)}.",
        )
    return ToolOutcome("ok", {"task_id": args.task_id, "changed": True, "echo": echo.note})


async def describe_task(
    session: AsyncSession, account_id: int, args: Mapping[str, Any]
) -> str | None:
    task_id = args.get("task_id")
    if not isinstance(task_id, int):
        return None
    row = await task_controls.get(session, account_id=account_id, task_id=task_id)
    return f"Task #{row.id}: {html.escape(row.title)}"


LIST_TASKS = ToolDefinition(
    name="list_tasks",
    description=(
        "List open tasks and commitments, including undated, overdue and stale review items."
    ),
    args_model=ListTasksArgs,
    capability=CapabilityClass.READ,
    handler=list_tasks_tool,
    release_flag=LIST_FLAG,
    returns_managed=True,
)
UPDATE_TASK = ToolDefinition(
    name="update_task",
    description=(
        "Complete or cancel a task at the owner's explicit request, or correct its title/due date. "
        "Age never means fulfilled. Changes are shown with Undo; tainted runs need confirmation."
    ),
    args_model=UpdateTaskArgs,
    capability=CapabilityClass.CONFIGURE,
    handler=update_task_tool,
    release_flag=UPDATE_FLAG,
    describe=describe_task,
)
