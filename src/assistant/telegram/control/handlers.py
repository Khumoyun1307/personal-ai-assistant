"""Control-chat command handlers (ARCHITECTURE §6.7).

Every reply goes through `OutboundGateway`. These handlers never call
`Message.answer()`, `reply()` or any other aiogram shortcut — those auto-inject
a `business_connection_id` and would send **as the owner** (AGENTS §7). An AST
test scans this package to keep that true.

Handlers are plain async functions over our own types, not aiogram models, so
they can be tested without constructing Telegram objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import connections, preferences, service
from assistant.accounts.models import Account, PendingInput
from assistant.agent import confirmations
from assistant.agent.confirmations import OP_CONFIRM, OP_REJECT, TOOL_CONFIRM_CARD
from assistant.conversations.coverage import STATUS_GAP_WINDOW
from assistant.conversations.visibility import ConversationPanel
from assistant.core.clock import Clock
from assistant.core.jobs import APPLY_CONFIRMATION_TASK, JobScheduler, owner_control_lock
from assistant.db.uow import after_commit, transaction
from assistant.inbox.connection_alerts import MANAGED_CONSENT_BUTTONS, MANAGED_CONSENT_QUESTION
from assistant.memory import undo
from assistant.observability.status import StatusReport
from assistant.privacy import deletion
from assistant.telegram.control.cards import card_keyboard
from assistant.telegram.gateway import ControlChat, OutboundGateway, SendResult
from assistant.telegram.render import escape

#: The version of the consent text and policy currently in force.
POLICY_VERSION = service.POLICY_VERSION

#: Named in the consent, because consent has to say who the data goes to.
AI_PROCESSORS = list(service.AI_PROCESSORS)

#: Answers that count as agreement to a consent question. Anything else declines.
AGREEMENT_WORDS = frozenset({"yes", "ha", "да", "y", "ok", "okay"})

#: Stop words treated as `/pause` before any model is called (§6.7). Matched
#: against the whole message, so an ordinary sentence containing "stop" does
#: not trip it.
# The Uzbek forms deliberately include both the modifier letter turned comma
# (the correct character) and the ASCII apostrophe people type on phone
# keyboards. Both have to match.
STOP_WORDS: frozenset[str] = frozenset(
    {"stop", "стоп", "to'xta", "toxta", "toʻxta", "тўхта", "pause", "пауза"}
)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """A control-chat message, reduced to what handlers need."""

    account_id: int
    chat_id: int
    telegram_message_id: int
    text: str
    from_id: int
    #: When Telegram delivered it to us (`inbound_updates.received_at`), for
    #: the acknowledgement-latency metric. None outside the inbox (tests).
    received_at: datetime | None = None
    #: Forwarded from someone else: `text` is the third party's, never a
    #: command (§6 invariant 3b).
    forwarded: bool = False
    #: Text quoted from another chat or from a forwarded message.
    quoted_text: str | None = None
    #: Who the forwarded/quoted text came from (user ids, chat ids, the
    #: names of hidden users), to recognise managed-chat content.
    origin_user_ids: frozenset[int] = frozenset()
    origin_chat_ids: frozenset[int] = frozenset()
    origin_names: frozenset[str] = frozenset()

    @property
    def third_party(self) -> bool:
        """The message carries third-party text: the run it starts is tainted."""
        return self.forwarded or self.quoted_text is not None


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """What a handler did, so the caller can record and assert on it."""

    handled: bool
    command: str
    send: SendResult | None = None
    note: str | None = None


def is_stop_word(text: str) -> bool:
    """Whole-message match only — never a substring."""
    return text.strip().lower().strip("!.") in STOP_WORDS


def parse_command(text: str) -> tuple[str, str]:
    """Split `/cmd arg` into `("cmd", "arg")`. Non-commands give `("", text)`.

    Handles the `/cmd@botname` form Telegram adds in groups.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return "", stripped
    head, _, rest = stripped.partition(" ")
    command = head[1:].split("@", 1)[0].lower()
    return command, rest.strip()


HELP_TEXT = (
    "<b>What I can do</b>\n\n"
    "/start — set up or re-run onboarding\n"
    "/help — this message\n"
    "/status — what I can see: connection, rights, coverage, gaps, consent\n"
    "/settings — language, timezone, the mode new chats start in, chats by mode\n"
    "/privacy — what I store, who sees it, and how to delete it\n\n"
    "Send me anything else and I will answer it.\n"
    "Send <b>stop</b> at any time to pause everything."
)


async def handle_start(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    message: IncomingMessage,
) -> HandlerResult:
    """`/start` — create the account, record where to reach the owner, ask for consent."""
    account, created = await service.get_or_create_account(
        session, telegram_user_id=message.from_id, clock=clock
    )
    await service.set_control_chat(session, account, message.chat_id)

    consent = await service.active_consent(session, account.id, kind=service.CONSENT_AI_CONTROL)
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)

    if consent.granted:
        result = await gateway.send_text(
            target,
            "You are already set up. /help shows what I can do.",
        )
        return HandlerResult(True, "start", result, note="already_onboarded")

    # Consent is asked for explicitly, naming the processor, before anything
    # is sent to it (§6 invariant 9).
    await service.set_pending_input(
        session,
        account_id=account.id,
        kind="onboarding_consent",
        clock=clock,
        prompt_message_id=message.telegram_message_id,
    )
    result = await gateway.send_text(
        target,
        "<b>Before we start</b>\n\n"
        "To answer you, I send the text of this chat to "
        f"<b>{', '.join(AI_PROCESSORS)}</b>. Nothing is sent anywhere else, and "
        "your messages are not used to train any model.\n\n"
        "See /privacy for what I store and how to delete it.\n\n"
        "Reply <b>yes</b> to agree, or anything else to decline. "
        "You can revoke this later at any time.",
    )
    return HandlerResult(True, "start", result, note="created" if created else "consent_requested")


async def handle_consent_reply(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    message: IncomingMessage,
    account: Account,
) -> HandlerResult:
    """The owner's answer to the consent question."""
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)
    agreed = message.text.strip().lower() in AGREEMENT_WORDS
    await service.clear_pending_input(session, account_id=account.id)

    if not agreed:
        result = await gateway.send_text(
            target,
            "No problem — nothing will be sent to an AI provider.\n"
            "/settings and /privacy still work. Run /start again to change your mind.",
        )
        return HandlerResult(True, "consent", result, note="declined")

    await service.grant_consent(
        session,
        account,
        kind=service.CONSENT_AI_CONTROL,
        policy_version=POLICY_VERSION,
        processors=AI_PROCESSORS,
        clock=clock,
    )
    account.status = "active"
    result = await gateway.send_text(
        target, "Done. Ask me anything, or see /help.\n\nSend <b>stop</b> to pause."
    )
    return HandlerResult(True, "consent", result, note="granted")


#: The `/privacy managed` card asks exactly what the connection notice asks.
MANAGED_CONSENT_TEXT = MANAGED_CONSENT_QUESTION
MANAGED_CONSENT_GRANTED_TEXT = (
    "Turned on. Alviro's AI agent can now read the messages saved from the chats "
    "you connected when it works on them for you; the text is processed by "
    "{processors}. Send <code>/privacy revoke</code> to turn AI off (this also "
    "turns it off for this chat)."
)
MANAGED_CONSENT_DECLINED_TEXT = (
    "Not now. Alviro's AI agent doesn't read the chats you connected. "
    "Send <code>/privacy managed</code> if you change your mind."
)
MANAGED_CONSENT_ALREADY_ON_TEXT = (
    "It's already on. Send <code>/privacy revoke</code> to turn AI off (for this chat too)."
)


async def handle_managed_consent_request(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    message: IncomingMessage,
) -> HandlerResult:
    """`/privacy managed` — ask for consent to process managed chats (FR-ONB-05b)."""
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)
    state = await service.active_consent(session, account.id, kind=service.CONSENT_AI_MANAGED)
    if state.granted:
        result = await gateway.send_text(target, MANAGED_CONSENT_ALREADY_ON_TEXT)
        return HandlerResult(True, "managed_consent", result, note="already_granted")
    if await connections.connection_for_account(session, account.id) is None:
        result = await gateway.send_text(
            target,
            "I am not connected to your account yet. Connect me in your Telegram settings "
            "(Chat Automation, or Telegram Business → Chatbots in older apps) first; "
            "I will ask you then.",
        )
        return HandlerResult(True, "managed_consent", result, note="not_connected")
    # A card, not a text question: only a tap on Turn on or Not now decides, so
    # an unrelated message can never be taken as an answer (S02.6).
    card = await service.create_card(
        session,
        account_id=account.id,
        kind=service.MANAGED_CONSENT_CARD,
        clock=clock,
        ttl=service.MANAGED_CONSENT_TTL,
    )
    assert card.token is not None
    result = await gateway.send_text(
        target,
        MANAGED_CONSENT_TEXT.format(processors=", ".join(AI_PROCESSORS)),
        keyboard=card_keyboard(
            service.MANAGED_CONSENT_CARD,
            card.token,
            list(MANAGED_CONSENT_BUTTONS),
        ),
    )
    if result.ok:
        card.prompt_message_id = result.message_id
    else:
        await session.delete(card)  # never shown: nothing to press
    await session.flush()
    return HandlerResult(True, "managed_consent", result, note="requested")


async def _grant_managed_consent(session: AsyncSession, clock: Clock, account: Account) -> bool:
    """Record `ai_managed` (FR-ONB-05b). False if it was already active."""
    if (await service.active_consent(session, account.id, kind=service.CONSENT_AI_MANAGED)).granted:
        return False
    await service.grant_consent(
        session,
        account,
        kind=service.CONSENT_AI_MANAGED,
        policy_version=POLICY_VERSION,
        processors=AI_PROCESSORS,
        clock=clock,
    )
    # Agreeing again lifts a "stop observing" choice made earlier.
    await service.set_managed_mode(session, account, service.MANAGED_MODE_OBSERVE)
    return True


async def handle_help(
    gateway: OutboundGateway, account: Account, message: IncomingMessage
) -> HandlerResult:
    result = await gateway.send_text(
        ControlChat(account_id=account.id, chat_id=message.chat_id), HELP_TEXT
    )
    return HandlerResult(True, "help", result)


async def handle_privacy(
    session: AsyncSession,
    gateway: OutboundGateway,
    account: Account,
    message: IncomingMessage,
    *,
    policy_url: str | None = None,
) -> HandlerResult:
    """`/privacy` — say plainly what is stored and who sees it (FR-PRV-01)."""
    consent = await service.active_consent(session, account.id, kind=service.CONSENT_AI_CONTROL)
    managed = await service.active_consent(session, account.id, kind=service.CONSENT_AI_MANAGED)
    connected = await connections.connection_for_account(session, account.id) is not None

    def describe(state: service.ConsentState) -> str:
        if not state.granted:
            return "<b>not granted</b> — nothing is sent to an AI provider"
        return (
            f"granted (policy {state.policy_version}, "
            f"processor: {', '.join(state.processors) or 'none named'})"
        )

    text = (
        "<b>Privacy</b>\n\n"
        f"AI for this chat: {describe(consent)}\n"
        f"AI for your chats with other people: {describe(managed)}\n\n"
        "In this chat I store your messages and my replies (text kept for 90 days), "
        "your settings, and a record of what I did that contains no message text "
        "(kept for 1 year).\n\n"
        "From the chats you connected, I save new messages (text kept up to 90 days), "
        "whether or not AI is on. If someone else writes to me directly, I don't keep "
        "their message. Everything is on a server in the US (Oracle, Ashburn) with "
        "encrypted daily backups, also kept at Backblaze B2 in the EU, for up to 35 "
        "days.\n\n"
        "Your messages are never used to train a model."
    )
    if connected and not managed.granted:
        text += "\n\nSend <code>/privacy managed</code> to decide about your other chats."
    if consent.granted or managed.granted:
        text += "\n\nSend <code>/privacy revoke</code> to stop all AI processing."
    if policy_url:
        text += f'\n\nFull policy: <a href="{escape(policy_url)}">{escape(policy_url)}</a>'
    result = await gateway.send_text(
        ControlChat(account_id=account.id, chat_id=message.chat_id), text
    )
    return HandlerResult(True, "privacy", result)


async def handle_revoke(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    message: IncomingMessage,
) -> HandlerResult:
    """`/privacy revoke` — consent is revocable at any time (FR-PRV-02).

    Takes effect at once: the consent wrapper reads consent on every model
    call, so queued work re-checks and stops (§6 invariant 9).

    Open consent cards close first: a Turn on tapped later on a card shown
    before the revoke must not grant again. Closing them before revoking
    also orders a concurrent press (it holds the card's row lock) ahead of
    the revoke, so a grant it commits is revoked here too.
    """
    await service.close_cards(session, account_id=account.id, kind=service.MANAGED_CONSENT_CARD)
    revoked = await service.revoke_consent(
        session, account, kind=service.CONSENT_AI_CONTROL, clock=clock
    )
    managed_revoked = await service.revoke_consent(
        session, account, kind=service.CONSENT_AI_MANAGED, clock=clock
    )
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)
    if not revoked and not managed_revoked:
        result = await gateway.send_text(target, "There was no AI consent to revoke.")
        return HandlerResult(True, "revoke", result, note="nothing_to_revoke")
    result = await gateway.send_text(
        target,
        "AI consent revoked. Nothing more will be sent to an AI provider.\n"
        "/settings, /status and /privacy still work. Send /start to agree again.",
    )
    if managed_revoked:
        await offer_managed_data_choices(session, gateway, clock, account, message.chat_id)
    return HandlerResult(True, "revoke", result, note="revoked")


# ------------------------------------------------------------ managed-data cards
MANAGED_DELETE_KIND = "managed_delete"
MANAGED_MODE_KIND = "managed_mode"

DELETE_CARD_TEXT = (
    "<b>Delete what I stored from your chats?</b>\n\n"
    "This removes every message, name, summary and raw update I kept from the chats "
    "you included under Chat Automation, and your notes about the people in them. It "
    "cannot be undone. Your own notes and settings stay."
)
DELETE_CARD_BUTTONS = [("Delete", "delete"), ("Keep", "keep")]
MODE_CARD_TEXT = (
    "<b>How should I continue?</b>\n\n"
    "<b>Without AI</b> — I keep observing your included chats for coverage and "
    "visibility only; nothing is sent to an AI provider.\n"
    "<b>Stop observing</b> — I store nothing further from them. To disconnect me "
    "completely, remove me under Settings → Chat Automation; there is no way for "
    "me to do that myself."
)


@dataclass(frozen=True, slots=True)
class CardPress:
    """A button press on a control-chat card, already parsed and owner-routed."""

    kind: str
    token: str
    op: str
    callback_id: str
    chat_id: int
    message_id: int | None


async def offer_managed_data_choices(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    chat_id: int,
) -> SendResult:
    """Card A of the decline/revoke flow (FR-ONB-06, J1 step 5)."""
    card = await service.create_card(
        session, account_id=account.id, kind=MANAGED_DELETE_KIND, clock=clock
    )
    assert card.token is not None
    result = await gateway.send_text(
        ControlChat(account_id=account.id, chat_id=chat_id),
        DELETE_CARD_TEXT,
        keyboard=card_keyboard(MANAGED_DELETE_KIND, card.token, DELETE_CARD_BUTTONS),
    )
    card.prompt_message_id = result.message_id
    await session.flush()
    return result


@dataclass(frozen=True, slots=True)
class _Settlement:
    """What a resolved card press tells the owner, decided inside the
    transaction and sent only after it commits."""

    answer: str
    text: str | None = None
    #: A follow-up card already created in the transaction: `(card_id, kind,
    #: token, text, buttons)`.
    follow_up: tuple[int, str, str, str, list[tuple[str, str]]] | None = None
    #: The card's message says more than its question (a connection notice):
    #: only its buttons are removed, and `text` arrives as a new message.
    keep_body: bool = False


def _queue_settlement(
    session: AsyncSession,
    gateway: OutboundGateway,
    press: CardPress,
    target: ControlChat,
    message_id: int | None,
    settlement: _Settlement,
) -> None:
    """Every Telegram call of a card press runs after the commit (AGENTS §13):
    no row lock is held while Telegram answers, and a transaction that fails
    and is retried has announced nothing, so the retry announces exactly once.
    A crash between commit and send loses the announcement, never the effect:
    at most once, like every control-chat card (§6 invariant 6)."""

    async def announce(factory: async_sessionmaker[AsyncSession]) -> None:
        await gateway.answer_callback(press.callback_id, settlement.answer)
        if settlement.keep_body and message_id is not None:
            await gateway.clear_control_keyboard(target, message_id)
        if settlement.text is not None:
            if message_id is None or settlement.keep_body:
                await gateway.send_text(target, settlement.text)
            else:
                await gateway.edit_control_text(target, message_id, settlement.text)
        if settlement.follow_up is not None:
            card_id, kind, token, text, buttons = settlement.follow_up
            result = await gateway.send_text(
                target, text, keyboard=card_keyboard(kind, token, buttons)
            )
            async with transaction(factory) as uow:
                card = await uow.session.get(PendingInput, card_id)
                if card is not None:
                    if result.ok:
                        card.prompt_message_id = result.message_id
                    else:
                        # Never shown: nothing can press it.
                        await uow.session.delete(card)

    after_commit(session, announce)


async def _delete_choice_card(
    session: AsyncSession, clock: Clock, account: Account
) -> tuple[int, str, str, str, list[tuple[str, str]]]:
    """Card A (delete or keep what was stored), created now and sent after
    the commit — the follow-up of a declined consent card."""
    card = await service.create_card(
        session, account_id=account.id, kind=MANAGED_DELETE_KIND, clock=clock
    )
    assert card.token is not None
    return (card.id, MANAGED_DELETE_KIND, card.token, DELETE_CARD_TEXT, DELETE_CARD_BUTTONS)


async def _mode_choice_card(
    session: AsyncSession, clock: Clock, account: Account
) -> tuple[int, str, str, str, list[tuple[str, str]]]:
    """Card B (continue without AI, or stop observing), created now and sent
    after the commit."""
    card = await service.create_card(
        session, account_id=account.id, kind=MANAGED_MODE_KIND, clock=clock
    )
    assert card.token is not None
    return (
        card.id,
        MANAGED_MODE_KIND,
        card.token,
        MODE_CARD_TEXT,
        [("Without AI", "observe"), ("Stop observing", "stop")],
    )


async def handle_card_press(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    """Resolve a card exactly once; edit it in place; always answer the press.

    `claim_card` is scoped to the pressing owner's account and deletes the
    row under a lock, so a stale, foreign, expired or double tap all end as
    "no longer active" (§6 invariant 12; S01.3 primitives). The database work
    happens here; what the owner is told is queued for after the commit
    (`_queue_settlement`), so the result carries no `SendResult`.
    """
    card = await service.claim_card(session, account_id=account.id, token=press.token, clock=clock)
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    message_id = press.message_id
    if card is None or card.kind != press.kind:
        _queue_settlement(
            session, gateway, press, target, message_id, _Settlement(CALLBACK_EXPIRED_TEXT)
        )
        return HandlerResult(False, "card", None, note="no_longer_active")
    if message_id is None:
        message_id = card.prompt_message_id

    # A consent card that rides on a connection notice (its `ref_id` is the
    # connection event) keeps the notice's text: tier, rights, advice.
    in_notice = card.kind == service.MANAGED_CONSENT_CARD and card.ref_id is not None
    match (press.kind, press.op):
        case (k, "agree") if k == service.MANAGED_CONSENT_CARD:
            granted = await _grant_managed_consent(session, clock, account)
            settlement = _Settlement(
                "Turned on" if granted else "Already on",
                MANAGED_CONSENT_GRANTED_TEXT.format(processors=", ".join(AI_PROCESSORS))
                if granted
                else MANAGED_CONSENT_ALREADY_ON_TEXT,
                keep_body=in_notice,
            )
            note = "granted" if granted else "already_granted"
        case (k, "decline") if k == service.MANAGED_CONSENT_CARD:
            state = await service.active_consent(
                session, account.id, kind=service.CONSENT_AI_MANAGED
            )
            if state.granted:
                # Agreed meanwhile (another card): revoking is /privacy revoke.
                settlement = _Settlement(
                    "Already on",
                    MANAGED_CONSENT_ALREADY_ON_TEXT,
                    keep_body=in_notice,
                )
                note = "already_granted"
            else:
                await service.record_managed_consent_declined(
                    session, account_id=account.id, clock=clock
                )
                settlement = _Settlement(
                    "Not now",
                    MANAGED_CONSENT_DECLINED_TEXT,
                    await _delete_choice_card(session, clock, account),
                    keep_body=in_notice,
                )
                note = "declined"
        case (k, "delete") if k == MANAGED_DELETE_KIND:
            report = await deletion.delete_managed_data(session, account_id=account.id, clock=clock)
            settlement = _Settlement(
                "Deleted",
                f"🗑 Deleted: {report.messages} message(s), {report.contacts_cleared} "
                f"contact name(s), {report.payloads_cleared} raw update(s), "
                f"{report.summaries} summary(ies), {report.memory_items} note(s).",
                await _mode_choice_card(session, clock, account),
            )
            note = "deleted"
        case (k, "keep") if k == MANAGED_DELETE_KIND:
            settlement = _Settlement(
                "Kept",
                "Kept. Nothing was deleted.",
                await _mode_choice_card(session, clock, account),
            )
            note = "kept"
        case (k, "observe") if k == MANAGED_MODE_KIND:
            await service.set_managed_mode(session, account, service.MANAGED_MODE_OBSERVE)
            settlement = _Settlement(
                "Observing without AI",
                "👀 I keep observing your included chats for coverage and visibility. "
                "Nothing is sent to an AI provider. Send <code>/privacy managed</code> "
                "to change that.",
            )
            note = "observe"
        case (k, "stop") if k == MANAGED_MODE_KIND:
            await service.set_managed_mode(session, account, service.MANAGED_MODE_STOPPED)
            settlement = _Settlement(
                "Stopped",
                "⏹ Stopped. I store nothing further from your included chats. To "
                "disconnect me completely, remove me under Settings → Chat Automation. "
                "Send <code>/privacy managed</code> to start observing again.",
            )
            note = "stopped"
        case _:
            _queue_settlement(
                session, gateway, press, target, message_id, _Settlement(CALLBACK_EXPIRED_TEXT)
            )
            return HandlerResult(False, "card", None, note="unknown_op")
    _queue_settlement(session, gateway, press, target, message_id, settlement)
    return HandlerResult(True, "card", None, note=note)


CALLBACK_EXPIRED_TEXT = "This button is no longer active."

TOOL_CONFIRMED_TEXT = "✅ Confirmed. Applying it now; I will tell you when it is done."
TOOL_REJECTED_TEXT = "❌ Rejected. Nothing was changed."


async def handle_tool_confirmation_press(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    scheduler: JobScheduler | None,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    """Confirm / Reject on a held tool call (ARCHITECTURE §7.1, S03.1).

    The owner's own tap needs no further card. The card is claimed once
    (single-use token, owner-scoped), so a double tap is a no-op; the
    confirmation moves `pending` → `confirmed` and its job is deferred in
    this transaction, or `pending` → `rejected`. Telegram is told after the
    commit (`_queue_settlement`)."""
    card = await service.claim_card(session, account_id=account.id, token=press.token, clock=clock)
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    message_id = press.message_id
    if card is None or card.kind != TOOL_CONFIRM_CARD or card.ref_id is None:
        _queue_settlement(
            session, gateway, press, target, message_id, _Settlement(CALLBACK_EXPIRED_TEXT)
        )
        return HandlerResult(False, "card", None, note="no_longer_active")
    if message_id is None:
        message_id = card.prompt_message_id
    approve = press.op == OP_CONFIRM
    if press.op not in (OP_CONFIRM, OP_REJECT):
        _queue_settlement(
            session, gateway, press, target, message_id, _Settlement(CALLBACK_EXPIRED_TEXT)
        )
        return HandlerResult(False, "card", None, note="unknown_op")
    moved = await confirmations.decide(
        session, account_id=account.id, confirmation_id=card.ref_id, approve=approve, clock=clock
    )
    if not moved:
        _queue_settlement(
            session, gateway, press, target, message_id, _Settlement(CALLBACK_EXPIRED_TEXT)
        )
        return HandlerResult(False, "card", None, note="already_decided")
    if approve:
        if scheduler is None:
            raise RuntimeError("a job scheduler is required to apply a confirmation")
        connection = await session.connection()
        raw = await connection.get_raw_connection()
        await scheduler.defer(
            APPLY_CONFIRMATION_TASK,
            args={"confirmation_id": card.ref_id},
            lock=owner_control_lock(account.id),
            connection=raw.driver_connection,
        )
        settlement = _Settlement("Confirmed", TOOL_CONFIRMED_TEXT)
        note = "confirmed"
    else:
        settlement = _Settlement("Rejected", TOOL_REJECTED_TEXT)
        note = "rejected"
    _queue_settlement(session, gateway, press, target, message_id, settlement)
    return HandlerResult(True, "card", None, note=note)


UNDONE_TEXT = "↩️ Undone."
UNDO_SUPERSEDED_TEXT = (
    "It was changed again since, so nothing was undone. Check /settings or ask me what is "
    "saved now."
)


async def handle_undo_press(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    """Undo on an echoed change (S03.3). The owner's own tap: no further card.
    Single-use and owner-scoped like every card; the change is reversed only
    while the state it created is still in place (`memory/undo.py`)."""
    card = await service.claim_card(session, account_id=account.id, token=press.token, clock=clock)
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    message_id = press.message_id
    if card is None or card.kind != undo.UNDO_CARD or card.ref_id is None:
        _queue_settlement(
            session, gateway, press, target, message_id, _Settlement(CALLBACK_EXPIRED_TEXT)
        )
        return HandlerResult(False, "card", None, note="no_longer_active")
    if message_id is None:
        message_id = card.prompt_message_id
    if press.op != undo.OP_UNDO:
        _queue_settlement(
            session, gateway, press, target, message_id, _Settlement(CALLBACK_EXPIRED_TEXT)
        )
        return HandlerResult(False, "card", None, note="unknown_op")
    outcome = await undo.undo(session, account_id=account.id, change_id=card.ref_id, clock=clock)
    if outcome.status is undo.UndoStatus.UNDONE:
        settlement = _Settlement("Undone", UNDONE_TEXT, keep_body=True)
    elif outcome.status is undo.UndoStatus.SUPERSEDED:
        settlement = _Settlement("Not undone", UNDO_SUPERSEDED_TEXT, keep_body=True)
    else:
        settlement = _Settlement(CALLBACK_EXPIRED_TEXT)
    _queue_settlement(session, gateway, press, target, message_id, settlement)
    return HandlerResult(
        outcome.status is undo.UndoStatus.UNDONE, "card", None, note=outcome.status.value
    )


def local_time(value: datetime | None, timezone: str) -> str:
    """A timestamp in the owner's timezone, for presentation only (AGENTS §9)."""
    if value is None:
        return "—"
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError, KeyError:
        zone = ZoneInfo("UTC")
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


TIER_TEXT = {
    "none": "no permissions (disabled or no rights)",
    "observe_only": "observe only (no reply right)",
    "can_reply": "can reply",
    "extended": "can reply, plus rights I do not use",
}


def render_status(report: StatusReport, *, visibility: str) -> str:
    """The visibility report (FR-ONB-08): what I can see, and what is wrong.

    Every line comes from `StatusReport`, so the `get_status` tool and this
    command cannot disagree. The coverage caveat is never swallowed
    (§6 invariant 11).
    """
    tz = report.timezone
    lines = [
        "<b>Status</b>",
        "",
        f"Account: {escape(report.account_status)}",
        f"Autonomy: {'paused' if report.autonomy.paused else 'active'}",
        f"Timezone: {escape(tz)}",
        "",
        "<b>Connection</b>",
    ]
    conn = report.connection
    if not conn.present:
        lines.append("  Not connected. Add me under Settings → Chat Automation.")
    else:
        tier = TIER_TEXT.get(conn.tier or "", conn.tier or "unknown")
        lines += [
            f"  State: {escape(conn.status or 'unknown')}",
            f"  Rights: {escape(tier)}",
            f"  Connected since: {local_time(conn.connected_at, tz)}",
        ]
    cov = report.coverage
    lines += ["", "<b>Coverage</b>"]
    if cov.conversations == 0:
        lines.append("  No managed chats observed yet.")
    else:
        modes = ", ".join(f"{count} {mode}" for mode, count in sorted(cov.by_mode.items()))
        lines += [
            f"  Chats observed: {cov.conversations} ({escape(modes)})",
            f"  Observing since: {local_time(cov.coverage_started_at, tz)}",
        ]
    lines.append(f"  {escape(visibility)}")
    if cov.latest_gap is not None:
        gap = cov.latest_gap
        lines.append(
            f"  Latest gap ({escape(gap.certainty)}): {local_time(gap.at_risk_from, tz)} → "
            f"{local_time(gap.at_risk_to, tz)}"
        )
    if cov.older_unresolved_gaps:
        # Known gaps are always shown (FR-ONB-08); past the window, without a
        # warning (M02 D57).
        lines.append(
            f"  Older gaps ({STATUS_GAP_WINDOW.days} or more days ago): {cov.older_unresolved_gaps}"
        )
    consent = report.consent
    lines += [
        "",
        "<b>AI consent</b>",
        f"  This chat: {'granted' if consent.control_chat else 'not granted'}",
        f"  Included chats: {'granted' if consent.managed_chats else 'not granted'}",
    ]
    if report.autonomy.conversations_paused:
        detail = ", ".join(
            f"{count} {reason}"
            for reason, count in sorted(report.autonomy.conversations_paused.items())
        )
        lines += ["", "<b>Autonomy per chat</b>", f"  Paused: {escape(detail)}"]
    inbox = report.inbox
    lines += ["", "<b>Inbox</b>"]
    if inbox.pending or inbox.failed:
        lines.append(f"  pending: {inbox.pending}, failed: {inbox.failed}")
    else:
        lines.append("  nothing pending")
    if inbox.poller_heartbeat_age_seconds is not None:
        lines.append(f"  Last poll: {inbox.poller_heartbeat_age_seconds} s ago")
    lines += [
        "",
        "<b>AI spend this month</b>",
        f"  ${escape(report.budget.month_to_date_usd)}"
        + (
            f" of ${escape(report.budget.monthly_limit_usd)}"
            if report.budget.monthly_limit_usd
            else ""
        ),
    ]
    if inbox.failed:
        lines += ["", f"⚠️ {inbox.failed} update(s) could not be processed."]
    if report.coverage_caveat:
        lines += ["", escape(report.coverage_caveat)]
    return "\n".join(lines)


async def handle_status(
    gateway: OutboundGateway,
    account: Account,
    message: IncomingMessage,
    report: StatusReport,
    *,
    visibility: str,
) -> HandlerResult:
    """`/status` — must work even when the model is unreachable."""
    result = await gateway.send_text(
        ControlChat(account_id=account.id, chat_id=message.chat_id),
        render_status(report, visibility=visibility),
    )
    return HandlerResult(True, "status", result)


PANEL_UNKNOWN = "I don't have that chat. Only chats included under Chat Automation appear here."


def render_panel(panel: ConversationPanel, *, timezone: str) -> str:
    name = panel.display_name or "(no name)"
    handle = f" @{escape(panel.username)}" if panel.username else ""
    counts = ", ".join(
        f"{count} {direction.replace('_', ' ')}"
        for direction, count in sorted(panel.counts_by_direction.items())
    )
    window = (
        f"open until about {local_time(panel.window_closes_at, timezone)}"
        if panel.window_open
        else "closed (no contact message in the last 24 h)"
    )
    lines = [
        f"<b>{escape(name)}</b>{handle}",
        "",
        f"Mode: {escape(panel.mode)}",
        f"First seen: {local_time(panel.first_seen_at, timezone)}",
        f"Observing since: {local_time(panel.coverage_started_at, timezone)}",
        f"Messages: {panel.total}" + (f" ({escape(counts)})" if counts else ""),
    ]
    if panel.deleted or panel.expired:
        lines.append(f"Deleted: {panel.deleted}, expired: {panel.expired}")
    lines += [
        f"Last from contact: {local_time(panel.last_contact_msg_at, timezone)}",
        f"Reply window: {window} (approximate)",
    ]
    if panel.availability != "active":
        lines.append("⚠️ The chat may be paused or excluded; my last action there failed.")
    if panel.autonomy_paused_reason == "ingest_error":
        lines.append("⚠️ A message here could not be processed; automation is paused.")
    if panel.unresolved_gaps:
        lines.append(f"⚠️ {panel.unresolved_gaps} coverage gap(s): some messages may be missing.")
    if panel.mode == "ignore":
        lines.append("Ignored: new messages from this chat are not stored.")
    if panel.summary_overview:
        lines += ["", f"<b>Summary</b> (AI-written): {escape(panel.summary_overview)}"]
    if panel.notes:
        lines += ["", f"<b>Your notes</b> ({panel.notes_total}):"]
        lines += [f"• {escape(note)}" for note in panel.notes]
    return "\n".join(lines)


async def handle_settings(
    gateway: OutboundGateway, account: Account, message: IncomingMessage
) -> HandlerResult:
    """`/settings` — deterministic, no model involved, so it always works."""
    text = (
        "<b>Settings</b>\n\n"
        f"Language: {account.locale_pref or 'auto'}\n"
        f"Timezone: {account.timezone}\n\n"
        "Send <code>/settings timezone Asia/Tashkent</code> or "
        "<code>/settings language uz</code> to change one."
    )
    result = await gateway.send_text(
        ControlChat(account_id=account.id, chat_id=message.chat_id), text
    )
    return HandlerResult(True, "settings", result)


async def apply_setting(
    session: AsyncSession,
    gateway: OutboundGateway,
    account: Account,
    message: IncomingMessage,
    argument: str,
    clock: Clock,
) -> HandlerResult:
    """`/settings <key> <value>`. Validated, not trusted."""
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)
    key, _, value = argument.partition(" ")
    key, value = key.strip().lower(), value.strip()

    if not value:
        return await handle_settings(gateway, account, message)

    # The same validation and audit as the panel and the agent's tool (S03.3).
    if key == "timezone":
        try:
            await preferences.update(session, account, timezone=value, actor="owner", clock=clock)
        except preferences.PreferenceError:
            result = await gateway.send_text(
                target, f"I do not know the timezone <code>{escape(value)}</code>."
            )
            return HandlerResult(True, "settings", result, note="invalid_timezone")
        result = await gateway.send_text(target, f"Timezone set to {escape(account.timezone)}.")
        return HandlerResult(True, "settings", result, note="timezone")

    if key in {"language", "locale"}:
        try:
            await preferences.update(session, account, language=value, actor="owner", clock=clock)
        except preferences.PreferenceError:
            result = await gateway.send_text(target, "That does not look like a language code.")
            return HandlerResult(True, "settings", result, note="invalid_language")
        result = await gateway.send_text(
            target, f"Language set to {escape(account.locale_pref or value)}."
        )
        return HandlerResult(True, "settings", result, note="language")

    result = await gateway.send_text(target, "I can set <b>timezone</b> or <b>language</b>.")
    return HandlerResult(True, "settings", result, note="unknown_key")


REFUSAL_TEXT = (
    "This is a personal assistant and I only work for its owner. I will not reply further."
)


async def send_refusal(
    gateway: OutboundGateway, *, chat_id: int, account_id: int = 0
) -> SendResult:
    """The fixed reply to a non-allowlisted user (§6 invariant 12).

    One fixed sentence, no model, no state. `account_id` is 0 because there is
    no account: this person is not a tenant and must never become one.
    """
    return await gateway.send_text(
        ControlChat(account_id=account_id, chat_id=chat_id), REFUSAL_TEXT
    )


def describe_unknown_command(command: str) -> str:
    return f"I do not know /{command}. Try /help."


__all__ = [
    "AGREEMENT_WORDS",
    "AI_PROCESSORS",
    "HELP_TEXT",
    "MANAGED_CONSENT_TEXT",
    "POLICY_VERSION",
    "REFUSAL_TEXT",
    "STOP_WORDS",
    "HandlerResult",
    "IncomingMessage",
    "apply_setting",
    "describe_unknown_command",
    "handle_consent_reply",
    "handle_help",
    "handle_managed_consent_request",
    "handle_privacy",
    "handle_revoke",
    "handle_settings",
    "handle_start",
    "handle_status",
    "is_stop_word",
    "parse_command",
    "send_refusal",
]
