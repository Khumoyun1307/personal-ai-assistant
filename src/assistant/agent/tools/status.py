"""`get_status` — the assistant's own state (ARCHITECTURE §8.2, R0).

Read-only and content-free: the same `StatusReport` that `/status` renders
(`observability/status.py`), so the model and the command never disagree. It
carries no contact text or names, so its results never taint a run and need
no `ai_managed` consent.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from assistant.accounts.models import Account
from assistant.agent.tools.registry import CapabilityClass, ToolContext, ToolDefinition, ToolOutcome
from assistant.db.uow import transaction
from assistant.observability.status import build_status

RELEASE_FLAG = "tool.get_status"


class GetStatusArgs(BaseModel):
    """No arguments. `extra="forbid"` rejects anything the model invents."""

    model_config = ConfigDict(extra="forbid")


async def get_status(ctx: ToolContext, _args: GetStatusArgs) -> ToolOutcome:
    services = ctx.services
    async with transaction(services.session_factory) as uow:
        account = await uow.session.get(Account, ctx.account_id)
        if account is None:
            return ToolOutcome("error", {"reason": "account not found"})
        report = await build_status(
            uow.session,
            account,
            services.clock.now(),
            monthly_budget_usd=services.monthly_budget_usd,
        )
    return ToolOutcome("ok", report.as_dict())


GET_STATUS = ToolDefinition(
    name="get_status",
    description=(
        "The assistant's current state: account status, whether autonomy is paused, "
        "AI consent, the business connection and its rights, which chats are observed "
        "and since when, coverage gaps, inbox health and this month's AI spend. "
        "Use it for any question about the assistant itself or what it can see."
    ),
    args_model=GetStatusArgs,
    capability=CapabilityClass.READ,
    handler=get_status,
    release_flag=RELEASE_FLAG,
)
