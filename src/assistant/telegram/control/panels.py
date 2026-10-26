"""Conversation panel buttons and the /settings panel (J12, FR-CTL-10; S03.3).

Deterministic control-chat UI — no model involved, so it works when the
provider is down. Every button is a card (`pending_inputs` with a
single-use token, owner-scoped, `accounts/service.py::claim_card`); a press
that keeps the message useful re-arms it with a fresh token. Telegram calls
of a press run after the commit (AGENTS §13).

- **Conversation panel** (`/start bizChat<id>` from "Manage Bot", or a chat
  picked in a /settings list): summary, notes, mode, and Observe / Ignore
  buttons. Observe applies at once. Ignore asks first, in place: new
  messages will not be stored, what is stored stays.
- **/settings**: language and timezone (as before), the default mode for new
  chats (Observe / Ignore), and the chats listed by mode, paginated; a chat
  opens its panel.

The Ignore confirmation offers no deletion yet: "Also delete existing data"
needs `delete_conversation_data`, which S03.5 builds. The hook is
`IGNORE_DELETE_OPTION` below — deliberately absent, not a button that does
nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import callbacks, preferences, service
from assistant.accounts.models import Account, PendingInput
from assistant.conversations import modes, visibility
from assistant.conversations.visibility import ConversationPanel
from assistant.core.clock import Clock
from assistant.db.uow import after_commit, transaction
from assistant.telegram.control.handlers import (
    CALLBACK_EXPIRED_TEXT,
    PANEL_UNKNOWN,
    CardPress,
    HandlerResult,
    IncomingMessage,
    render_panel,
)
from assistant.telegram.gateway import ControlChat, OutboundGateway, SendResult
from assistant.telegram.render import escape

PANEL_CARD = "conv_panel"
IGNORE_CARD = "conv_ignore"
SETTINGS_CARD = "settings"
LIST_CARD = "chat_list"
#: The card kinds this module answers.
KINDS = frozenset({PANEL_CARD, IGNORE_CARD, SETTINGS_CARD, LIST_CARD})

#: Chats per /settings list page.
PAGE_SIZE = 8
#: A button label is cut here (Telegram shows short labels anyway).
LABEL_CHARS = 32

#: S03.5 hook: "Also delete existing data" on the Ignore confirmation, as
#: `(label, op)`. None until `delete_conversation_data` exists (S03.5 wires
#: it and adds the op to `_ignore_confirm`). Never shown as a dead button.
IGNORE_DELETE_OPTION: tuple[str, str] | None = None

Rows = list[list[tuple[str, str]]]

MODE_LABELS = {"observe": "Observe", "ignore": "Ignore", "assist": "Assist"}

IGNORE_CONFIRM_TEXT = (
    "<b>Ignore the chat with {name}?</b>\n\n"
    "From now on I will not store or use new messages from this chat. What is "
    "already stored stays: messages until they expire, the summary and your notes."
)


def keyboard(kind: str, token: str, rows: Rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=callbacks.encode(kind, token, op))
                for label, op in row
            ]
            for row in rows
        ]
    )


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
    """Observe / Ignore, the current one ticked. None for a bot chat: it stays
    ignored (FR-OBS-04). Assist arrives with drafts (M04)."""
    if panel.is_bot:
        return None
    return [
        [
            (MODE_LABELS[mode] + (" ✓" if panel.mode == mode else ""), mode)
            for mode in modes.SETTABLE_MODES
        ]
    ]


async def panel_screen(
    session: AsyncSession, clock: Clock, account: Account, panel: ConversationPanel
) -> Screen:
    return await _screen(
        session,
        clock,
        account.id,
        PANEL_CARD,
        render_panel(panel, timezone=account.timezone),
        panel_rows(panel),
        ref_id=panel.conversation_id,
    )


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
        result = await gateway.send_text(target, PANEL_UNKNOWN)
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
    if panel is None or press.op not in modes.SETTABLE_MODES:
        _after_press(session, gateway, press, target, answer=CALLBACK_EXPIRED_TEXT)
        return HandlerResult(False, "card", None, note="no_longer_active")
    if press.op == modes.MODE_IGNORE and panel.mode != modes.MODE_IGNORE:
        # Ask first, in place of the panel (the owner's own tap still
        # needs the effect spelled out: new messages will not be stored).
        name = escape(panel.display_name or "this contact")
        screen = await _screen(
            session,
            clock,
            account.id,
            IGNORE_CARD,
            IGNORE_CONFIRM_TEXT.format(name=name),
            _ignore_confirm(),
            ref_id=conversation_id,
        )
        _after_press(session, gateway, press, target, answer="Confirm to ignore", edit=screen)
        return HandlerResult(True, "card", None, note="ignore_asked")
    note = "unchanged"
    if press.op != panel.mode:
        try:
            await modes.set_mode(
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
        note = press.op
        panel = await _load_panel(session, clock, account, conversation_id)
        assert panel is not None
    screen = await panel_screen(session, clock, account, panel)
    _after_press(session, gateway, press, target, answer=MODE_LABELS[panel.mode], edit=screen)
    return HandlerResult(True, "card", None, note=note)


def _ignore_confirm() -> Rows:
    row = [("Ignore", "confirm"), ("Cancel", "cancel")]
    if IGNORE_DELETE_OPTION is not None:  # S03.5
        row.insert(1, IGNORE_DELETE_OPTION)
    return [row]


async def _press_ignore(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
    conversation_id: int,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    if press.op not in ("confirm", "cancel"):
        _after_press(session, gateway, press, target, answer=CALLBACK_EXPIRED_TEXT)
        return HandlerResult(False, "card", None, note="unknown_op")
    note = "cancelled"
    if press.op == "confirm":
        try:
            await modes.set_mode(
                session,
                account_id=account.id,
                conversation_id=conversation_id,
                mode=modes.MODE_IGNORE,
                actor="owner",
                clock=clock,
            )
        except LookupError:
            _after_press(session, gateway, press, target, answer=CALLBACK_EXPIRED_TEXT)
            return HandlerResult(False, "card", None, note="no_longer_active")
        note = "ignored"
    panel = await _load_panel(session, clock, account, conversation_id)
    if panel is None:
        _after_press(session, gateway, press, target, answer=CALLBACK_EXPIRED_TEXT)
        return HandlerResult(False, "card", None, note="no_longer_active")
    screen = await panel_screen(session, clock, account, panel)
    answer = "Ignored: new messages are not stored" if note == "ignored" else "Nothing changed"
    _after_press(session, gateway, press, target, answer=answer, edit=screen)
    return HandlerResult(True, "card", None, note=note)


# ------------------------------------------------------------ /settings
async def settings_screen(session: AsyncSession, clock: Clock, account: Account) -> Screen:
    counts = await modes.counts_by_mode(session, account.id)
    default = preferences.default_mode(account)
    text = (
        "<b>Settings</b>\n\n"
        f"Language: {escape(account.locale_pref or 'auto')}\n"
        f"Timezone: {escape(account.timezone)}\n"
        f"New chats start in: {MODE_LABELS[default]}\n"
        f"Chats: {counts.get('observe', 0)} observed, {counts.get('ignore', 0)} ignored\n\n"
        "Send <code>/settings timezone Asia/Tashkent</code> or "
        "<code>/settings language uz</code> to change one."
    )
    rows: Rows = [
        [
            ("New chats: " + MODE_LABELS[m] + (" ✓" if default == m else ""), f"d{m}")
            for m in modes.SETTABLE_MODES
        ],
        [
            (f"Observed chats ({counts.get('observe', 0)})", "lobserve"),
            (f"Ignored chats ({counts.get('ignore', 0)})", "lignore"),
        ],
    ]
    return await _screen(session, clock, account.id, SETTINGS_CARD, text, rows)


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
    label = {"observe": "Observed", "ignore": "Ignored"}.get(mode, mode)
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
    if op in {f"d{m}" for m in modes.SETTABLE_MODES}:
        change = await preferences.update(
            session, account, new_default_mode=op[1:], actor="owner", clock=clock
        )
        screen = await settings_screen(session, clock, account)
        answer = f"New chats: {MODE_LABELS[op[1:]]}"
        _after_press(session, gateway, press, target, answer=answer, edit=screen)
        return HandlerResult(
            True, "card", None, note="default_mode" if change.audit_id else "unchanged"
        )
    if op in {f"l{m}" for m in modes.SETTABLE_MODES}:
        listing = await list_screen(session, clock, account, op[1:], 1)
        settings = await settings_screen(session, clock, account)  # re-armed
        _after_press(session, gateway, press, target, answer="Chats", edit=settings, send=listing)
        return HandlerResult(True, "card", None, note="listed")
    _after_press(session, gateway, press, target, answer=CALLBACK_EXPIRED_TEXT)
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
            _after_press(session, gateway, press, target, answer=CALLBACK_EXPIRED_TEXT)
            return HandlerResult(False, "card", None, note="unknown_chat")
        mode = panel.mode if panel.mode in modes.SETTABLE_MODES else modes.MODE_OBSERVE
        listing = await list_screen(session, clock, account, mode, 1)  # re-armed
        shown = await panel_screen(session, clock, account, panel)
        _after_press(session, gateway, press, target, answer="Opened", edit=listing, send=shown)
        return HandlerResult(True, "card", None, note="opened")
    _after_press(session, gateway, press, target, answer=CALLBACK_EXPIRED_TEXT)
    return HandlerResult(False, "card", None, note="unknown_op")


# ------------------------------------------------------------ dispatch
async def handle_press(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    """A press on one of this module's cards: claimed once, owner-scoped."""
    card = await service.claim_card(session, account_id=account.id, token=press.token, clock=clock)
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    if card is None or card.kind != press.kind:
        _after_press(session, gateway, press, target, answer=CALLBACK_EXPIRED_TEXT)
        return HandlerResult(False, "card", None, note="no_longer_active")
    if card.kind == PANEL_CARD and card.ref_id is not None:
        return await _press_panel(session, gateway, clock, account, press, card.ref_id)
    if card.kind == IGNORE_CARD and card.ref_id is not None:
        return await _press_ignore(session, gateway, clock, account, press, card.ref_id)
    if card.kind == SETTINGS_CARD:
        return await _press_settings(session, gateway, clock, account, press)
    if card.kind == LIST_CARD:
        return await _press_list(session, gateway, clock, account, press)
    _after_press(session, gateway, press, target, answer=CALLBACK_EXPIRED_TEXT)
    return HandlerResult(False, "card", None, note="no_longer_active")
