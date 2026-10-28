"""`set_conversation_mode`, `update_preferences` (ARCHITECTURE §8.2; PRD §11.1,
FR-MEM-02; S03.3).

Both are `configure`: held for a confirmation card in a tainted run,
otherwise executed and echoed with a one-tap Undo. The domain setters
(`conversations/modes.py`, `accounts/preferences.py`) validate, scope by
account and audit; these handlers only translate. A mode change is echoed
with the same fixed receipt as the conversation panel's (`mode_change_text`,
M03 fix round C3).
"""

from __future__ import annotations

import html
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts import preferences, service
from assistant.accounts.models import Account
from assistant.agent import owner_texts
from assistant.agent.locales import SupportedLocale, supported_locale
from assistant.agent.tools.common import (
    ONE_TARGET_RULE,
    ArgumentRuleError,
    OptionalId,
    require_contacts,
)
from assistant.agent.tools.echo import queue_echo
from assistant.agent.tools.memory import contact_of_chat
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
)
from assistant.conversations import modes
from assistant.conversations.models import Contact, Conversation
from assistant.core.errors import NotInAccountError
from assistant.db.uow import transaction
from assistant.privacy import retention_policy
from assistant.privacy.retention_policy import CONTROL_CHAT, MESSAGES, RAW_UPDATES

MODE_FLAG = "tool.set_conversation_mode"
PREFERENCES_FLAG = "tool.update_preferences"

#: What the mode does, in English, as exactly as the owner's receipt
#: (`owner_texts.mode_receipt`) says it: the tool result's `effect` (for the
#: model) and a tainted run's confirmation card (for the owner), so it is
#: worded for both readers. Use `mode_effect`.
MODE_EFFECT = {
    modes.MODE_IGNORE: (
        "new messages from this chat are no longer saved (only a note that a message "
        "arrived); the chat is left out of summaries, catch-ups and the list of who is "
        "waiting for a reply; what was saved before stays unless deleted (messages until "
        "they expire), and Ignore does not hide it from answers"
    ),
    modes.MODE_OBSERVE: "new messages from this chat are saved and used again",
    modes.MODE_ASSIST: (
        "drafts are offered for new messages that need a reply; nothing is sent without "
        "your approval"
    ),
}

#: The Assist receipt (S04.2; English in P2, P2-O11): what the owner gets
#: from the panel or the tool. `{name}` is escaped by `mode_change_text`.
ASSIST_ON = "<b>Assist is on</b> for the chat with {name}."
ASSIST_DRAFTS = (
    "When new messages here need a reply, I draft one and show it to you here to approve, "
    "edit or reject. Nothing is sent without your approval."
)
ASSIST_SAVED = "New messages here are saved and used, as in Observe."
ASSIST_AI_OFF = (
    "AI for your connected chats is off, so no drafts are made. Send /privacy managed to "
    "turn it on."
)
ASSIST_UNCHANGED = "Nothing to change: the chat with {name} is already in Assist."
#: Said with Observe only when the chat was in Ignore until now.
OBSERVE_AFTER_IGNORE = "; messages that arrived while it was ignored were not saved"


def mode_effect(mode: str, *, previous: str | None) -> str:
    """What switching to `mode` from `previous` does (C3 review: the lost
    Ignore-period messages are named only for a chat that was ignored)."""
    effect = MODE_EFFECT.get(mode, "")
    if mode == modes.MODE_OBSERVE and previous == modes.MODE_IGNORE:
        effect += OBSERVE_AFTER_IGNORE
    return effect


async def mode_change_text(
    session: AsyncSession,
    account: Account,
    *,
    previous: str,
    current: str,
    name: str | None,
    deleting: bool = False,
) -> str:
    """What a mode change did, for the owner, in their locale (§6 invariant
    11): the receipt, or "nothing to change" when the chat was already in
    that mode (M03 D68). `name`: the contact's stored name, or None. Fixed
    texts only; no model is involved (`agent/owner_texts.py`)."""
    locale = supported_locale(account.locale_pref)
    if current == modes.MODE_ASSIST:
        return await _assist_text(session, account, locale, previous=previous, name=name)
    if previous == current:
        return owner_texts.mode_unchanged(locale, mode=current, name=name)
    ai_on = True  # only the Observe receipt depends on it
    if current == modes.MODE_OBSERVE:
        ai_on = (await service.active_consent(session, account.id, kind="ai_managed")).granted
    return owner_texts.mode_receipt(
        locale,
        mode=current,
        previous=previous,
        name=name,
        days=retention_policy.days(account.settings, MESSAGES),
        ai_on=ai_on,
        deleting=deleting,
    )


async def _assist_text(
    session: AsyncSession,
    account: Account,
    locale: SupportedLocale,
    *,
    previous: str,
    name: str | None,
) -> str:
    """The Assist receipt, or "nothing to change" (fixed texts, §6 invariant 11)."""
    who = html.escape(name, quote=False) if name else owner_texts.CONTACT_FALLBACK[locale]
    if previous == modes.MODE_ASSIST:
        return ASSIST_UNCHANGED.format(name=who)
    lines = [ASSIST_ON.format(name=who), "• " + ASSIST_DRAFTS, "• " + ASSIST_SAVED]
    if previous == modes.MODE_IGNORE:
        lines.append("• " + owner_texts.OBSERVE_GAP[locale])
    if not (await service.active_consent(session, account.id, kind="ai_managed")).granted:
        lines.append("• " + ASSIST_AI_OFF)
    return "\n".join(lines)


# ------------------------------------------------------------ set_conversation_mode
class SetConversationModeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: OptionalId = Field(description="The chat, or null to use contact_id.")
    contact_id: OptionalId = Field(description="The contact whose chat it is, or null.")
    mode: Literal["observe", "ignore", "assist"] = Field(
        description=(
            "observe: store and use new messages; ignore: stop storing new messages; "
            "assist: as observe, and offer a draft for the owner's approval when new "
            "messages need a reply."
        )
    )

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        if (self.conversation_id is None) == (self.contact_id is None):
            raise ArgumentRuleError(ONE_TARGET_RULE)
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
            account = await session.get(Account, ctx.account_id)
            if account is None:  # pragma: no cover - the FK guarantees it
                raise NotInAccountError("account")
            name = (
                await session.execute(
                    select(Contact.display_name)
                    .where(Contact.account_id == ctx.account_id)
                    .where(Contact.id == change.contact_id)
                )
            ).scalar_one_or_none()
            text = "🔧 " + await mode_change_text(
                session, account, previous=change.previous, current=change.current, name=name
            )
            echo = await queue_echo(ctx, session, change.audit_id, text)
    data: dict[str, object] = {
        "conversation_id": change.conversation_id,
        "contact_id": change.contact_id,
        "previous_mode": change.previous,
        "mode": change.current,
        "effect": mode_effect(change.current, previous=change.previous),
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
    """For the confirmation card (owner-facing): whose chat it is (S1) and
    what the mode would do from the chat's mode now (C3 review)."""
    conversation_id = args.get("conversation_id")
    contact_id = args.get("contact_id")
    if isinstance(contact_id, int):
        await require_contacts(session, account_id, [contact_id])
        conversation_id = await modes.conversation_for_contact(session, account_id, contact_id)
    if not isinstance(conversation_id, int):
        return None
    name = await contact_of_chat(session, account_id, conversation_id)
    current = (
        await session.execute(
            select(Conversation.mode)
            .where(Conversation.account_id == account_id)
            .where(Conversation.id == conversation_id)
        )
    ).scalar_one_or_none()
    effect = mode_effect(str(args.get("mode")), previous=current)
    return f"the chat with {name}: {effect}"


SET_CONVERSATION_MODE = ToolDefinition(
    name="set_conversation_mode",
    description=(
        "Set one chat to observe (store and use new messages), ignore (stop storing "
        "new messages; what is stored stays) or assist (as observe, and offer drafts for "
        "the owner's approval when new messages need a reply; nothing is sent without "
        "approval). The owner sees it with an Undo button. Chats with bots stay ignored."
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
        raise ArgumentRuleError("timezone must be an IANA name such as Asia/Tashkent")
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
            raise ArgumentRuleError("give at least one setting to change")
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
            raise NotInAccountError("account")
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
            # A retention period has no Undo (M03 D85): the owner changes it
            # again in /settings, where a shorter one asks first.
            undoable = not preferences.changes_retention(change.changes)
            if not undoable:
                lines.append(RETENTION_NO_UNDO)
            echo = await queue_echo(
                ctx,
                session,
                change.audit_id,
                "⚙️ Settings changed\n" + "\n".join(lines),
                undo=undoable,
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


#: Said with a confirmed retention change (M03 D85).
RETENTION_NO_UNDO = (
    "A retention change has no Undo: change it again in /settings → Retention…. A "
    "shorter period deletes older data at the next hourly cleanup; with a longer one, "
    "data already deleted does not come back."
)


async def describe_preferences(
    session: AsyncSession, account_id: int, args: Mapping[str, Any]
) -> str | None:
    """For the confirmation card: what a retention value does compared with
    the period in force now — only a shorter one deletes (review item 12)."""
    account = await session.get(Account, account_id)
    settings = account.settings if account is not None else None
    lines = []
    for key, field in preferences.FIELD_RETENTION.items():
        days = args.get(field)
        if not isinstance(days, int) or isinstance(days, bool):
            continue
        label = retention_policy.CLASSES[key].label
        now = retention_policy.days(settings, key)
        if days < now:
            lines.append(
                f"{label}: {now} → {days} days. Anything older than {days} days is "
                "deleted at the next hourly cleanup and cannot be restored."
            )
        elif days > now:
            lines.append(
                f"{label}: {now} → {days} days. Nothing is deleted; data already deleted "
                "does not come back."
            )
        else:
            lines.append(f"{label}: already {days} days; nothing changes.")
    return " ".join(lines) or None


UPDATE_PREFERENCES = ToolDefinition(
    name="update_preferences",
    description=(
        "Change the owner's settings: preferred answer language, timezone, the mode "
        "new chats start in, the default reply language, tone and style for replies "
        "to contacts, and how long messages, this chat and raw updates are kept "
        "(shorter only). The owner sees the change with an Undo button; a retention "
        "change needs the owner's tap on a card first and has no Undo. Free-text "
        "style wishes are not settings: save them with remember."
    ),
    args_model=UpdatePreferencesArgs,
    capability=CapabilityClass.CONFIGURE,
    handler=update_preferences_tool,
    release_flag=PREFERENCES_FLAG,
    confirm_if=_sets_retention,
    describe=describe_preferences,
)
