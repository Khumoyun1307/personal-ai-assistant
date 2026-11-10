"""Conversation panel buttons and the /settings panel (J12, FR-CTL-10; S03.3).

Deterministic control-chat UI — no model involved, so it works when the
provider is down. Every button is a card (`pending_inputs` with a
single-use token, owner-scoped, `accounts/service.py::claim_card`); a press
that keeps the message useful re-arms it with a fresh token. Telegram calls
of a press run after the commit (AGENTS §13).

- **Conversation panel** (`/start bizChat<id>` from "Manage Bot", or a chat
  picked in a /settings list): summary, notes, mode, and Observe / Ignore
  buttons. Observe applies at once. Ignore asks first, in place: new
  messages will not be saved, what was saved before stays. After a change
  the panel opens with a receipt of what the mode does, or "nothing to
  change" (M03 fix round C3, D68; texts in `agent/owner_texts.py`).
- **/settings**: language and timezone (as before), the default mode for new
  chats (Observe / Ignore), and the chats listed by mode, paginated; a chat
  opens its panel.

S03.5 adds the data controls (FR-PRV-03/04, FR-CTL-10):

- the Ignore confirmation's "Ignore + delete data" (`IGNORE_DELETE_OPTION`):
  Ignore, and delete what is stored from the chat, in one transaction;
- the panel's "Delete stored data…": asks in place — this chat's data, or
  the chat and the notes about the person, or Cancel;
- /settings: the reply language, tone and style; retention per data class
  (a shorter period asks first: it deletes at the next sweep); "Delete
  everything…" (asks first; also `/privacy delete`).

Every deletion is the owner's own tap on a card that says what goes and
that it cannot be undone (`privacy/deletion.py`).

M03 fix round C4 adds the language and the time zone (FR-ONB-01):

- the onboarding language card (`/start`, `handlers.send_language_card`):
  a tap saves the language through `accounts/preferences.py` (validated
  and audited like every settings change), edits the card to a short "saved"
  line and, without `ai_control`, sends the consent question in that
  language, armed only once it arrived;
- the onboarding time-zone card (`handlers.offer_timezone`): a tap saves
  the zone the same way;
- /settings → Language… opens the same four options on a card of its own.

These screens and operational settings use the owner's language through
the shared locale resolver and `core/control_texts.json` (stabilization I28).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import preferences, service
from assistant.accounts.models import Account, PendingInput
from assistant.agent import owner_texts
from assistant.agent.locales import SupportedLocale, supported_locale
from assistant.agent.tools.modes import mode_change_text
from assistant.conversations import modes, priority, visibility
from assistant.conversations.visibility import ConversationPanel
from assistant.core.clock import Clock
from assistant.core.control_texts import buttons as localized_buttons
from assistant.core.control_texts import deletion_text
from assistant.core.control_texts import text as operational_text
from assistant.core.jobs import JobScheduler
from assistant.db.uow import after_commit, transaction
from assistant.inbox.notification_feedback import list_mutes, reset_mutes
from assistant.privacy import deletion, retention_policy
from assistant.privacy.consent_prompts import render_consent_prompt
from assistant.telegram.control import onboarding_texts as texts
from assistant.telegram.control.cards import Rows, rows_keyboard
from assistant.telegram.control.handlers import (
    AI_PROCESSORS,
    ONBOARDING_LANGUAGE_CARD,
    ONBOARDING_TIMEZONE_CARD,
    CardPress,
    HandlerResult,
    IncomingMessage,
    arm_consent_question,
    local_time,
    queue_deletion,
    render_panel,
)
from assistant.telegram.gateway import ControlChat, OutboundGateway, SendResult
from assistant.telegram.render import escape

PANEL_CARD = "conv_panel"
IGNORE_CARD = "conv_ignore"
SETTINGS_CARD = "settings"
LIST_CARD = "chat_list"
#: S03.5: the panel's delete screen, the reply and retention screens, the
#: retention confirmation and "delete everything".
DELETE_CARD = "conv_delete"
REPLY_CARD = "set_reply"
RETENTION_CARD = "set_keep"
RETENTION_CONFIRM_CARD = "keep_ok"
DELETE_ALL_CARD = "del_all"
#: C4: /settings → Language…
LANGUAGE_CARD = "set_lang"
#: S05.2P: /settings → Alerts & digest…
ALERTS_CARD = "set_alerts"
MUTES_CARD = "set_mutes"
#: The card kinds this module answers.
KINDS = frozenset(
    {
        PANEL_CARD,
        IGNORE_CARD,
        SETTINGS_CARD,
        LIST_CARD,
        DELETE_CARD,
        REPLY_CARD,
        RETENTION_CARD,
        RETENTION_CONFIRM_CARD,
        DELETE_ALL_CARD,
        LANGUAGE_CARD,
        ALERTS_CARD,
        MUTES_CARD,
        ONBOARDING_LANGUAGE_CARD,
        ONBOARDING_TIMEZONE_CARD,
    }
)

#: S05.2P: the panel's contact-priority row, as `op → (label, priority)`
#: (FR-TRI-04; the values are `conversations/priority.PRIORITIES`).
PRIORITY_OPS = {
    "p_vip": ("VIP", "vip"),
    "p_normal": ("Normal", "normal"),
    "p_low": ("Low", "low"),
}

#: Chats per /settings list page.
PAGE_SIZE = 8
#: A button label is cut here (Telegram shows short labels anyway).
LABEL_CHARS = 32

#: "Also delete existing data" on the Ignore confirmation (S03.5), as
#: `(label, op)`: Ignore, and delete what is stored from the chat.
IGNORE_DELETE_OPTION: tuple[str, str] | None = ("Ignore + delete data", "confirmdel")

MODE_LABELS = {"observe": "Observe", "ignore": "Ignore", "assist": "Assist"}

CANNOT_UNDO = "This cannot be undone."
DELETE_ALL_TEXT = (
    "<b>Delete everything?</b>\n\n"
    "This deletes all stored content: every message, name, summary and note (yours "
    "too), the history of this chat and the raw updates.\n\n"
    "Kept: your account, settings, AI consents and the connection, so I keep observing "
    "your chats from now on (to stop that: /privacy revoke, or remove me under "
    "Chat Automation). To delete one chat only, open its panel from /settings. "
    + CANNOT_UNDO
    + " "
    + deletion.ARRIVING_NOTE
    + " "
    + deletion.BACKUP_NOTE
)


def keyboard(kind: str, token: str, rows: Rows) -> InlineKeyboardMarkup:
    return rows_keyboard(kind, token, rows)


@dataclass(frozen=True, slots=True)
class Screen:
    """A message with buttons: its text and a card already created in the
    transaction (`card_id`, `kind`, `token`, rows)."""

    text: str
    card_id: int | None = None
    kind: str | None = None
    token: str | None = None
    rows: Rows | None = None

    def markup(self) -> InlineKeyboardMarkup | None:
        if self.kind is None or self.token is None or not self.rows:
            return None
        return keyboard(self.kind, self.token, self.rows)


async def _screen(
    session: AsyncSession,
    clock: Clock,
    account_id: int,
    kind: str,
    text: str,
    rows: Rows | None,
    ref_id: int | None = None,
) -> Screen:
    if not rows:
        return Screen(text)
    card = await service.create_card(
        session, account_id=account_id, kind=kind, clock=clock, ref_id=ref_id
    )
    assert card.token is not None
    return Screen(text, card.id, kind, card.token, rows)


async def _record(
    factory: async_sessionmaker[AsyncSession], screen: Screen, result: SendResult
) -> None:
    """A shown card remembers its message; one that never arrived is removed."""
    if screen.card_id is None:
        return
    async with transaction(factory) as uow:
        card = await uow.session.get(PendingInput, screen.card_id)
        if card is None:
            return
        if result.ok and result.message_id is not None:
            card.prompt_message_id = result.message_id
        elif not result.ok:
            await uow.session.delete(card)


async def _send_now(
    session: AsyncSession, gateway: OutboundGateway, target: ControlChat, screen: Screen
) -> SendResult:
    """Send inside a command handler (as the other command handlers do)."""
    result = await gateway.send_text(target, screen.text, keyboard=screen.markup())
    if screen.card_id is not None:
        card = await session.get(PendingInput, screen.card_id)
        if card is not None:
            if result.ok:
                card.prompt_message_id = result.message_id
            else:
                await session.delete(card)
        await session.flush()
    return result


def _after_press(
    session: AsyncSession,
    gateway: OutboundGateway,
    press: CardPress,
    target: ControlChat,
    *,
    answer: str,
    edit: Screen | None = None,
    send: Screen | None = None,
) -> None:
    """After the commit: answer the press, edit the pressed message, send a new one."""
    message_id = press.message_id

    async def run(factory: async_sessionmaker[AsyncSession]) -> None:
        await gateway.answer_callback(press.callback_id, answer)
        if edit is not None:
            if message_id is None:
                result = await gateway.send_text(target, edit.text, keyboard=edit.markup())
            else:
                result = await gateway.edit_control_text(
                    target, message_id, edit.text, keyboard=edit.markup()
                )
            await _record(factory, edit, result)
        if send is not None:
            result = await gateway.send_text(target, send.text, keyboard=send.markup())
            await _record(factory, send, result)

    after_commit(session, run)


# ------------------------------------------------------------ the conversation panel
def panel_rows(panel: ConversationPanel) -> Rows | None:
    """Observe / Ignore / Assist, then contact priority, both ticked.
    None for a bot chat: it stays ignored (FR-OBS-04)."""
    if panel.is_bot:
        return None
    rows: Rows = [
        [
            (MODE_LABELS[mode] + (" ✓" if panel.mode == mode else ""), mode)
            for mode in modes.SETTABLE_MODES
        ],
        [("Delete stored data…", OP_DELETE)],
    ]
    if panel.contact_id is not None:  # nothing to set a priority on otherwise
        rows.append(
            [
                (_ticked(label, panel.contact_priority == value), op)
                for op, (label, value) in PRIORITY_OPS.items()
            ]
        )
    return rows


OP_DELETE = "delete"


async def panel_screen(
    session: AsyncSession,
    clock: Clock,
    account: Account,
    panel: ConversationPanel,
    *,
    deleting: bool = False,
    change: tuple[str, str] | None = None,
) -> Screen:
    """The panel with its buttons. `deleting`: a deletion of the chat's data
    was just queued, so the panel shows none of it (`render_panel`).
    `change`: the mode `(before, after)` a tap just set; the panel opens with
    what that did, or "nothing to change" (C3). During a deletion the
    receipt leaves out the name, as the panel does."""
    notice = None
    if change is not None:
        before, after = change
        notice = await mode_change_text(
            session,
            account,
            previous=before,
            current=after,
            name=None if deleting else panel.display_name,
            deleting=deleting,
        )
        if before != after:
            notice = "✅ " + notice
    text = render_panel(
        panel,
        timezone=account.timezone,
        deleting=deleting,
        locale=supported_locale(account.locale_pref),
        notice=notice,
    )
    if panel.mode == modes.MODE_ASSIST and not deleting:
        text += "\n\n" + ASSIST_PANEL_LINE
    rows = panel_rows(panel)
    return await _screen(
        session, clock, account.id, PANEL_CARD, text, rows, ref_id=panel.conversation_id
    )


#: The conversation panel's line while Assist is on (S04.2 §3.7).
ASSIST_PANEL_LINE = (
    "✍️ <b>Assist</b>: when new messages need a reply, I draft one and show it to you "
    "here; nothing is sent without your approval."
)


def ignore_confirm_text(account: Account, name: str) -> str:
    """The Ignore confirmation (`name` escaped): what Ignore does, in the
    owner's locale, then what "Ignore + delete data" deletes."""
    effects = owner_texts.ignore_effects(
        supported_locale(account.locale_pref),
        days=retention_policy.days(account.settings, retention_policy.MESSAGES),
    )
    return "\n\n".join(
        (
            operational_text("ignore_question", account.locale_pref, name=name),
            "\n".join(effects),
            deletion_text("ignore_delete_body", account.locale_pref, name=name),
        )
    )


#: The press's toast (English, like the buttons) once the mode is set.
MODE_SET_ANSWER = {
    modes.MODE_OBSERVE: "Observe is on",
    modes.MODE_IGNORE: "Ignore is on",
    modes.MODE_ASSIST: "Assist is on",
}
NOTHING_CHANGED = "Nothing changed"


async def handle_panel_command(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    message: IncomingMessage,
    panel: ConversationPanel | None,
) -> HandlerResult:
    """`/start bizChat<id>` (TF §12 LV-10). Unknown, foreign and malformed
    ids all get the same reply (AGENTS §6.7)."""
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)
    if panel is None:
        result = await gateway.send_text(
            target, operational_text("panel_unknown", account.locale_pref)
        )
        return HandlerResult(True, "panel", result, note="unknown_chat")
    screen = await panel_screen(session, clock, account, panel)
    result = await _send_now(session, gateway, target, screen)
    return HandlerResult(True, "panel", result, note="shown")


async def _load_panel(
    session: AsyncSession, clock: Clock, account: Account, conversation_id: int
) -> ConversationPanel | None:
    return await visibility.conversation_panel(
        session, account_id=account.id, conversation_id=conversation_id, now=clock.now()
    )


async def _press_panel(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
    conversation_id: int,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    panel = await _load_panel(session, clock, account, conversation_id)
    if panel is None or press.op not in (*modes.SETTABLE_MODES, OP_DELETE, *PRIORITY_OPS):
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="no_longer_active")
    if press.op in PRIORITY_OPS:
        if panel.is_bot or panel.contact_id is None:  # no buttons were offered
            _after_press(
                session,
                gateway,
                press,
                target,
                answer=operational_text("expired", account.locale_pref),
            )
            return HandlerResult(False, "card", None, note="no_priority")
        try:
            saved = await priority.set_priority(
                session,
                account_id=account.id,
                contact_id=panel.contact_id,
                priority=PRIORITY_OPS[press.op][1],
                actor="owner",
                clock=clock,
            )
        except LookupError:  # `NotInAccountError`: not this account's contact
            _after_press(
                session,
                gateway,
                press,
                target,
                answer=operational_text("expired", account.locale_pref),
            )
            return HandlerResult(False, "card", None, note="no_longer_active")
        panel = await _load_panel(session, clock, account, conversation_id)
        if panel is None:
            _after_press(
                session,
                gateway,
                press,
                target,
                answer=operational_text("expired", account.locale_pref),
            )
            return HandlerResult(False, "card", None, note="no_longer_active")
        screen = await panel_screen(session, clock, account, panel)
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("saved", account.locale_pref),
            edit=screen,
        )
        return HandlerResult(True, "card", None, note="priority" if saved.changed else "unchanged")
    if press.op == OP_DELETE:
        if panel.is_bot:  # a bot chat is purged already and has no buttons
            _after_press(
                session,
                gateway,
                press,
                target,
                answer=operational_text("expired", account.locale_pref),
            )
            return HandlerResult(False, "card", None, note="bot_chat")
        # Ask first, in place: what goes, and that it cannot be undone.
        name = escape(panel.display_name or "this contact")
        screen = await _screen(
            session,
            clock,
            account.id,
            DELETE_CARD,
            deletion_text("delete_chat_body", account.locale_pref, name=name),
            [
                localized_buttons(
                    [
                        ("chat_data_button", "chat"),
                        ("chat_notes_button", "contact"),
                        ("cancel", "cancel"),
                    ],
                    account.locale_pref,
                )
            ],
            ref_id=conversation_id,
        )
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("confirm_delete", account.locale_pref),
            edit=screen,
        )
        return HandlerResult(True, "card", None, note="delete_asked")
    if press.op == modes.MODE_IGNORE and panel.mode != modes.MODE_IGNORE:
        # Ask first, in place of the panel (the owner's own tap still
        # needs the effect spelled out: new messages will not be stored).
        name = escape(panel.display_name or "this contact")
        screen = await _screen(
            session,
            clock,
            account.id,
            IGNORE_CARD,
            ignore_confirm_text(account, name),
            _ignore_confirm(account.locale_pref),
            ref_id=conversation_id,
        )
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("confirm_ignore", account.locale_pref),
            edit=screen,
        )
        return HandlerResult(True, "card", None, note="ignore_asked")
    change = (panel.mode, panel.mode)
    if press.op != panel.mode:
        try:
            changed = await modes.set_mode(
                session,
                account_id=account.id,
                conversation_id=conversation_id,
                mode=press.op,
                actor="owner",
                clock=clock,
            )
        except modes.ModeError:
            _after_press(session, gateway, press, target, answer="Bot chats stay ignored")
            return HandlerResult(False, "card", None, note="bot_chat")
        change = (changed.previous, changed.current)
        panel = await _load_panel(session, clock, account, conversation_id)
        assert panel is not None
    screen = await panel_screen(session, clock, account, panel, change=change)
    moved = change[0] != change[1]
    answer = MODE_SET_ANSWER[change[1]] if moved else NOTHING_CHANGED
    _after_press(session, gateway, press, target, answer=answer, edit=screen)
    return HandlerResult(True, "card", None, note=press.op if moved else "unchanged")


def _ignore_confirm(locale: str | None = None) -> Rows:
    row = [("ignore_button", "confirm"), ("cancel", "cancel")]
    if IGNORE_DELETE_OPTION is not None:  # S03.5
        row.insert(1, ("ignore_delete_button", IGNORE_DELETE_OPTION[1]))
    return [localized_buttons(row, locale)]


async def _queue_deletion(
    session: AsyncSession,
    scheduler: JobScheduler | None,
    account_id: int,
    scope: str,
    subject_id: int,
    card_id: int | None,
) -> Screen:
    """`handlers.queue_deletion` (M03 D83), as a screen to send."""
    return Screen(await queue_deletion(session, scheduler, account_id, scope, subject_id, card_id))


async def _press_ignore(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
    conversation_id: int,
    scheduler: JobScheduler | None = None,
    card_id: int | None = None,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    delete_op = IGNORE_DELETE_OPTION[1] if IGNORE_DELETE_OPTION is not None else None
    if press.op not in ("confirm", "cancel", delete_op):
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="unknown_op")
    note = "cancelled"
    receipt = None
    change = None
    if press.op in ("confirm", delete_op):
        try:
            # Ignore applies at once: nothing new is stored from now on.
            changed = await modes.set_mode(
                session,
                account_id=account.id,
                conversation_id=conversation_id,
                mode=modes.MODE_IGNORE,
                actor="owner",
                clock=clock,
            )
        except LookupError:
            _after_press(
                session,
                gateway,
                press,
                target,
                answer=operational_text("expired", account.locale_pref),
            )
            return HandlerResult(False, "card", None, note="no_longer_active")
        change = (changed.previous, changed.current)
        if press.op == delete_op:
            # Queued in this transaction: committed with the Ignore, or neither.
            receipt = await _queue_deletion(
                session,
                scheduler,
                account.id,
                deletion.SCOPE_CONVERSATION,
                conversation_id,
                card_id,
            )
        # A stale confirmation (set to Ignore another way meanwhile) changed
        # nothing, and says so (D68, §6 invariant 11).
        note = "ignored" if changed.changed else "unchanged"
        if receipt is not None:
            note = "ignored_and_deleting" if changed.changed else "deleting"
    panel = await _load_panel(session, clock, account, conversation_id)
    if panel is None:
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="no_longer_active")
    screen = await panel_screen(
        session, clock, account, panel, deleting=receipt is not None, change=change
    )
    answer = {
        "ignored": MODE_SET_ANSWER[modes.MODE_IGNORE],
        "ignored_and_deleting": "Ignore is on; deleting its data",
        "deleting": "Deleting its data",
    }.get(note, NOTHING_CHANGED)
    _after_press(session, gateway, press, target, answer=answer, edit=screen, send=receipt)
    return HandlerResult(True, "card", None, note=note)


async def _press_delete(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
    conversation_id: int,
    scheduler: JobScheduler | None = None,
    card_id: int | None = None,
) -> HandlerResult:
    """The panel's delete screen: this chat's data, the chat and the notes
    about the person, or Cancel (FR-PRV-04). The owner's own second tap."""
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    if press.op not in ("chat", "contact", "cancel"):
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="unknown_op")
    receipt = None
    try:
        if press.op == "chat":
            receipt = await _queue_deletion(
                session,
                scheduler,
                account.id,
                deletion.SCOPE_CONVERSATION,
                conversation_id,
                card_id,
            )
        elif press.op == "contact":
            contact_id = await modes.contact_of(session, account.id, conversation_id)
            receipt = await _queue_deletion(
                session, scheduler, account.id, deletion.SCOPE_CONTACT, contact_id, card_id
            )
    except LookupError:
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="no_longer_active")
    panel = await _load_panel(session, clock, account, conversation_id)
    if panel is None:
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="no_longer_active")
    screen = await panel_screen(session, clock, account, panel, deleting=receipt is not None)
    answer = operational_text(
        "deleting" if receipt is not None else "nothing_deleted", account.locale_pref
    )
    _after_press(session, gateway, press, target, answer=answer, edit=screen, send=receipt)
    return HandlerResult(True, "card", None, note=press.op if receipt else "cancelled")


# ------------------------------------------------------------ /settings
async def settings_screen(session: AsyncSession, clock: Clock, account: Account) -> Screen:
    counts = await modes.counts_by_mode(session, account.id)
    default = preferences.default_mode(account)
    now = preferences.current(account)
    keep = {key: retention_policy.days(account.settings, key) for key in retention_policy.CLASSES}

    def wording(key: str, **values: object) -> str:
        return operational_text(key, account.locale_pref, **values)

    text = (
        wording("settings_heading")
        + "\n\n"
        + wording(
            "settings_body",
            language=escape(texts.language_label(account.locale_pref)),
            zone=escape(account.timezone),
            mode=wording(default),
            observed=counts.get("observe", 0),
            ignored=counts.get("ignore", 0),
            replies=_reply_summary(now, account.locale_pref),
            messages=keep[retention_policy.MESSAGES],
            control=keep[retention_policy.CONTROL_CHAT],
            raw=keep[retention_policy.RAW_UPDATES],
        )
    )
    rows: Rows = [
        [
            (_ticked(wording("new_chats", mode=wording(m)), default == m), f"d{m}")
            for m in preferences.SETTABLE_DEFAULT_MODES
        ],
        [
            (wording("observed", count=counts.get("observe", 0)), "lobserve"),
            (wording("ignored", count=counts.get("ignore", 0)), "lignore"),
        ],
        localized_buttons(
            [("language", OP_LANGUAGE), ("replies", "replies"), ("retention", "retention")],
            account.locale_pref,
        ),
        localized_buttons([("alerts", "alerts")], account.locale_pref),
        localized_buttons([("delete_all_more", "delall")], account.locale_pref),
    ]
    return await _screen(session, clock, account.id, SETTINGS_CARD, text, rows)


# ------------------------------------------------------------ language and time zone (C4)
OP_LANGUAGE = "lang"
OP_BACK = "back"


async def language_screen(session: AsyncSession, clock: Clock, account: Account) -> Screen:
    """/settings → Language…: the onboarding picker's four options in their
    fixed order, the current one ticked."""
    chosen = texts.chosen_locale(account.locale_pref)
    order = texts.DEFAULT_ORDER
    rows = [
        *texts.picker_rows(order, ticked=chosen),
        [(operational_text("back", account.locale_pref), OP_BACK)],
    ]
    return await _screen(session, clock, account.id, LANGUAGE_CARD, texts.picker_text(order), rows)


async def _save_language(
    session: AsyncSession, clock: Clock, account: Account, op: str
) -> SupportedLocale | None:
    """The tapped language, saved like every preference change (validated,
    audited); None for an op no picker offers."""
    locale = texts.LANGUAGE_BY_OP.get(op)
    if locale is not None:
        await preferences.update(session, account, language=locale, actor="owner", clock=clock)
    return locale


async def _press_language(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    if press.op == OP_BACK:
        screen = await settings_screen(session, clock, account)
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("settings", account.locale_pref),
            edit=screen,
        )
        return HandlerResult(True, "card", None, note="back")
    locale = await _save_language(session, clock, account, press.op)
    if locale is None:
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="unknown_op")
    screen = await settings_screen(session, clock, account)
    _after_press(session, gateway, press, target, answer=texts.LANGUAGE_LABELS[locale], edit=screen)
    return HandlerResult(True, "card", None, note="language")


async def _press_onboarding_language(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    """The /start language card: save, say so in place, and — without
    `ai_control` — ask for consent in that language once committed. The
    question is armed only when Telegram took it (SEC-02)."""
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    locale = await _save_language(session, clock, account, press.op)
    if locale is None:
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="unknown_op")
    # Any other picker (an older /start) goes too: one language choice.
    await service.close_cards(session, account_id=account.id, kind=ONBOARDING_LANGUAGE_CARD)
    _after_press(
        session,
        gateway,
        press,
        target,
        answer=texts.LANGUAGE_LABELS[locale],
        edit=Screen(texts.LANGUAGE_SAVED[locale]),
    )
    consent = await service.active_consent(session, account.id, kind=service.CONSENT_AI_CONTROL)
    if consent.granted:
        return HandlerResult(True, "card", None, note="language")
    account_id = account.id
    prompt = render_consent_prompt(account, texts.consent_prompt(locale, AI_PROCESSORS))

    async def ask(factory: async_sessionmaker[AsyncSession]) -> None:
        result = await gateway.send_text(target, prompt)
        if not result.ok:
            return  # never shown: nothing for a "yes" to answer
        async with transaction(factory) as uow:
            await arm_consent_question(uow.session, clock, account_id, result.message_id)

    after_commit(session, ask)
    return HandlerResult(True, "card", None, note="language_consent_asked")


async def _press_onboarding_timezone(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    zone = texts.TIMEZONE_BY_OP.get(press.op)
    if zone is None:
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="unknown_op")
    await preferences.update(session, account, timezone=zone, actor="owner", clock=clock)
    locale = supported_locale(account.locale_pref)
    saved = texts.TIMEZONE_SAVED[locale].format(zone=escape(account.timezone))
    _after_press(session, gateway, press, target, answer=zone, edit=Screen(saved))
    return HandlerResult(True, "card", None, note="timezone")


# ------------------------------------------------------------ replies (S03.5)
REPLY_LANGUAGES = (
    (preferences.REPLY_LANGUAGE_MATCH, "Same as chat"),
    ("uz", "Uzbek"),
    ("ru", "Russian"),
    ("en", "English"),
)
TONE_LABELS = {"neutral": "Neutral", "friendly": "Friendly", "formal": "Formal"}
STYLE_LABELS = {"brief": "Brief", "detailed": "Detailed"}


def _reply_summary(values: dict[str, object], locale: str | None = None) -> str:
    language = values[preferences.FIELD_REPLY_LANGUAGE]
    shown = dict(REPLY_LANGUAGES).get(str(language), str(language))
    tone = TONE_LABELS.get(str(values[preferences.FIELD_REPLY_TONE]), "")
    style = STYLE_LABELS.get(str(values[preferences.FIELD_REPLY_STYLE]), "")
    return ", ".join(
        [
            _reply_label(shown, locale),
            *(_reply_label(label, locale).lower() for label in (tone, style)),
        ]
    )


def _reply_label(label: str, locale: str | None) -> str:
    key = {
        "Same as chat": "same_chat",
        "Uzbek": "uzbek",
        "Russian": "russian",
        "English": "english",
        **{value: key for key, value in TONE_LABELS.items()},
        **{value: key for key, value in STYLE_LABELS.items()},
    }.get(label)
    return operational_text(key, locale) if key else escape(label)


def _ticked(label: str, chosen: bool) -> str:
    return label + (" ✓" if chosen else "")


async def reply_screen(session: AsyncSession, clock: Clock, account: Account) -> Screen:
    now = preferences.current(account)
    text = operational_text("reply_body", account.locale_pref)
    rows: Rows = [
        [
            (
                _ticked(
                    _reply_label(label, account.locale_pref),
                    now[preferences.FIELD_REPLY_LANGUAGE] == code,
                ),
                f"l_{code}",
            )
            for code, label in REPLY_LANGUAGES
        ],
        [
            (
                _ticked(
                    _reply_label(label, account.locale_pref),
                    now[preferences.FIELD_REPLY_TONE] == tone,
                ),
                f"t_{tone}",
            )
            for tone, label in TONE_LABELS.items()
        ],
        [
            (
                _ticked(
                    _reply_label(label, account.locale_pref),
                    now[preferences.FIELD_REPLY_STYLE] == style,
                ),
                f"s_{style}",
            )
            for style, label in STYLE_LABELS.items()
        ],
        [(operational_text("back", account.locale_pref), "back")],
    ]
    return await _screen(session, clock, account.id, REPLY_CARD, text, rows)


async def _press_reply(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    kind, _, value = press.op.partition("_")
    changes: dict[str, str] = {}
    if kind == "l" and value in dict(REPLY_LANGUAGES):
        changes["reply_language"] = value
    elif kind == "t" and value in TONE_LABELS:
        changes["reply_tone"] = value
    elif kind == "s" and value in STYLE_LABELS:
        changes["reply_style"] = value
    elif press.op == "back":
        screen = await settings_screen(session, clock, account)
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("settings", account.locale_pref),
            edit=screen,
        )
        return HandlerResult(True, "card", None, note="back")
    else:
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="unknown_op")
    change = await preferences.update(
        session,
        account,
        reply_language=changes.get("reply_language"),
        reply_tone=changes.get("reply_tone"),
        reply_style=changes.get("reply_style"),
        actor="owner",
        clock=clock,
    )
    screen = await reply_screen(session, clock, account)
    _after_press(
        session,
        gateway,
        press,
        target,
        answer=operational_text("saved", account.locale_pref),
        edit=screen,
    )
    return HandlerResult(True, "card", None, note="reply" if change.audit_id else "unchanged")


# ------------------------------------------------------------ alerts and digest (S05.2P)
#: `(label, op, stored value)` per button of the alerts screen. The values
#: are what `accounts/preferences.py` stores and validates (F0-interfaces §10:
#: quiet hours as "HH:MM-HH:MM" or "off", the digest as "HH:MM" or "off");
#: ops stay short, the whole callback data is at most 64 bytes.
AlertChoice = tuple[str, str, str | int | bool]
QUIET_CHOICES: tuple[AlertChoice, ...] = (
    ("Off", "q_off", "off"),
    ("22–07", "q_2207", "22:00-07:00"),  # noqa: RUF001 (en dash: a range)
    ("23–08", "q_2308", "23:00-08:00"),  # noqa: RUF001
    ("00–08", "q_0008", "00:00-08:00"),  # noqa: RUF001
)
BYPASS_CHOICES: tuple[AlertChoice, ...] = (
    ("Bypass on", "b_on", True),
    ("Bypass off", "b_off", False),
)
DIGEST_CHOICES: tuple[AlertChoice, ...] = (
    ("Off", "d_off", "off"),
    ("08:00", "d_0800", "08:00"),
    ("12:00", "d_1200", "12:00"),
    ("18:00", "d_1800", "18:00"),
    ("21:00", "d_2100", "21:00"),
)
CAP_CHOICES: tuple[AlertChoice, ...] = (
    ("3/h", "c_3", 3),
    ("6/h", "c_6", 6),
    ("10/h", "c_10", 10),
    ("20/h", "c_20", 20),
)
#: The preference each row sets (its `preferences.update` keyword).
ALERT_ROWS: tuple[tuple[str, tuple[AlertChoice, ...]], ...] = (
    ("quiet_hours", QUIET_CHOICES),
    ("urgent_bypass", BYPASS_CHOICES),
    ("digest_time", DIGEST_CHOICES),
    ("alert_cap_per_hour", CAP_CHOICES),
)
#: A button's op → the one `preferences.update` change it makes.
ALERT_OPS: dict[str, dict[str, Any]] = {
    op: {field: value} for field, choices in ALERT_ROWS for _, op, value in choices
}


async def alerts_screen(session: AsyncSession, clock: Clock, account: Account) -> Screen:
    """/settings → Alerts & digest…: quiet hours, urgent bypass, the digest
    time and the hourly cap, each current value ticked. Only stored settings
    are shown; the digest itself follows the stored time (S05.3b)."""
    now = preferences.current(account)
    locale = account.locale_pref
    quiet = now["quiet_hours"]
    held = operational_text("urgent_wait", locale) if quiet != "off" else ""
    text = (
        operational_text("alerts_heading", locale)
        + "\n\n"
        + operational_text(
            "alerts_body",
            locale,
            quiet=operational_text("off", locale) if quiet == "off" else escape(str(quiet)),
            held=held,
            bypass=operational_text("on" if now["urgent_bypass"] else "off", locale),
            digest=(
                operational_text("off", locale)
                if now["digest_time"] == "off"
                else escape(str(now["digest_time"]))
            ),
            cap=now["alert_cap_per_hour"],
            zone=escape(account.timezone),
        )
    )
    label_keys = {"Off": "off_button", "Bypass on": "bypass_on", "Bypass off": "bypass_off"}

    def label_for(field: str, label: str, value: str | int | bool) -> str:
        if field == "alert_cap_per_hour":
            return operational_text("hourly_cap", locale, count=value)
        return operational_text(label_keys[label], locale) if label in label_keys else label

    rows: Rows = [
        [
            (
                _ticked(
                    label_for(field, label, value),
                    value == now[field],
                ),
                op,
            )
            for label, op, value in choices
        ]
        for field, choices in ALERT_ROWS
    ]
    rows.append([(operational_text("mutes_button", locale), "mutes")])
    rows.append([(operational_text("back", locale), OP_BACK)])
    return await _screen(session, clock, account.id, ALERTS_CARD, text, rows)


async def _press_alerts(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    if press.op == "mutes":
        screen = await mutes_screen(session, clock, account)
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("mutes_title", account.locale_pref),
            edit=screen,
        )
        return HandlerResult(True, "card", None, note="mutes")
    if press.op == OP_BACK:
        screen = await settings_screen(session, clock, account)
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("settings", account.locale_pref),
            edit=screen,
        )
        return HandlerResult(True, "card", None, note="back")
    change = ALERT_OPS.get(press.op)
    if change is None:
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="unknown_op")
    saved = await preferences.update(session, account, **change, actor="owner", clock=clock)
    screen = await alerts_screen(session, clock, account)
    _after_press(
        session,
        gateway,
        press,
        target,
        answer=operational_text("saved", account.locale_pref),
        edit=screen,
    )
    return HandlerResult(True, "card", None, note="alerts" if saved.audit_id else "unchanged")


async def mutes_screen(
    session: AsyncSession, clock: Clock, account: Account, page: int = 0
) -> Screen:
    mutes = await list_mutes(session, account_id=account.id, now=clock.now())
    locale = account.locale_pref
    page = min(max(page, 0), max((len(mutes) - 1) // PAGE_SIZE, 0))
    lines = [
        f"<b>{operational_text('mutes_title', locale)}</b>",
        operational_text("mutes_body", locale),
    ]
    rows: Rows = []
    for mute in mutes[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
        label = operational_text(f"mute_kind_{mute.kind}", locale)
        scope = (
            operational_text("mute_contact", locale, id=mute.contact_id)
            if mute.contact_id is not None
            else operational_text("mute_no_contact", locale)
        )
        if mute.monitor_id is not None:
            scope += ", " + operational_text("mute_monitor", locale, id=mute.monitor_id)
        lines.append(
            f"\n{escape(label)} ({scope})\n"
            + operational_text(
                "mute_reason", locale, since=local_time(mute.since, account.timezone)
            )
        )
        rows.append(
            [
                (
                    operational_text("mute_reset", locale, label=label, scope=scope),
                    f"r{mute.notification_id}",
                )
            ]
        )
    if not mutes:
        lines.append("\n" + operational_text("mutes_empty", locale))
    if page > 0:
        rows.append([(operational_text("previous", locale), f"p{page - 1}")])
    if (page + 1) * PAGE_SIZE < len(mutes):
        rows.append([(operational_text("next", locale), f"p{page + 1}")])
    rows.append([(operational_text("mutes_reset_all", locale), "all")])
    rows.append([(operational_text("back", locale), OP_BACK)])
    return await _screen(session, clock, account.id, MUTES_CARD, "\n".join(lines), rows)


async def _press_mutes(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    if press.op == OP_BACK:
        screen = await alerts_screen(session, clock, account)
        answer = operational_text("alerts_title", account.locale_pref)
    elif press.op == "all":
        await reset_mutes(session, account_id=account.id)
        screen = await mutes_screen(session, clock, account)
        answer = operational_text("mutes_all_reset", account.locale_pref)
    elif press.op[:1] in {"r", "p"} and press.op[1:].isdigit() and len(press.op[1:]) <= 18:
        value = int(press.op[1:])
        if press.op.startswith("r"):
            if not await reset_mutes(session, account_id=account.id, notification_id=value):
                _after_press(
                    session,
                    gateway,
                    press,
                    target,
                    answer=operational_text("expired", account.locale_pref),
                )
                return HandlerResult(False, "card", None, note="unknown_mute")
            answer = operational_text("mute_reset_done", account.locale_pref)
            value = 0
        else:
            answer = operational_text("mutes_title", account.locale_pref)
        screen = await mutes_screen(session, clock, account, value)
    else:
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="unknown_op")
    _after_press(session, gateway, press, target, answer=answer, edit=screen)
    return HandlerResult(True, "card", None, note="mutes")


# ------------------------------------------------------------ retention (S03.5)
#: Op prefix per retention class (callback data stays short).
RETENTION_OPS = {
    "m": retention_policy.MESSAGES,
    "c": retention_policy.CONTROL_CHAT,
    "r": retention_policy.RAW_UPDATES,
}
RETENTION_SHORT = {"m": "Msgs", "c": "Chat", "r": "Raw"}
RETENTION_WHAT = {
    retention_policy.MESSAGES: "Message text from your chats",
    retention_policy.CONTROL_CHAT: "The history of this chat",
    retention_policy.RAW_UPDATES: "Raw Telegram updates",
}


async def retention_screen(session: AsyncSession, clock: Clock, account: Account) -> Screen:
    text = operational_text("retention_body", account.locale_pref)
    rows: Rows = []
    for prefix, key in RETENTION_OPS.items():
        cls = retention_policy.CLASSES[key]
        chosen = retention_policy.days(account.settings, key)
        rows.append(
            [
                (
                    _ticked(
                        operational_text(
                            "retention_days",
                            account.locale_pref,
                            label=operational_text(
                                {
                                    "m": "messages_short",
                                    "c": "chat_short",
                                    "r": "raw_short",
                                }[prefix],
                                account.locale_pref,
                            ),
                            days=days,
                        ),
                        days == chosen,
                    ),
                    f"{prefix}_{days}",
                )
                for days in cls.choices
            ]
        )
    rows.append([(operational_text("back", account.locale_pref), "back")])
    return await _screen(session, clock, account.id, RETENTION_CARD, text, rows)


def _retention_choice(op: str) -> tuple[str, int] | None:
    """`m_30` → (messages, 30), only for a value the panel offers."""
    prefix, _, value = op.partition("_")
    key = RETENTION_OPS.get(prefix)
    if key is None or not value.isdigit() or len(value) > 3:
        return None
    days = int(value)
    return (key, days) if days in retention_policy.CLASSES[key].choices else None


async def _apply_retention(
    session: AsyncSession, clock: Clock, account: Account, key: str, days: int
) -> bool:
    change = await preferences.update(
        session, account, retention={key: days}, actor="owner", clock=clock
    )
    return change.audit_id is not None


async def _press_retention(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    if press.op == "back":
        screen = await settings_screen(session, clock, account)
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("settings", account.locale_pref),
            edit=screen,
        )
        return HandlerResult(True, "card", None, note="back")
    choice = _retention_choice(press.op)
    if choice is None:
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="unknown_op")
    key, days = choice
    if days < retention_policy.days(account.settings, key):
        # Shorter: it deletes at the next sweep, so ask first (in place).
        text = operational_text(
            "retention_confirm",
            account.locale_pref,
            what=operational_text(
                {
                    "messages": "messages_short",
                    "control_chat": "chat_short",
                    "raw_updates": "raw_short",
                }[key],
                account.locale_pref,
            ),
            days=days,
            undo=operational_text("cannot_undo", account.locale_pref),
        )
        screen = await _screen(
            session,
            clock,
            account.id,
            RETENTION_CONFIRM_CARD,
            text,
            [
                [
                    (operational_text("keep_days", account.locale_pref, days=days), press.op),
                    (operational_text("cancel", account.locale_pref), "cancel"),
                ],
            ],
        )
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("confirm_first", account.locale_pref),
            edit=screen,
        )
        return HandlerResult(True, "card", None, note="asked")
    changed = await _apply_retention(session, clock, account, key, days)
    screen = await retention_screen(session, clock, account)
    _after_press(
        session,
        gateway,
        press,
        target,
        answer=operational_text("saved", account.locale_pref),
        edit=screen,
    )
    return HandlerResult(True, "card", None, note="retention" if changed else "unchanged")


async def _press_retention_confirm(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    note = "cancelled"
    if press.op != "cancel":
        choice = _retention_choice(press.op)
        if choice is None:
            _after_press(
                session,
                gateway,
                press,
                target,
                answer=operational_text("expired", account.locale_pref),
            )
            return HandlerResult(False, "card", None, note="unknown_op")
        await _apply_retention(session, clock, account, *choice)
        note = "retention"
    screen = await retention_screen(session, clock, account)
    answer = operational_text(
        "saved_cleanup" if note == "retention" else "nothing_changed", account.locale_pref
    )
    _after_press(session, gateway, press, target, answer=answer, edit=screen)
    return HandlerResult(True, "card", None, note=note)


# ------------------------------------------------------------ delete everything (S03.5)
async def delete_all_screen(session: AsyncSession, clock: Clock, account: Account) -> Screen:
    return await _screen(
        session,
        clock,
        account.id,
        DELETE_ALL_CARD,
        deletion_text("delete_all_body", account.locale_pref),
        [
            localized_buttons(
                [("delete_all", "everything"), ("cancel", "cancel")], account.locale_pref
            )
        ],
    )


async def handle_delete_all_command(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    message: IncomingMessage,
) -> HandlerResult:
    """`/privacy delete`: the "delete everything" card (it asks first)."""
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)
    screen = await delete_all_screen(session, clock, account)
    result = await _send_now(session, gateway, target, screen)
    return HandlerResult(True, "privacy", result, note="delete_asked")


async def _press_delete_all(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
    scheduler: JobScheduler | None = None,
    card_id: int | None = None,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    if press.op not in ("everything", "cancel"):
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="unknown_op")
    receipt = None
    if press.op == "everything":
        receipt = await _queue_deletion(
            session, scheduler, account.id, deletion.SCOPE_EVERYTHING, account.id, card_id
        )
    screen = await settings_screen(session, clock, account)
    answer = operational_text(
        "deleting" if receipt is not None else "nothing_deleted", account.locale_pref
    )
    _after_press(session, gateway, press, target, answer=answer, edit=screen, send=receipt)
    return HandlerResult(True, "card", None, note="deleted" if receipt else "cancelled")


async def handle_settings_command(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    message: IncomingMessage,
) -> HandlerResult:
    """`/settings` — deterministic, no model involved, so it always works."""
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)
    screen = await settings_screen(session, clock, account)
    result = await _send_now(session, gateway, target, screen)
    return HandlerResult(True, "settings", result)


async def list_screen(
    session: AsyncSession, clock: Clock, account: Account, mode: str, page: int
) -> Screen:
    counts = await modes.counts_by_mode(session, account.id)
    total = counts.get(mode, 0)
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(page, 1), pages)
    chats = await modes.chats_in_mode(
        session, account.id, mode, offset=(page - 1) * PAGE_SIZE, limit=PAGE_SIZE
    )
    label = {"observe": "Observed", "ignore": "Ignored", "assist": "Assisted"}.get(mode, mode)
    text = f"<b>{label} chats</b> — page {page} of {pages}"
    if not chats:
        text += "\n\nNone."
    else:
        text += "\n\nTap a chat to open its panel."
    rows: Rows = [[(_label(chat), f"o_{chat.conversation_id}")] for chat in chats]
    nav = []
    if page > 1:
        nav.append(("◀ Previous", f"p_{mode}_{page - 1}"))
    if page < pages:
        nav.append(("Next ▶", f"p_{mode}_{page + 1}"))
    if nav:
        rows.append(nav)
    return await _screen(session, clock, account.id, LIST_CARD, text, rows or None)


def _label(chat: modes.ChatEntry) -> str:
    name = chat.display_name or (f"@{chat.username}" if chat.username else "(no name)")
    return name if len(name) <= LABEL_CHARS else name[: LABEL_CHARS - 1] + "…"


async def _press_settings(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    op = press.op
    if op in {f"d{m}" for m in preferences.SETTABLE_DEFAULT_MODES}:
        change = await preferences.update(
            session, account, new_default_mode=op[1:], actor="owner", clock=clock
        )
        screen = await settings_screen(session, clock, account)
        answer = operational_text(
            "new_chats",
            account.locale_pref,
            mode=operational_text(op[1:], account.locale_pref),
        )
        _after_press(session, gateway, press, target, answer=answer, edit=screen)
        return HandlerResult(
            True, "card", None, note="default_mode" if change.audit_id else "unchanged"
        )
    if op in {f"l{m}" for m in modes.SETTABLE_MODES}:
        listing = await list_screen(session, clock, account, op[1:], 1)
        settings = await settings_screen(session, clock, account)  # re-armed
        _after_press(session, gateway, press, target, answer="Chats", edit=settings, send=listing)
        return HandlerResult(True, "card", None, note="listed")
    screens = {
        OP_LANGUAGE: language_screen,
        "replies": reply_screen,
        "retention": retention_screen,
        "alerts": alerts_screen,
        "delall": delete_all_screen,
    }
    if op in screens:
        screen = await screens[op](session, clock, account)
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("settings", account.locale_pref),
            edit=screen,
        )
        return HandlerResult(True, "card", None, note=op)
    _after_press(
        session,
        gateway,
        press,
        target,
        answer=operational_text("expired", account.locale_pref),
    )
    return HandlerResult(False, "card", None, note="unknown_op")


async def _press_list(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    parts = press.op.split("_")
    if (
        len(parts) == 3
        and parts[0] == "p"
        and parts[1] in modes.SETTABLE_MODES
        and parts[2].isdigit()
    ):
        page = int(parts[2]) if len(parts[2]) <= 6 else 1
        screen = await list_screen(session, clock, account, parts[1], page)
        _after_press(session, gateway, press, target, answer=f"Page {page}", edit=screen)
        return HandlerResult(True, "card", None, note="page")
    if len(parts) == 2 and parts[0] == "o" and parts[1].isdigit() and len(parts[1]) <= 18:
        panel = await _load_panel(session, clock, account, int(parts[1]))
        if panel is None:  # not this owner's chat: reveal nothing
            _after_press(
                session,
                gateway,
                press,
                target,
                answer=operational_text("expired", account.locale_pref),
            )
            return HandlerResult(False, "card", None, note="unknown_chat")
        mode = panel.mode if panel.mode in modes.SETTABLE_MODES else modes.MODE_OBSERVE
        listing = await list_screen(session, clock, account, mode, 1)  # re-armed
        shown = await panel_screen(session, clock, account, panel)
        _after_press(session, gateway, press, target, answer="Opened", edit=listing, send=shown)
        return HandlerResult(True, "card", None, note="opened")
    _after_press(
        session,
        gateway,
        press,
        target,
        answer=operational_text("expired", account.locale_pref),
    )
    return HandlerResult(False, "card", None, note="unknown_op")


# ------------------------------------------------------------ dispatch
async def handle_press(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
    scheduler: JobScheduler | None = None,
) -> HandlerResult:
    """A press on one of this module's cards: claimed once, owner-scoped.
    `scheduler` queues the button deletions (M03 D83)."""
    card = await service.claim_card(session, account_id=account.id, token=press.token, clock=clock)
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    if card is None or card.kind != press.kind:
        _after_press(
            session,
            gateway,
            press,
            target,
            answer=operational_text("expired", account.locale_pref),
        )
        return HandlerResult(False, "card", None, note="no_longer_active")
    if card.kind == PANEL_CARD and card.ref_id is not None:
        return await _press_panel(session, gateway, clock, account, press, card.ref_id)
    if card.kind == IGNORE_CARD and card.ref_id is not None:
        return await _press_ignore(
            session, gateway, clock, account, press, card.ref_id, scheduler, card.id
        )
    if card.kind == SETTINGS_CARD:
        return await _press_settings(session, gateway, clock, account, press)
    if card.kind == LIST_CARD:
        return await _press_list(session, gateway, clock, account, press)
    if card.kind == DELETE_CARD and card.ref_id is not None:
        return await _press_delete(
            session, gateway, clock, account, press, card.ref_id, scheduler, card.id
        )
    if card.kind == REPLY_CARD:
        return await _press_reply(session, gateway, clock, account, press)
    if card.kind == RETENTION_CARD:
        return await _press_retention(session, gateway, clock, account, press)
    if card.kind == RETENTION_CONFIRM_CARD:
        return await _press_retention_confirm(session, gateway, clock, account, press)
    if card.kind == DELETE_ALL_CARD:
        return await _press_delete_all(session, gateway, clock, account, press, scheduler, card.id)
    if card.kind == LANGUAGE_CARD:
        return await _press_language(session, gateway, clock, account, press)
    if card.kind == ALERTS_CARD:
        return await _press_alerts(session, gateway, clock, account, press)
    if card.kind == MUTES_CARD:
        return await _press_mutes(session, gateway, clock, account, press)
    if card.kind == ONBOARDING_LANGUAGE_CARD:
        return await _press_onboarding_language(session, gateway, clock, account, press)
    if card.kind == ONBOARDING_TIMEZONE_CARD:
        return await _press_onboarding_timezone(session, gateway, clock, account, press)
    _after_press(
        session,
        gateway,
        press,
        target,
        answer=operational_text("expired", account.locale_pref),
    )
    return HandlerResult(False, "card", None, note="no_longer_active")
