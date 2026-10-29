"""`pause_autonomy`, `resume_autonomy` — the kill switch as tools (ARCHITECTURE
§7.1, §8.2; S04.4 §3.7).

Both are `control`. `pause_autonomy` only reduces autonomy, so it is
taint-exempt (`TAINT_EXEMPT_TOOLS`): it runs even in a tainted run, as the
kill switch must. `resume_autonomy` is card-only (`CARD_ONLY_TOOLS`): a model
call never resumes anything — it becomes a confirmation card, and only the
owner's tap runs this handler (through `agent.apply_confirmation`). Both do
exactly what `/pause` and `/resume` do (`actions/autonomy.py`) and are
idempotent on `(run_id, tool_call_id)`: a retried call returns the result it
recorded and changes nothing. Results are factual and content-free.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from assistant.accounts.models import AuditLogEntry
from assistant.actions import autonomy
from assistant.agent.tools.registry import CapabilityClass, ToolContext, ToolDefinition, ToolOutcome
from assistant.db.uow import transaction

PAUSE_FLAG = "tool.pause_autonomy"
RESUME_FLAG = "tool.resume_autonomy"


class NoArgs(BaseModel):
    """No arguments. `extra="forbid"` rejects anything the model invents."""

    model_config = ConfigDict(extra="forbid")


async def _recorded(ctx: ToolContext, event: str) -> bool:
    """Whether this very call (`run_id`, `call_id`) already changed the flag."""
    if not ctx.call_id:
        return False
    async with ctx.services.session_factory() as session:
        found = await session.scalar(
            select(AuditLogEntry.id)
            .where(
                AuditLogEntry.account_id == ctx.account_id,
                AuditLogEntry.event == event,
                AuditLogEntry.details["run_id"].astext == str(ctx.run_id),
                AuditLogEntry.details["call_id"].astext == ctx.call_id,
            )
            .limit(1)
        )
    return found is not None


async def pause_autonomy(ctx: ToolContext, _args: NoArgs) -> ToolOutcome:
    if await _recorded(ctx, autonomy.EVENT_PAUSED):
        return ToolOutcome("ok", {"autonomy": "paused", "changed": True})
    async with transaction(ctx.services.session_factory) as uow:
        changed = await autonomy.pause(
            uow.session,
            account_id=ctx.account_id,
            now=ctx.services.clock.now(),
            source=autonomy.SOURCE_TOOL,
            details={"run_id": ctx.run_id, "call_id": ctx.call_id},
        )
    return ToolOutcome(
        "ok",
        {
            "autonomy": "paused",
            "changed": changed,
            "note": "Automatic replies are stopped; replies the owner approves are still sent.",
        },
    )


async def resume_autonomy(ctx: ToolContext, _args: NoArgs) -> ToolOutcome:
    """Runs only from the owner's tap on its confirmation card."""
    if await _recorded(ctx, autonomy.EVENT_RESUMED):
        return ToolOutcome("ok", {"autonomy": "resumed", "changed": True})
    async with transaction(ctx.services.session_factory) as uow:
        changed = await autonomy.resume(
            uow.session,
            account_id=ctx.account_id,
            now=ctx.services.clock.now(),
            source=autonomy.SOURCE_CARD,
            details={"run_id": ctx.run_id, "call_id": ctx.call_id},
        )
    return ToolOutcome("ok", {"autonomy": "resumed", "changed": changed})


PAUSE_AUTONOMY = ToolDefinition(
    name="pause_autonomy",
    description=(
        "The kill switch: stop all automatic replies now, as /pause does. Replies the "
        "owner approves on a card are still sent. Use it whenever the owner asks to stop, "
        "pause or halt the assistant acting on their behalf."
    ),
    args_model=NoArgs,
    capability=CapabilityClass.CONTROL,
    handler=pause_autonomy,
    release_flag=PAUSE_FLAG,
)

RESUME_AUTONOMY = ToolDefinition(
    name="resume_autonomy",
    description=(
        "Ask to resume automatic replies after a pause. This never resumes by itself: the "
        "owner gets a confirmation card and only their tap resumes (or /resume)."
    ),
    args_model=NoArgs,
    capability=CapabilityClass.CONTROL,
    handler=resume_autonomy,
    release_flag=RESUME_FLAG,
)
