"""`get_status` — the assistant's own state (ARCHITECTURE §8.2, R0).

Read-only and content-free: account state, consent, business connection,
coverage and budget. It carries no contact text, so its results never taint a
run and need no `ai_managed` consent.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from assistant.accounts.models import Account
from assistant.accounts.service import CONSENT_AI_CONTROL, CONSENT_AI_MANAGED, active_consent
from assistant.agent.persistence import LlmCallRepository, month_start
from assistant.agent.tools.registry import CapabilityClass, ToolContext, ToolDefinition, ToolOutcome
from assistant.db.uow import transaction

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
        control = await active_consent(uow.session, ctx.account_id, kind=CONSENT_AI_CONTROL)
        managed = await active_consent(uow.session, ctx.account_id, kind=CONSENT_AI_MANAGED)
        spent = await LlmCallRepository(uow.session, ctx.account_id).spend_since(
            month_start(services.clock.now())
        )
    limit = services.monthly_budget_usd
    return ToolOutcome(
        "ok",
        {
            "account_status": account.status,
            "autonomy": "paused" if account.autonomy_paused else "active",
            "timezone": account.timezone,
            "ai_consent": {"control_chat": control.granted, "managed_chats": managed.granted},
            # M01 has no business connection at all; M02 adds the registry.
            "business_connection": "none",
            "coverage": "no managed chats are observed",
            "budget": {
                "month_to_date_usd": f"{spent:.4f}",
                "monthly_limit_usd": None if limit is None else f"{limit}",
            },
        },
    )


GET_STATUS = ToolDefinition(
    name="get_status",
    description=(
        "The assistant's current state: account status, whether autonomy is paused, "
        "AI consent, business connection, coverage and this month's AI spend. "
        "Use it for any question about the assistant itself."
    ),
    args_model=GetStatusArgs,
    capability=CapabilityClass.READ,
    handler=get_status,
    release_flag=RELEASE_FLAG,
)
