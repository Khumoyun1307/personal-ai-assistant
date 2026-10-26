"""`set_conversation_mode`, `update_preferences` (ARCHITECTURE §8.2; PRD §11.1,
FR-MEM-02; S03.3).

Both are `configure`: held for a confirmation card in a tainted run,
otherwise executed and echoed with a one-tap Undo. The domain setters
(`conversations/modes.py`, `accounts/preferences.py`) validate, scope by
account and audit; these handlers only translate.
"""

from __future__ import annotations

import html
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from assistant.accounts import preferences
from assistant.accounts.models import Account
from assistant.agent.tools.common import OptionalId, require_contacts
from assistant.agent.tools.echo import queue_echo
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
)
from assistant.conversations import modes
from assistant.conversations.models import Contact
from assistant.db.uow import transaction

MODE_FLAG = "tool.set_conversation_mode"
PREFERENCES_FLAG = "tool.update_preferences"

MODE_EFFECT = {
    modes.MODE_IGNORE: (
        "new messages from this chat are no longer stored or processed; what is "
        "already stored stays"
    ),
    modes.MODE_OBSERVE: "new messages from this chat are stored and used again",
}


# ------------------------------------------------------------ set_conversation_mode
class SetConversationModeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: OptionalId = Field(description="The chat, or null to use contact_id.")
    contact_id: OptionalId = Field(description="The contact whose chat it is, or null.")
    mode: Literal["observe", "ignore"] = Field(
        description="observe: store and use new messages; ignore: stop storing new messages."
    )

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        if (self.conversation_id is None) == (self.contact_id is None):
            raise ValueError("give exactly one of conversation_id or contact_id")
        return self


async def set_conversation_mode_tool(
    ctx: ToolContext, args: SetConversationModeArgs
) -> ToolOutcome:
    services = ctx.services
    async with transaction(services.session_factory) as uow:
        session = uow.session
        conversation_id = args.conversation_id
        if conversation_id is None:
            assert args.contact_id is not None
            await require_contacts(session, ctx.account_id, [args.contact_id])
            conversation_id = await modes.conversation_for_contact(
                session, ctx.account_id, args.contact_id
            )
        try:
            change = await modes.set_mode(
                session,
                account_id=ctx.account_id,
                conversation_id=conversation_id,
                mode=args.mode,
                actor="agent",
                clock=services.clock,
            )
        except modes.ModeError as error:
            return ToolOutcome("error", {"reason": str(error)})
        echo = None
        if change.audit_id is not None:
            name = (
                await session.execute(
                    select(Contact.display_name)
                    .where(Contact.account_id == ctx.account_id)
                    .where(Contact.id == change.contact_id)
                )
            ).scalar_one_or_none()
            who = html.escape(name) if name else "this contact"
            text = (
                f"🔧 Chat with {who} set to <b>{change.current.capitalize()}</b>: "
                f"{MODE_EFFECT[change.current]}."
            )
            echo = await queue_echo(ctx, session, change.audit_id, text)
    data: dict[str, object] = {
        "conversation_id": change.conversation_id,
        "contact_id": change.contact_id,
        "previous_mode": change.previous,
        "mode": change.current,
        "effect": MODE_EFFECT.get(change.current, ""),
    }
    if echo is None:
        # Already so: the owner's wish holds; nothing changed (review item 3).
        return ToolOutcome(
            "ok", {**data, "changed": False, "note": "the chat was already in this mode"}
        )
    data["changed"] = True
    data["echo"] = echo.note
    return ToolOutcome("ok", data)


SET_CONVERSATION_MODE = ToolDefinition(
    name="set_conversation_mode",
    description=(
        "Set one chat to observe (store and use new messages) or ignore (stop storing "
        "new messages; what is stored stays). The owner sees it with an Undo button. "
        "Assist mode is not available yet; chats with bots stay ignored."
    ),
    args_model=SetConversationModeArgs,
    capability=CapabilityClass.CONFIGURE,
    handler=set_conversation_mode_tool,
    release_flag=MODE_FLAG,
)


# ------------------------------------------------------------ update_preferences
def _language(value: str | None) -> str | None:
    return None if value is None else preferences.check_language(value)


def _timezone_shape(value: str | None) -> str | None:
    if value is not None and not 1 <= len(value.strip()) <= preferences.MAX_TIMEZONE_LENGTH:
        raise ValueError("timezone must be an IANA name such as Asia/Tashkent")
    return None if value is None else value.strip()


class UpdatePreferencesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferred_language: Annotated[str | None, AfterValidator(_language)] = Field(
        description="The language the owner wants answers in, as a code (uz, ru, en), or null."
    )
    timezone: Annotated[str | None, AfterValidator(_timezone_shape)] = Field(
        description="The owner's timezone as an IANA name (e.g. Asia/Tashkent), or null."
    )
    default_mode: Literal["observe", "ignore"] | None = Field(
        description="The mode new chats start in, or null."
    )

    @model_validator(mode="after")
    def _something(self) -> Self:
        if self.preferred_language is None and self.timezone is None and self.default_mode is None:
            raise ValueError("give at least one setting to change")
        return self


FIELD_LABELS = preferences.FIELD_LABELS


async def update_preferences_tool(ctx: ToolContext, args: UpdatePreferencesArgs) -> ToolOutcome:
    services = ctx.services
    async with transaction(services.session_factory) as uow:
        session = uow.session
        # `preferences.update` takes the account lock (FOR NO KEY UPDATE).
        account = await session.get(Account, ctx.account_id)
        if account is None:
            raise LookupError("account")
        try:
            change = await preferences.update(
                session,
                account,
                language=args.preferred_language,
                timezone=args.timezone,
                new_default_mode=args.default_mode,
                actor="agent",
                clock=services.clock,
            )
        except preferences.PreferenceError as error:
            return ToolOutcome("error", {"reason": str(error)})
        echo = None
        if change.audit_id is not None:
            lines = [
                f"{FIELD_LABELS[f]}: {html.escape(str(old or 'auto'))} → {html.escape(str(new))}"
                for f, (old, new) in change.changes.items()
            ]
            echo = await queue_echo(
                ctx, session, change.audit_id, "⚙️ Settings changed\n" + "\n".join(lines)
            )
    changed = {f: new for f, (_, new) in change.changes.items()}
    if echo is None:
        return ToolOutcome(
            "ok", {"changed": False, "note": "those settings already had these values"}
        )
    return ToolOutcome("ok", {"changed": True, "values": changed, "echo": echo.note})


UPDATE_PREFERENCES = ToolDefinition(
    name="update_preferences",
    description=(
        "Change the owner's settings: preferred answer language, timezone, or the "
        "mode new chats start in. The owner sees the change with an Undo button. "
        "Style and tone wishes are not settings: save them with remember."
    ),
    args_model=UpdatePreferencesArgs,
    capability=CapabilityClass.CONFIGURE,
    handler=update_preferences_tool,
    release_flag=PREFERENCES_FLAG,
)
