"""Read effective settings without contact content or state changes (Q1)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from assistant.accounts import preferences
from assistant.accounts.models import Account
from assistant.agent.tools.registry import CapabilityClass, ToolContext, ToolDefinition, ToolOutcome
from assistant.db.uow import transaction
from assistant.privacy import retention_policy

RELEASE_FLAG = "tool.get_preferences"


class GetPreferencesArgs(BaseModel):
    """No arguments: the account always comes from the owner run."""

    model_config = ConfigDict(extra="forbid")


async def get_preferences(ctx: ToolContext, _args: GetPreferencesArgs) -> ToolOutcome:
    async with transaction(ctx.services.session_factory) as uow:
        account = await uow.session.get(Account, ctx.account_id)
        if account is None:
            return ToolOutcome("error", {"reason": "account not found"})
        values = preferences.current(account)
    defaults: dict[str, Any] = {
        preferences.FIELD_LANGUAGE: None,
        preferences.FIELD_TIMEZONE: preferences.DEFAULT_TIMEZONE,
        preferences.FIELD_DEFAULT_MODE: preferences.FALLBACK_DEFAULT_MODE,
        **preferences.REPLY_DEFAULTS,
        **preferences.M05_DEFAULTS,
        **{
            field: retention_policy.CLASSES[key].default_days
            for key, field in preferences.FIELD_RETENTION.items()
        },
    }
    return ToolOutcome(
        "ok",
        {
            "preferences": {
                field: {
                    "value": value,
                    # Equality with the default, not whether the owner ever set it.
                    "is_default": value == defaults[field],
                    "change_via": ["chat", "/settings"],
                }
                for field, value in values.items()
            },
            "notes": {
                "preferred_language": "null means automatic answer language",
                "reply_language": "match means the contact's language",
                "digest_time": "off means no scheduled daily digest",
                "quiet_hours": "off means no quiet hours",
                "times": "local times in the reported timezone",
                "is_default": "equals the default value, even if explicitly chosen",
            },
        },
    )


GET_PREFERENCES = ToolDefinition(
    name="get_preferences",
    description=(
        "Read the owner's effective current settings before answering questions about "
        "them: answer language, timezone, mode for new chats, reply language/tone/style, "
        "retention days for messages/this chat/raw updates, quiet hours, urgent bypass, "
        "digest time (off means no scheduled digest), and alerts per hour. Each value "
        "says whether it equals the default and where to change it (chat or /settings). "
        "No arguments, no message content, no changes."
    ),
    args_model=GetPreferencesArgs,
    capability=CapabilityClass.READ,
    handler=get_preferences,
    release_flag=RELEASE_FLAG,
)
