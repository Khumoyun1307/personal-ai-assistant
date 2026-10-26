"""`set_conversation_mode`, `update_preferences` (ARCHITECTURE §8.2; PRD §11.1,
FR-MEM-02; S03.3).

Both are `configure`: held for a confirmation card in a tainted run,
otherwise executed and echoed with a one-tap Undo. The domain setters
(`conversations/modes.py`, `accounts/preferences.py`) validate, scope by
account and audit; these handlers only translate.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts import preferences
from assistant.accounts.models import Account
from assistant.agent.tools.common import OptionalId, require_contacts
from assistant.agent.tools.echo import queue_echo
from assistant.agent.tools.memory import contact_of_chat
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
)
from assistant.conversations import modes
from assistant.conversations.models import Contact
from assistant.db.uow import transaction
from assistant.privacy import retention_policy
from assistant.privacy.retention_policy import CONTROL_CHAT, MESSAGES, RAW_UPDATES

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


async def describe_mode(
    session: AsyncSession, account_id: int, args: Mapping[str, Any]
) -> str | None:
    """For the confirmation card (owner-facing): whose chat it is (S1)."""
    conversation_id = args.get("conversation_id")
    contact_id = args.get("contact_id")
    if isinstance(contact_id, int):
        await require_contacts(session, account_id, [contact_id])
        conversation_id = await modes.conversation_for_contact(session, account_id, contact_id)
    if not isinstance(conversation_id, int):
        return None
    name = await contact_of_chat(session, account_id, conversation_id)
    effect = MODE_EFFECT.get(str(args.get("mode")), "")
    return f"the chat with {name}: {effect}"


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
    describe=describe_mode,
)


# ------------------------------------------------------------ update_preferences
def _language(value: str | None) -> str | None:
    return None if value is None else preferences.check_language(value)


def _timezone_shape(value: str | None) -> str | None:
    if value is not None and not 1 <= len(value.strip()) <= preferences.MAX_TIMEZONE_LENGTH:
        raise ValueError("timezone must be an IANA name such as Asia/Tashkent")
    return None if value is None else value.strip()


def _reply_language(value: str | None) -> str | None:
    return None if value is None else preferences.check_reply_language(value)


def _days(key: str) -> Callable[[int | None], int | None]:
    """A retention value inside the class's allowed range (M03 D80)."""

    def check(value: int | None) -> int | None:
        return None if value is None else retention_policy.CLASSES[key].check(value)

    return check


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
    # S03.5: reply defaults (used for drafts from M04) and retention.
    reply_language: Annotated[str | None, AfterValidator(_reply_language)] = Field(
        description=(
            "The language replies to contacts are drafted in: 'match' (the contact's own "
            "language) or a code (uz, ru, en); or null."
        ),
    )
    reply_tone: Literal["neutral", "friendly", "formal"] | None = Field(
        description="The default tone of replies to contacts, or null."
    )
    reply_style: Literal["brief", "detailed"] | None = Field(
        description="The default length of replies to contacts, or null."
    )
    message_retention_days: Annotated[int | None, AfterValidator(_days(MESSAGES))] = Field(
        description="Keep message text this many days (14-90; older text is deleted), or null.",
    )
    control_chat_retention_days: Annotated[int | None, AfterValidator(_days(CONTROL_CHAT))] = Field(
        description="Keep this chat's history this many days (14-90), or null.",
    )
    raw_update_retention_days: Annotated[int | None, AfterValidator(_days(RAW_UPDATES))] = Field(
        description="Keep raw Telegram updates this many days (1-7), or null."
    )

    @model_validator(mode="after")
    def _something(self) -> Self:
        if all(getattr(self, name) is None for name in type(self).model_fields):
            raise ValueError("give at least one setting to change")
        return self

    def retention(self) -> dict[str, int]:
        """The retention values given, by data class."""
        given = {
            MESSAGES: self.message_retention_days,
            CONTROL_CHAT: self.control_chat_retention_days,
            RAW_UPDATES: self.raw_update_retention_days,
        }
        return {key: days for key, days in given.items() if days is not None}


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
                reply_language=args.reply_language,
                reply_tone=args.reply_tone,
                reply_style=args.reply_style,
                retention=args.retention(),
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


def _sets_retention(args: Any) -> bool:
    """A retention value deletes data at the next sweep: always a card (S03.5)."""
    return isinstance(args, UpdatePreferencesArgs) and bool(args.retention())


async def describe_preferences(
    session: AsyncSession, account_id: int, args: Mapping[str, Any]
) -> str | None:
    """For the confirmation card: what a retention value deletes, and when."""
    lines = []
    for key, field in preferences.FIELD_RETENTION.items():
        days = args.get(field)
        if isinstance(days, int):
            label = retention_policy.CLASSES[key].label.lower()
            lines.append(
                f"{label} older than {days} days are deleted at the next hourly cleanup "
                "and cannot be restored"
            )
    return "; ".join(lines) or None


UPDATE_PREFERENCES = ToolDefinition(
    name="update_preferences",
    description=(
        "Change the owner's settings: preferred answer language, timezone, the mode "
        "new chats start in, the default reply language, tone and style for replies "
        "to contacts, and how long messages, this chat and raw updates are kept "
        "(shorter only). The owner sees the change with an Undo button; a retention "
        "change needs the owner's tap on a card first. Free-text style wishes are not "
        "settings: save them with remember."
    ),
    args_model=UpdatePreferencesArgs,
    capability=CapabilityClass.CONFIGURE,
    handler=update_preferences_tool,
    release_flag=PREFERENCES_FLAG,
    confirm_if=_sets_retention,
    describe=describe_preferences,
)
