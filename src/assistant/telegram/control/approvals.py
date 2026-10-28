"""Approval cards: show, approve, edit, reject, expire and sync (S04.3).

ARCHITECTURE §6.7 (approval cards), §9.5 (approval transaction). Every
proposed owner-voice action gets one card in the control chat: who it goes
to, what it answers, the exact text, until when it can still be sent, and
Approve · Edit · Reject. Owner-facing controls live only here, in the
control chat (AGENTS §7).

- **State changes go only through S04.1's transitions** (`actions.lifecycle`,
  decisions R7): this module writes no `UPDATE` of `actions.state`. It sets
  only `card_message_id`/`card_state`, the card's own bookkeeping.
- **A press** runs in the inbox processor's transaction: every database
  change there, every Telegram call after the commit (`db.uow.after_commit`),
  the press always answered. A double tap is one transition, because each
  transition is a conditional UPDATE (RG-04). Approve defers
  `actions.execute` in the same transaction.
- **The card is eventually true**: every later state change is shown by an
  edit after its commit, and `system.sync_action_cards` edits any card whose
  `card_state` differs from the action's state (and presents a lost card).
- **Copy-ready** (FR-DRF-06): a card that cannot be sent offers the exact
  text through a `copy_text` button (Bot API `CopyTextButton`, 1-256
  characters, docs-checked 2026-10-28 at
  https://core.telegram.org/bots/api#copytextbutton), or says to copy the
  text shown.
- **Content on the card** (names, the contact's words, the draft) is shown
  literally: invisible and bidi characters are made visible as on the S03.1
  confirmation card (a line break stays one, except in a name; every line of
  the contact's words is marked as theirs), and a piece of content shaped
  like one of Telegram's tags is defused, so it can never become markup.
  The gateway's sanitizer is the one HTML escape (`telegram/render.py`);
  escaping here as well would show the owner `&amp;` instead of the `&` that
  will be sent.

Logs carry ids, states and codes only (AGENTS §9).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from aiogram.types import CopyTextButton, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import case, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import service
from assistant.accounts.callbacks import encode
from assistant.accounts.models import Account, ControlTurn, PendingInput
from assistant.actions import lifecycle
from assistant.actions.config import ActionsConfig
from assistant.actions.drafts import request_draft
from assistant.actions.models import Action
from assistant.actions.states import (
    LIVE_CARD,
    STATE_LABELS,
    STATE_REASON_EXPLANATIONS,
    ActionOrigin,
    ActionState,
    StateReason,
)
from assistant.agent.confirmations import render_description
from assistant.agent.drafting import Drafter, DraftOutcome, DraftStatus, draft_flags
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.clock import Clock
from assistant.core.delivery import MAX_TEXT_LENGTH, ErrorClass, SendResult
from assistant.core.errors import NotInAccountError
from assistant.core.jobs import (
    EXECUTE_TASK,
    REVISE_TASK,
    DraftReason,
    JobScheduler,
    revise_queueing_lock,
)
from assistant.core.reply_window import SAFETY_MARGIN, closes_at
from assistant.db.uow import after_commit, transaction
from assistant.memory.summaries import preferred_name
from assistant.observability.logging import get_logger
from assistant.telegram.control.handlers import (
    CALLBACK_EXPIRED_TEXT,
    CardPress,
    HandlerResult,
    IncomingMessage,
    local_time,
)
from assistant.telegram.gateway import ControlChat, OutboundGateway
from assistant.telegram.render import ALLOWED_TAGS, MAX_MESSAGE_CHARS, sanitize

logger = get_logger(__name__)

#: The callback kind of every approval-card button, and the `pending_inputs`
#: card kind that carries the Cancel-edit / Re-draft token (F0-interfaces §7).
KIND = "act"
#: The pending text question opened by Edit (`ref_id` = action id).
DRAFT_EDIT = "draft_edit"

OP_APPROVE = "ok"
OP_EDIT = "ed"
OP_REJECT = "rj"
OP_CANCEL_EDIT = "cx"
OP_REDRAFT = "rd"
_TOKEN_OPS = frozenset({OP_APPROVE, OP_EDIT, OP_REJECT})
_CARD_OPS = frozenset({OP_CANCEL_EDIT, OP_REDRAFT})

#: Bot API `CopyTextButton.text`: 1-256 characters.
COPY_TEXT_MAX = 256
#: The start of a draft too long for its card (TL decision, spec §3.1).
PREVIEW_CHARS = 600
#: A card is one control message: the gateway splits a sanitized text over
#: `MAX_MESSAGE_CHARS - 64`, and an edit keeps only the first part.
CARD_BUDGET = MAX_MESSAGE_CHARS - 96
EXCERPTS = 3
EXCERPT_CHARS = 300
#: How long the Edit question waits for the owner's message, at most.
EDIT_INPUT_TTL = timedelta(hours=1)
#: The sync presents a live card that has none only after this long: the
#: after-commit presentation normally shows it first.
PRESENT_GRACE = timedelta(seconds=30)
#: Failed edits of one card before the sync stops trying (logged).
SYNC_MAX_FAILURES = 10
SYNC_BATCH = 100

# ------------------------------------------------------------ texts
ANSWER_SENDING = "Sending…"
ANSWER_APPROVED_SENDING_OFF = "Approved — sending is off"
ANSWER_EXPIRED = "This draft has expired."
ANSWER_EDIT = "Send the new text, or tell me what to change."
ANSWER_REJECTED = "Rejected."
ANSWER_EDIT_CANCELLED = "Edit cancelled."
ANSWER_REDRAFTING = "Drafting a new version…"

REVISING_TEXT = "Revising…"
EDIT_CLOSED_TEXT = "That draft is no longer being edited, so your message was not used."
REVISION_FAILED_TEXT = "I couldn't revise the draft; it is unchanged."
REVISION_TOO_LONG_TEXT = (
    "That text is too long ({length} characters; the limit is {limit}). "
    "Send a shorter one, or tap Cancel edit."
)
REVISION_EMPTY_TEXT = "That text is empty. Send the new text, or tap Cancel edit."
REVISION_TIMED_OUT_TEXT = "Revising the draft took too long; it is unchanged."
RETIRED_CARD_TEXT = "📝 This draft was revised — see the new card below."

_HEADERS = {
    "dictated": "📝 <b>Message you dictated</b> — check the recipient and the text",
    "assist": "📝 <b>Assist draft</b>",
    "requested": "📝 <b>Draft reply</b>",
    "automatic": "📝 <b>Automatic reply</b>",
}

#: Short reasons for the state line ("Outdated — a newer message arrived");
#: the full explanation (F0, `actions/states.py`) follows on its own line.
REASON_SHORT: Mapping[StateReason, str] = {
    StateReason.SUPERSEDED: "a newer draft replaced it",
    StateReason.NEW_CONTACT_MESSAGE: "a newer message arrived",
    StateReason.TAKEOVER: "you replied in the chat yourself",
    StateReason.OWNER_EDIT: "you edited a message in the chat",
    StateReason.SOURCE_EDITED: "the message it answers was edited",
    StateReason.SOURCE_DELETED: "the message it answers was deleted",
    StateReason.RIGHTS_CHANGED: "the bot's permissions changed",
    StateReason.CONNECTION_DISABLED: "Chat Automation is off or disconnected",
    StateReason.CONNECTION_INVALID: "Telegram no longer accepts the connection",
    StateReason.IGNORE_MODE: "the chat is set to Ignore",
    StateReason.DATA_DELETED: "the chat's data was deleted",
    StateReason.CONSENT_REVOKED: "AI permission was revoked",
    StateReason.INGEST_ERROR: "a message in the chat could not be processed",
    StateReason.RESTORED_FROM_BACKUP: "a backup was restored",
    StateReason.WINDOW_CLOSED: "the 24-hour reply window closed",
    StateReason.MAX_AGE: "the approval period ended",
    StateReason.OWNER_REJECTED: "you rejected it",
    StateReason.POLICY_DENIED: "a required check did not pass",
    StateReason.SENDING_DISABLED: "sending is turned off",
    StateReason.TOO_LONG: "the text is empty or too long",
    StateReason.AUTONOMY_PAUSED: "automatic replies are paused",
    StateReason.SENT: "sent",
    StateReason.NOT_DELIVERED: "Telegram did not send it",
    StateReason.AMBIGUOUS: "delivery is uncertain",
    StateReason.STALE_EXECUTING: "the attempt did not finish",
    StateReason.LATE_RESULT: "a late result arrived",
}

#: States whose card offers the text to copy (FR-DRF-06).
_COPY_READY = frozenset(
    {ActionState.BLOCKED, ActionState.FAILED, ActionState.EXPIRED, ActionState.OUTDATED}
)
#: Of those, the ones an agent proposal can be re-drafted from.
_REDRAFTABLE = frozenset({ActionState.OUTDATED, ActionState.EXPIRED})


# ------------------------------------------------------------ the view
@dataclass(frozen=True, slots=True)
class ApprovalCardView:
    """What one card shows: a read of the action and its context, account-scoped."""

    action_id: int
    account_id: int
    conversation_id: int
    state: ActionState
    reason: StateReason | None
    origin: ActionOrigin
    proactive: bool
    version: int
    text: str | None
    draft_flags: Mapping[str, Any] | None
    expires_at: datetime | None
    executed_at: datetime | None
    error_class: str | None
    approval_token: str | None
    revision_turn_id: int | None
    card_message_id: int | None
    card_state: str | None
    contact_name: str | None
    #: The contact's messages this answers, oldest first, each cut short.
    excerpts: tuple[str, ...]
    #: When the reply window closes, less the safety margin.
    window_end: datetime | None
    timezone: str
    control_chat_id: int | None
    #: `ActionsConfig.sending_enabled`: with sending off an approved card
    #: says so, never "Sending…" (spec §3.3, decisions R17).
    sending_enabled: bool
    #: The live `act` card's token (Cancel edit / Re-draft), if one exists.
    card_token: str | None = None
    #: Re-draft was pressed: the card keeps its state and offers copy only.
    redraft_requested: bool = False

    @property
    def sendable_until(self) -> datetime | None:
        """`min(expires_at, window close - margin)` (spec §3.1 item 5)."""
        values = [v for v in (self.expires_at, self.window_end) if v is not None]
        return min(values) if values else None


async def load_card_view(
    session: AsyncSession, *, account_id: int, action_id: int, sending_enabled: bool
) -> ApprovalCardView | None:
    """The card's view of one action of this account, or None.
    `sending_enabled` is the presenter's `ActionsConfig.sending_enabled`."""
    row = (
        await session.execute(
            select(Action, Conversation.last_contact_msg_at, Contact, Account)
            .join(Conversation, Conversation.id == Action.conversation_id)
            .join(Contact, Contact.id == Conversation.contact_id)
            .join(Account, Account.id == Action.account_id)
            .where(
                Action.id == action_id,
                Action.account_id == account_id,
                Conversation.account_id == account_id,
                Contact.account_id == account_id,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    action, anchor, contact, account = row
    window = closes_at(anchor)
    token = await session.scalar(
        select(PendingInput.token)
        .where(
            PendingInput.account_id == account_id,
            PendingInput.kind == KIND,
            PendingInput.ref_id == action_id,
            PendingInput.token.is_not(None),
        )
        .order_by(PendingInput.id.desc())
        .limit(1)
    )
    return ApprovalCardView(
        action_id=action.id,
        account_id=account_id,
        conversation_id=action.conversation_id,
        state=ActionState(action.state),
        reason=None if action.state_reason is None else StateReason(action.state_reason),
        origin=ActionOrigin(action.origin),
        proactive=action.trigger_turn_id is None,
        version=action.version,
        text=action.text,
        draft_flags=action.draft_flags,
        expires_at=action.expires_at,
        executed_at=action.executed_at,
        error_class=action.error_class,
        approval_token=action.approval_token,
        revision_turn_id=action.revision_turn_id,
        card_message_id=action.card_message_id,
        card_state=action.card_state,
        contact_name=preferred_name(contact.display_name, contact.username),
        excerpts=await _excerpts(session, account_id, action),
        window_end=None if window is None else window - SAFETY_MARGIN,
        timezone=account.timezone,
        control_chat_id=account.control_chat_id,
        sending_enabled=sending_enabled,
        card_token=token,
    )


async def _excerpts(session: AsyncSession, account_id: int, action: Action) -> tuple[str, ...]:
    """The contact messages after the last owner/assistant message, up to the
    draft's base message (or what a dictated message was shown through): at
    most three, oldest first, each cut to 300 characters."""
    upper = action.base_message_id or action.shown_through_message_id
    if upper is None:
        return ()
    alive = (
        Message.account_id == account_id,
        Message.conversation_id == action.conversation_id,
        Message.id <= upper,
        Message.deleted_at.is_(None),
        Message.purged_at.is_(None),
    )
    lower = await session.scalar(
        select(Message.id)
        .where(*alive, Message.direction.in_(("owner_manual", "owner_automated", "assistant_sent")))
        .order_by(Message.id.desc())
        .limit(1)
    )
    query = select(Message.text, Message.content_type).where(*alive, Message.direction == "contact")
    if lower is not None:
        query = query.where(Message.id > lower)
    rows = (await session.execute(query.order_by(Message.id.desc()).limit(EXCERPTS))).all()
    excerpts = []
    for text, content_type in reversed(rows):
        if text is None:
            excerpts.append(f"[{content_type}]")
        elif len(text) > EXCERPT_CHARS:
            excerpts.append(text[:EXCERPT_CHARS].rstrip() + "…")
        else:
            excerpts.append(text)
    return tuple(excerpts)


# ------------------------------------------------------------ rendering
#: A piece of raw text the sanitizer would turn into a real tag: `<b>`,
#: `</i>`, `<a href=…>` (the same shape as `render._ESCAPED_TAG_RE`, before
#: escaping).
_TAG_SHAPED = re.compile(r"<(/?)([a-zA-Z0-9-]+)((?:\s[^&<>]*?)?)>")


def _defuse(match: re.Match[str]) -> str:
    tag, attributes = match.group(2).lower(), match.group(3)
    if tag not in ALLOWED_TAGS or (attributes and not (tag == "a" and "href=" in attributes)):
        return match.group(0)  # the sanitizer escapes it already
    return "\\u003c" + match.group(0)[1:]


def shown(text: str, *, one_line: bool = False) -> str:
    """Content as the owner must see it: hidden and bidi characters visible
    (as on S03.1 cards), and nothing that could turn into markup.

    A line break stays a line break — the card shows the exact text
    (FR-DRF-03), and "copy the text above" must copy it (FR-DRF-06) — except
    in a `one_line` field (a name), where it is made visible like any other
    control character. Every other control character (`\\r`, `\\t`, …) stays
    a visible escape."""
    if one_line:
        visible = render_description(text)
    else:
        visible = "\n".join(render_description(line) for line in text.split("\n"))
    return _TAG_SHAPED.sub(_defuse, visible)


def _their_words(excerpt: str) -> list[str]:
    """One excerpt of the contact's words, every line marked as theirs, so a
    line break in them can never start what looks like a card line of its
    own ("Sendable until …", a state line)."""
    return [
        f"› <i>{shown(line)}</i>" if line else "›"  # noqa: RUF001
        for line in excerpt.split("\n")
    ]


def _copyable(text: str | None) -> bool:
    if text is None or not text.strip():
        return False
    return lifecycle.utf16_len(text) <= COPY_TEXT_MAX


def _copy_button(text: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="📋 Copy text", copy_text=CopyTextButton(text=text))


def _button(label: str, token: str, op: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=encode(KIND, token, op))


def card_keyboard(view: ApprovalCardView) -> InlineKeyboardMarkup | None:
    """Buttons by state (spec §3.1). None: a card without buttons."""
    state = view.state
    if state is ActionState.AWAITING_APPROVAL and view.approval_token:
        token = view.approval_token
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    _button("✅ Approve", token, OP_APPROVE),
                    _button("✏️ Edit", token, OP_EDIT),
                    _button("✖️ Reject", token, OP_REJECT),
                ]
            ]
        )
    if state is ActionState.REVISING:
        if view.card_token is None:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[[_button("Cancel edit", view.card_token, OP_CANCEL_EDIT)]]
        )
    if state not in _COPY_READY:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    if (
        state in _REDRAFTABLE
        and view.origin is ActionOrigin.AGENT_PROPOSAL
        and view.card_token is not None
        and not view.redraft_requested
    ):
        rows.append([_button("🔄 Re-draft", view.card_token, OP_REDRAFT)])
    if _copyable(view.text):
        assert view.text is not None
        rows.append([_copy_button(view.text)])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _header(view: ApprovalCardView) -> str:
    if view.origin is ActionOrigin.OWNER_DICTATED:
        return _HEADERS["dictated"]
    if view.origin is ActionOrigin.DELEGATION_AUTO:
        return _HEADERS["automatic"]
    return _HEADERS["assist" if view.proactive else "requested"]


def _short(reason: StateReason | None) -> str:
    return "" if reason is None else f" — {REASON_SHORT.get(reason, reason.value)}"


def state_line(view: ApprovalCardView) -> str:
    """The FR-ACT-02 label of the action's state; never "sent" unless it was."""
    state = view.state
    if state is ActionState.REVISING:
        if view.revision_turn_id is None:
            return f"✏️ Editing — {ANSWER_EDIT}"
        return REVISING_TEXT
    if state is ActionState.APPROVED and not view.sending_enabled:
        # Nothing can be sent: never "Sending…" (R17); the executor's Tx A
        # then ends it `blocked`/`sending_disabled` with copy-ready text.
        return f"<b>{ANSWER_APPROVED_SENDING_OFF}</b>"
    if state is ActionState.SUCCEEDED:
        sent_at = view.executed_at
        when = "" if sent_at is None else " " + _clock_time(sent_at, view.timezone)
        return f"{STATE_LABELS[state]}{when}"
    if state in {
        ActionState.FAILED,
        ActionState.OUTDATED,
        ActionState.EXPIRED,
        ActionState.CANCELLED,
        ActionState.BLOCKED,
    }:
        return f"<b>{STATE_LABELS[state]}</b>{_short(view.reason)}"
    return f"<b>{STATE_LABELS[state]}</b>"


def _clock_time(value: datetime, timezone: str) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError, KeyError:
        zone = ZoneInfo("UTC")
    return value.astimezone(zone).strftime("%H:%M")


def _explanation(view: ApprovalCardView) -> str | None:
    if view.reason is None or view.state in LIVE_CARD or view.state is ActionState.SUCCEEDED:
        return None
    if view.state in {ActionState.APPROVED, ActionState.EXECUTING}:
        return None
    return STATE_REASON_EXPLANATIONS.get(view.reason)


def _warnings(view: ApprovalCardView) -> list[str]:
    """From `draft_flags` only (decisions R8), never from `policy_decision`;
    the owner's own dictated words carry none."""
    flags = view.draft_flags
    if view.origin is ActionOrigin.OWNER_DICTATED or not flags:
        return []
    lines = []
    if flags.get("makes_commitment") is True:
        lines.append("⚠️ Contains a commitment (date, amount or promise) — check it")
    if flags.get("sensitive_topic") is True:
        lines.append("⚠️ Sensitive topic")
    return lines


@dataclass(frozen=True, slots=True)
class RenderedCard:
    text: str
    keyboard: InlineKeyboardMarkup | None
    #: Sent first, as its own message, when the draft does not fit its card.
    full_text: str | None = None


def _compose(view: ApprovalCardView, body: str, *, copy_note: str | None) -> str:
    name = shown(view.contact_name, one_line=True) if view.contact_name else "this contact"
    parts = [_header(view), "", f"To: <b>{name}</b>"]
    if view.excerpts:
        parts += ["", "Answering (their words):"]
        for excerpt in view.excerpts:
            parts += _their_words(excerpt)
    parts += ["", body]
    warnings = _warnings(view)
    if warnings:
        parts += ["", *warnings]
    parts.append("")
    until = view.sendable_until
    if view.state in LIVE_CARD and until is not None:
        parts.append(f"Sendable until {local_time(until, view.timezone)}")
    parts.append(state_line(view))
    if (explanation := _explanation(view)) is not None:
        parts.append(f"<i>{explanation}</i>")
    if view.redraft_requested:
        parts.append("Re-draft requested — a new card will follow.")
    if copy_note is not None:
        parts.append(copy_note)
    return "\n".join(parts)


def render_card(view: ApprovalCardView, *, timezone: str | None = None) -> RenderedCard:
    """The card's text (control-chat HTML) and buttons for the view's state.

    `timezone` overrides the account's (for tests and previews). A text too
    long for one card moves to a message of its own sent first; the card
    keeps its first 600 characters. The approval still binds to the stored
    version, and the executor sends exactly the stored text.
    """
    if timezone is not None:
        view = replace(view, timezone=timezone)
    keyboard = card_keyboard(view)
    needs_copy_note = view.state in _COPY_READY and view.text is not None
    copy_note = None
    if needs_copy_note and not _copyable(view.text):
        copy_note = "Copy the text above to send it yourself."
    if view.text is None:
        return RenderedCard(
            _compose(view, "<i>(The text is no longer stored.)</i>", copy_note=None), keyboard
        )
    full = _compose(view, f"<blockquote>{shown(view.text)}</blockquote>", copy_note=copy_note)
    if len(sanitize(full)) <= CARD_BUDGET:
        return RenderedCard(full, keyboard)
    preview = shown(view.text[:PREVIEW_CHARS].rstrip()) + "…"
    body = (
        f"Full text in the message above ({len(view.text)} characters). The start:\n"
        f"<blockquote>{preview}</blockquote>"
    )
    full_text = f"📝 Full text ({len(view.text)} characters):\n\n{shown(view.text)}"
    return RenderedCard(_compose(view, body, copy_note=copy_note), keyboard, full_text)


# ------------------------------------------------------------ helpers
async def _lock(session: AsyncSession, account_id: int, conversation_id: int) -> bool:
    """§9.4 order: the account, then the conversation, both `FOR SHARE`."""
    found = await session.scalar(
        select(Account.id).where(Account.id == account_id).with_for_update(read=True)
    )
    if found is None:
        return False
    locked = await session.scalar(
        select(Conversation.id)
        .where(Conversation.id == conversation_id, Conversation.account_id == account_id)
        .with_for_update(read=True)
    )
    return locked is not None


async def ensure_card_token(
    session: AsyncSession, *, account_id: int, action_id: int, clock: Clock
) -> str:
    """The live `act` card of this action (Cancel edit / Re-draft), created
    when there is none. Its op is checked against the action's state at the
    press, so one token serves whichever button the state shows."""
    token = await session.scalar(
        select(PendingInput.token)
        .where(
            PendingInput.account_id == account_id,
            PendingInput.kind == KIND,
            PendingInput.ref_id == action_id,
            PendingInput.token.is_not(None),
            PendingInput.expires_at > clock.now(),
        )
        .order_by(PendingInput.id.desc())
        .limit(1)
    )
    if token is not None:
        return token
    card = await service.create_card(
        session, account_id=account_id, kind=KIND, clock=clock, ref_id=action_id
    )
    assert card.token is not None
    return card.token


async def clear_edit_inputs(
    session: AsyncSession, *, account_id: int | None, action_ids: Sequence[int]
) -> int:
    """Drop the Edit question of these actions (never another question)."""
    if not action_ids:
        return 0
    statement = delete(PendingInput).where(
        PendingInput.kind == DRAFT_EDIT,
        PendingInput.token.is_(None),
        PendingInput.ref_id.in_(list(action_ids)),
    )
    if account_id is not None:
        statement = statement.where(PendingInput.account_id == account_id)
    result = await session.execute(statement.execution_options(synchronize_session=False))
    return int(getattr(result, "rowcount", 0) or 0)


def _needs_card_token(view: ApprovalCardView) -> bool:
    return view.state is ActionState.REVISING or (
        view.state in _REDRAFTABLE and view.origin is ActionOrigin.AGENT_PROPOSAL
    )


def _edit_ttl(expires_at: datetime | None, now: datetime) -> timedelta:
    if expires_at is None:
        return EDIT_INPUT_TTL
    return max(timedelta(seconds=1), min(expires_at - now, EDIT_INPUT_TTL))


# ------------------------------------------------------------ the presenter
class ApprovalCards:
    """`ApprovalCardPresenter` (F0-interfaces §8.2), the `act` press handler,
    the `draft_edit` input handler and the card jobs. Built once in
    `_wire_m04`; holds no session or connection between calls."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        gateway: OutboundGateway,
        clock: Clock,
        config: ActionsConfig,
    ) -> None:
        self._sf = session_factory
        self._gateway = gateway
        self._clock = clock
        self._config = config
        #: Failed edits per action since its last good one (process-local).
        self._failures: dict[int, int] = {}

    # ---------------------------------------------------------- the port
    def present_after_commit(self, session: AsyncSession, action_id: int) -> None:
        async def present(_: async_sessionmaker[AsyncSession]) -> None:
            await self.present(action_id)

        after_commit(session, present)

    def sync_after_commit(self, session: AsyncSession, action_id: int) -> None:
        async def sync(_: async_sessionmaker[AsyncSession]) -> None:
            await self.sync(action_id)

        after_commit(session, sync)

    # ---------------------------------------------------------- show / edit
    async def _prepare(self, action_id: int) -> ApprovalCardView | None:
        """The view, in a short transaction that also makes the card token
        the view's buttons need."""
        async with transaction(self._sf) as uow:
            session = uow.session
            account_id = await session.scalar(
                select(Action.account_id).where(Action.id == action_id)
            )
            if account_id is None:
                return None
            view = await load_card_view(
                session,
                account_id=account_id,
                action_id=action_id,
                sending_enabled=self._config.sending_enabled,
            )
            if view is None or not _needs_card_token(view):
                return view
            token = await ensure_card_token(
                session, account_id=account_id, action_id=action_id, clock=self._clock
            )
            return replace(view, card_token=token)

    async def present(self, action_id: int) -> SendResult | None:
        """A new card for the action (an existing card is edited instead)."""
        view = await self._prepare(action_id)
        if view is None:
            return None
        if view.card_message_id is not None:
            return await self._edit(view)
        return await self._send_new(view)

    async def sync(self, action_id: int) -> SendResult | None:
        """Show the action's current state on its card, in place."""
        view = await self._prepare(action_id)
        if view is None:
            return None
        if view.card_message_id is None:
            # Never shown (its presentation failed): a live card is shown now.
            return await self._send_new(view) if view.state in LIVE_CARD else None
        return await self._edit(view)

    async def show_new_version(self, action_id: int) -> SendResult | None:
        """A revised version: edited in place, or — when its text moves above
        the card — a new card below that text, the old card retired, so
        "full text in the message above" stays true."""
        view = await self._prepare(action_id)
        if view is None:
            return None
        if view.card_message_id is None or render_card(view).full_text is None:
            return await self.sync(action_id)
        old = view.card_message_id
        result = await self._send_new(view)
        target = self._target(view)
        if old is not None and target is not None and result is not None and result.ok:
            await self._gateway.edit_control_text(target, old, RETIRED_CARD_TEXT)
        return result

    def _target(self, view: ApprovalCardView) -> ControlChat | None:
        if view.control_chat_id is None:
            logger.warning("approval_card_no_control_chat", action_id=view.action_id)
            return None
        return ControlChat(account_id=view.account_id, chat_id=view.control_chat_id)

    async def _send_new(self, view: ApprovalCardView) -> SendResult | None:
        target = self._target(view)
        if target is None:
            return None
        card = render_card(view)
        if card.full_text is not None:
            first = await self._gateway.send_text(target, card.full_text)
            if not first.ok:
                # The card would point at a message that is not there; a
                # live card is presented by the sync instead.
                logger.warning(
                    "approval_card_not_sent", action_id=view.action_id, error=first.error_class
                )
                return first
        result = await self._gateway.send_text(target, card.text, keyboard=card.keyboard)
        if result.ok and result.message_id is not None:
            await self._record(view, message_id=result.message_id)
            logger.info("approval_card_sent", action_id=view.action_id, state=view.state.value)
        else:
            logger.warning(
                "approval_card_not_sent", action_id=view.action_id, error=result.error_class
            )
        return result

    async def _edit(self, view: ApprovalCardView) -> SendResult | None:
        target = self._target(view)
        if target is None or view.card_message_id is None:
            return None
        card = render_card(view)
        result = await self._gateway.edit_control_text(
            target, view.card_message_id, card.text, keyboard=card.keyboard
        )
        # A refusal Telegram answered (e.g. "message is not modified", or a
        # card the owner deleted) cannot succeed by repeating it: settled.
        if result.ok or result.error_class is ErrorClass.BAD_REQUEST:
            self._failures.pop(view.action_id, None)
            await self._record(view, message_id=view.card_message_id)
            return result
        failures = self._failures.get(view.action_id, 0) + 1
        self._failures[view.action_id] = failures
        if failures >= SYNC_MAX_FAILURES:
            logger.error("approval_card_sync_gave_up", action_id=view.action_id, failures=failures)
        else:
            logger.warning(
                "approval_card_edit_failed",
                action_id=view.action_id,
                error=result.error_class,
                failures=failures,
            )
        return result

    async def _record(self, view: ApprovalCardView, *, message_id: int) -> None:
        """The card shows `view.state`; a newer version than the one shown
        leaves `card_state` NULL, so the sync shows it next minute."""
        async with transaction(self._sf) as uow:
            await uow.session.execute(
                update(Action)
                .where(Action.id == view.action_id, Action.account_id == view.account_id)
                .values(
                    card_message_id=message_id,
                    card_state=case((Action.version == view.version, view.state.value), else_=None),
                )
                .execution_options(synchronize_session=False)
            )

    # ---------------------------------------------------------- presses
    async def handle_press(
        self,
        session: AsyncSession,
        gateway: OutboundGateway,
        clock: Clock,
        scheduler: JobScheduler | None,
        account: Account,
        press: CardPress,
    ) -> HandlerResult:
        """An `act` button (spec §3.3). The token is resolved within the
        presser's account only: the action's approval token for Approve,
        Edit and Reject, else the action's `act` card for Cancel edit and
        Re-draft. Anything not found or no longer valid answers "no longer
        active"; a known action's card is synced after the commit."""
        found = await session.execute(
            select(Action.id, Action.conversation_id).where(
                Action.account_id == account.id, Action.approval_token == press.token
            )
        )
        live = found.one_or_none()
        if live is not None:
            if press.op not in _TOKEN_OPS:
                return self._settle(session, gateway, press, CALLBACK_EXPIRED_TEXT, live.id)
            if not await _lock(session, account.id, live.conversation_id):
                return self._settle(session, gateway, press, CALLBACK_EXPIRED_TEXT, None)
            if press.op == OP_APPROVE:
                return await self._approve(
                    session, gateway, clock, scheduler, account, press, live.id
                )
            if press.op == OP_EDIT:
                return await self._start_edit(session, gateway, clock, account, press, live.id)
            return await self._reject(session, gateway, clock, account, press, live.id)
        card = await service.claim_card(
            session, account_id=account.id, token=press.token, clock=clock
        )
        if card is None or card.kind != KIND or card.ref_id is None or press.op not in _CARD_OPS:
            action_id = card.ref_id if card is not None and card.kind == KIND else None
            return self._settle(session, gateway, press, CALLBACK_EXPIRED_TEXT, action_id)
        action = (
            await session.execute(
                select(
                    Action.id, Action.conversation_id, Action.state, Action.version, Action.origin
                ).where(Action.id == card.ref_id, Action.account_id == account.id)
            )
        ).one_or_none()
        if action is None or not await _lock(session, account.id, action.conversation_id):
            return self._settle(session, gateway, press, CALLBACK_EXPIRED_TEXT, None)
        if press.op == OP_CANCEL_EDIT:
            return await self._cancel_edit(session, gateway, clock, account, press, action)
        return await self._redraft(session, gateway, clock, scheduler, account, press, action)

    def _settle(
        self,
        session: AsyncSession,
        gateway: OutboundGateway,
        press: CardPress,
        answer: str,
        action_id: int | None,
        *,
        note: str = "no_longer_active",
        handled: bool = False,
        redraft_requested: bool = False,
    ) -> HandlerResult:
        """Answer the press and update the card, after the commit only."""

        async def announce(_: async_sessionmaker[AsyncSession]) -> None:
            await gateway.answer_callback(press.callback_id, answer)
            if action_id is None:
                return
            if redraft_requested:
                await self._show_redraft_requested(action_id)
            else:
                await self.sync(action_id)

        after_commit(session, announce)
        return HandlerResult(handled, "card", None, note=note)

    async def _show_redraft_requested(self, action_id: int) -> None:
        view = await self._prepare(action_id)
        if view is not None and view.card_message_id is not None:
            await self._edit(replace(view, redraft_requested=True))

    async def _approve(
        self,
        session: AsyncSession,
        gateway: OutboundGateway,
        clock: Clock,
        scheduler: JobScheduler | None,
        account: Account,
        press: CardPress,
        action_id: int,
    ) -> HandlerResult:
        if scheduler is None:
            raise RuntimeError("a job scheduler is required to approve an action")
        result = await lifecycle.approve(
            session, account_id=account.id, token=press.token, now=clock.now()
        )
        if result.status is lifecycle.ApproveStatus.APPROVED:
            # In this transaction: approval and its execution commit together
            # (§9.5). No queueing lock, so no conflict can abort the press.
            connection = await session.connection()
            raw = await connection.get_raw_connection()
            await scheduler.defer(
                EXECUTE_TASK, args={"action_id": action_id}, connection=raw.driver_connection
            )
            answer = ANSWER_SENDING if self._config.sending_enabled else ANSWER_APPROVED_SENDING_OFF
            logger.info("action_approved_by_owner", account_id=account.id, action_id=action_id)
            return self._settle(
                session, gateway, press, answer, action_id, note="approved", handled=True
            )
        if result.status is lifecycle.ApproveStatus.EXPIRED:
            return self._settle(session, gateway, press, ANSWER_EXPIRED, action_id, note="expired")
        return self._settle(session, gateway, press, CALLBACK_EXPIRED_TEXT, action_id)

    async def _start_edit(
        self,
        session: AsyncSession,
        gateway: OutboundGateway,
        clock: Clock,
        account: Account,
        press: CardPress,
        action_id: int,
    ) -> HandlerResult:
        now = clock.now()
        version = await lifecycle.start_edit(
            session, account_id=account.id, token=press.token, now=now
        )
        row = (
            await session.execute(
                select(Action.state, Action.expires_at, Action.card_message_id).where(
                    Action.id == action_id, Action.account_id == account.id
                )
            )
        ).one()
        if version is None:
            if row.state == ActionState.EXPIRED.value:
                return self._settle(
                    session, gateway, press, ANSWER_EXPIRED, action_id, note="expired"
                )
            return self._settle(session, gateway, press, CALLBACK_EXPIRED_TEXT, action_id)
        await ensure_card_token(session, account_id=account.id, action_id=action_id, clock=clock)
        await service.set_pending_input(
            session,
            account_id=account.id,
            kind=DRAFT_EDIT,
            clock=clock,
            ref_id=action_id,
            prompt_message_id=row.card_message_id,
            ttl=_edit_ttl(row.expires_at, now),
        )
        return self._settle(
            session, gateway, press, ANSWER_EDIT, action_id, note="editing", handled=True
        )

    async def _reject(
        self,
        session: AsyncSession,
        gateway: OutboundGateway,
        clock: Clock,
        account: Account,
        press: CardPress,
        action_id: int,
    ) -> HandlerResult:
        if await lifecycle.reject(
            session, account_id=account.id, token=press.token, now=clock.now()
        ):
            return self._settle(
                session, gateway, press, ANSWER_REJECTED, action_id, note="rejected", handled=True
            )
        return self._settle(session, gateway, press, CALLBACK_EXPIRED_TEXT, action_id)

    async def _cancel_edit(
        self,
        session: AsyncSession,
        gateway: OutboundGateway,
        clock: Clock,
        account: Account,
        press: CardPress,
        action: Any,
    ) -> HandlerResult:
        if action.state != ActionState.REVISING.value:
            return self._settle(session, gateway, press, CALLBACK_EXPIRED_TEXT, action.id)
        token = await lifecycle.cancel_edit(
            session,
            account_id=account.id,
            action_id=action.id,
            expected_version=action.version,
            now=clock.now(),
        )
        if token is None:
            return self._settle(session, gateway, press, CALLBACK_EXPIRED_TEXT, action.id)
        await clear_edit_inputs(session, account_id=account.id, action_ids=[action.id])
        return self._settle(
            session,
            gateway,
            press,
            ANSWER_EDIT_CANCELLED,
            action.id,
            note="edit_cancelled",
            handled=True,
        )

    async def _redraft(
        self,
        session: AsyncSession,
        gateway: OutboundGateway,
        clock: Clock,
        scheduler: JobScheduler | None,
        account: Account,
        press: CardPress,
        action: Any,
    ) -> HandlerResult:
        if (
            action.state not in {state.value for state in _REDRAFTABLE}
            or action.origin != ActionOrigin.AGENT_PROPOSAL.value
        ):
            return self._settle(session, gateway, press, CALLBACK_EXPIRED_TEXT, action.id)
        if scheduler is None:
            raise RuntimeError("a job scheduler is required to re-draft")
        try:
            # No flag: deferred in this transaction, in a SAVEPOINT, so a
            # second tap while one is queued cannot abort the press (R17).
            await request_draft(
                session,
                account_id=account.id,
                conversation_id=action.conversation_id,
                reason=DraftReason.REDRAFT,
                scheduler=scheduler,
                clock=clock,
                debounce=self._config.draft_debounce,
            )
        except NotInAccountError:
            return self._settle(session, gateway, press, CALLBACK_EXPIRED_TEXT, None)
        return self._settle(
            session,
            gateway,
            press,
            ANSWER_REDRAFTING,
            action.id,
            note="redraft_requested",
            handled=True,
            redraft_requested=True,
        )

    # ---------------------------------------------------------- the edit
    async def handle_edit_input(
        self,
        session: AsyncSession,
        gateway: OutboundGateway,
        clock: Clock,
        scheduler: JobScheduler | None,
        account: Account,
        message: IncomingMessage,
        pending: PendingInput,
    ) -> HandlerResult:
        """The owner's answer to Edit (spec §3.4): replacement text or an
        instruction, decided by the revision run (P2-T08). The dispatcher
        never passes a forwarded or quoted message here (M03 D49)."""
        target = ControlChat(account_id=account.id, chat_id=message.chat_id)
        await service.clear_pending_input(session, account_id=account.id)
        row = (
            await session.execute(
                select(Action.id, Action.conversation_id, Conversation.contact_id)
                .join(Conversation, Conversation.id == Action.conversation_id)
                .where(
                    Action.id == pending.ref_id,
                    Action.account_id == account.id,
                    Conversation.account_id == account.id,
                )
            )
        ).one_or_none()
        if row is None or not await _lock(session, account.id, row.conversation_id):
            return self._tell(session, gateway, target, EDIT_CLOSED_TEXT, None, note="closed")
        action = (
            await session.execute(
                select(Action.state, Action.version).where(
                    Action.id == row.id, Action.account_id == account.id
                )
            )
        ).one()
        if action.state != ActionState.REVISING.value:
            return self._tell(session, gateway, target, EDIT_CLOSED_TEXT, row.id, note="closed")
        if scheduler is None:
            raise RuntimeError("a job scheduler is required to revise a draft")
        turn = await service.record_control_turn(
            session,
            account_id=account.id,
            role="owner",
            text=message.text,
            telegram_message_id=message.telegram_message_id,
            # Now, by the injected clock: the revising reconciler measures
            # how long the revision has been waiting from this time.
            created_at=clock.now(),
            # The owner's own words; `refs` lets deleting that chat redact it.
            contains_managed=False,
            refs={"conversation_ids": [row.conversation_id], "contact_ids": [row.contact_id]},
        )
        recorded = await lifecycle.set_revision_turn(
            session,
            account_id=account.id,
            action_id=row.id,
            expected_version=action.version,
            turn_id=turn.id,
        )
        if not recorded:  # pragma: no cover - the row is locked and revising
            return self._tell(session, gateway, target, EDIT_CLOSED_TEXT, row.id, note="closed")
        await _defer_revision(session, scheduler, action_id=row.id, turn_id=turn.id)
        logger.info("revision_requested", account_id=account.id, action_id=row.id, turn_id=turn.id)
        return self._tell(
            session, gateway, target, REVISING_TEXT, row.id, note="revising", handled=True
        )

    def _tell(
        self,
        session: AsyncSession,
        gateway: OutboundGateway,
        target: ControlChat,
        text: str,
        action_id: int | None,
        *,
        note: str,
        handled: bool = False,
    ) -> HandlerResult:
        async def announce(_: async_sessionmaker[AsyncSession]) -> None:
            await gateway.send_text(target, text)
            if action_id is not None:
                await self.sync(action_id)

        after_commit(session, announce)
        return HandlerResult(handled, DRAFT_EDIT, None, note=note)

    # ---------------------------------------------------------- jobs
    async def expire_due(self) -> int:
        """`system.expire_actions`: S04.1's `expire_due` (one rule, one reason
        order), the Edit questions of what expired cleared, cards updated."""
        async with transaction(self._sf) as uow:
            expired = await lifecycle.expire_due(uow.session, now=self._clock.now())
            await clear_edit_inputs(uow.session, account_id=None, action_ids=expired)
            for action_id in expired:
                self.sync_after_commit(uow.session, action_id)
        if expired:
            logger.info("actions_expired", count=len(expired))
        return len(expired)

    async def sync_cards(self) -> int:
        """`system.sync_action_cards`: edit every card that shows an old
        state, and present a live card that was never shown."""
        now = self._clock.now()
        async with self._sf() as session:
            unsynced = list(
                await session.scalars(
                    select(Action.id)
                    .where(
                        Action.card_message_id.is_not(None),
                        Action.card_state.is_distinct_from(Action.state),
                    )
                    .order_by(Action.id)
                    .limit(SYNC_BATCH)
                )
            )
            unshown = list(
                await session.scalars(
                    select(Action.id)
                    .where(
                        Action.card_message_id.is_(None),
                        Action.state.in_([state.value for state in LIVE_CARD]),
                        Action.created_at < now - PRESENT_GRACE,
                    )
                    .order_by(Action.id)
                    .limit(SYNC_BATCH)
                )
            )
        done = 0
        for action_id in unsynced:
            if self._failures.get(action_id, 0) >= SYNC_MAX_FAILURES:
                continue
            await self.sync(action_id)
            done += 1
        for action_id in unshown:
            await self.present(action_id)
            done += 1
        return done

    async def notify(self, account_id: int, text: str) -> None:
        """A short control-chat message about a card (after a commit)."""
        async with self._sf() as session:
            chat_id = await session.scalar(
                select(Account.control_chat_id).where(Account.id == account_id)
            )
        if chat_id is None:
            return
        await self._gateway.send_text(ControlChat(account_id=account_id, chat_id=chat_id), text)

    async def give_up_revision(
        self, session: AsyncSession, *, account_id: int, action_id: int, version: int, now: datetime
    ) -> bool:
        """A revision that never finished (the revising reconciler): back to
        the same version's card, the owner told after the commit."""
        token = await lifecycle.revision_failed(
            session, account_id=account_id, action_id=action_id, expected_version=version, now=now
        )
        if token is None:
            return False

        async def announce(_: async_sessionmaker[AsyncSession]) -> None:
            await self.notify(account_id, REVISION_TIMED_OUT_TEXT)
            await self.sync(action_id)

        after_commit(session, announce)
        return True


async def _defer_revision(
    session: AsyncSession, scheduler: JobScheduler, *, action_id: int, turn_id: int
) -> None:
    """`replies.revise` in the caller's transaction, inside a SAVEPOINT: a
    queueing-lock conflict rolls back only the savepoint, never the edit
    (F0-interfaces §6.1); the revising reconciler covers a job lost there."""
    savepoint = await session.begin_nested()
    try:
        connection = await session.connection()
        raw = await connection.get_raw_connection()
        job_id = await scheduler.defer(
            REVISE_TASK,
            args={"action_id": action_id, "turn_id": turn_id},
            queueing_lock=revise_queueing_lock(action_id),
            connection=raw.driver_connection,
        )
    except BaseException:
        await savepoint.rollback()
        raise
    if job_id is None:
        await savepoint.rollback()
    else:
        await savepoint.commit()


# ------------------------------------------------------------ replies.revise
@dataclass(frozen=True, slots=True)
class _Revising:
    account_id: int
    conversation_id: int
    version: int
    origin: ActionOrigin


class RevisionJob:
    """`replies.revise(action_id, turn_id)` (spec §3.4; queue `llm`, no lock,
    no retry). The model runs with no lock held; the new version is applied
    in a short transaction only if the action is still being revised with
    this turn and version — so a revision finishing late never changes what
    was or will be sent."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        cards: ApprovalCards,
        drafter: Drafter,
        clock: Clock,
        scheduler: JobScheduler,
    ) -> None:
        self._sf = session_factory
        self._cards = cards
        self._drafter = drafter
        self._clock = clock
        self._scheduler = scheduler

    async def run(self, action_id: int, turn_id: int) -> str:
        before = await self._revising(action_id, turn_id)
        if before is None:
            logger.info("revision_stale", action_id=action_id)
            await self._hand_over(action_id, turn_id)
            return "stale"
        outcome = await self._drafter.revise(
            account_id=before.account_id, action_id=action_id, turn_id=turn_id
        )
        result = outcome.result
        if outcome.status is not DraftStatus.DRAFTED or result is None:
            return await self._failed(before, action_id, turn_id, outcome)
        if result.revision_kind == "replacement":
            # The owner's words, verbatim — never the model's rewrite (P2-T08).
            text = await self._turn_text(before.account_id, turn_id)
            if text is None:
                return await self._failed(before, action_id, turn_id, outcome)
        else:
            text = result.text
        return await self._apply(before, action_id, turn_id, text, outcome)

    async def _revising(self, action_id: int, turn_id: int) -> _Revising | None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(
                        Action.account_id, Action.conversation_id, Action.version, Action.origin
                    ).where(
                        Action.id == action_id,
                        Action.state == ActionState.REVISING.value,
                        Action.revision_turn_id == turn_id,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return _Revising(row[0], row[1], row[2], ActionOrigin(row[3]))

    async def _hand_over(self, action_id: int, turn_id: int) -> None:
        """This job's turn is no longer the action's (cancelled and edited
        again while it waited): the current turn's job may have been refused
        by this one's queueing lock, so it is deferred now (a conflict means
        it is queued already)."""
        async with self._sf() as session:
            current = await session.scalar(
                select(Action.revision_turn_id).where(
                    Action.id == action_id, Action.state == ActionState.REVISING.value
                )
            )
        if current is None or current == turn_id:
            return
        await self._scheduler.defer(
            REVISE_TASK,
            args={"action_id": action_id, "turn_id": current},
            queueing_lock=revise_queueing_lock(action_id),
        )

    async def _turn_text(self, account_id: int, turn_id: int) -> str | None:
        async with self._sf() as session:
            text: str | None = await session.scalar(
                select(ControlTurn.text).where(
                    ControlTurn.id == turn_id,
                    ControlTurn.account_id == account_id,
                    ControlTurn.role == "owner",
                )
            )
        return text

    async def _still_revising(
        self, session: AsyncSession, before: _Revising, action_id: int, turn_id: int
    ) -> bool:
        """Under the §9.4 locks: still this turn's revision of this version."""
        if not await _lock(session, before.account_id, before.conversation_id):
            return False
        found = await session.scalar(
            select(Action.id)
            .where(
                Action.id == action_id,
                Action.account_id == before.account_id,
                Action.state == ActionState.REVISING.value,
                Action.version == before.version,
                Action.revision_turn_id == turn_id,
            )
            .with_for_update()
        )
        return found is not None

    async def _apply(
        self,
        before: _Revising,
        action_id: int,
        turn_id: int,
        text: str,
        outcome: DraftOutcome,
    ) -> str:
        now = self._clock.now()
        dictated = before.origin is ActionOrigin.OWNER_DICTATED
        async with transaction(self._sf) as uow:
            session = uow.session
            if not await self._still_revising(session, before, action_id, turn_id):
                logger.info("revision_stale", action_id=action_id)
                return "stale"
            assert outcome.result is not None
            applied = await lifecycle.apply_new_version(
                session,
                account_id=before.account_id,
                action_id=action_id,
                expected_version=before.version,
                text=text,
                draft_run_id=outcome.run_id,
                # The owner's own dictated words carry no draft flags (R8).
                draft_flags=None if dictated else draft_flags(outcome.result),
                shown_through_message_id=outcome.shown_through_message_id,
                base_message_id=None if dictated else outcome.base_message_id,
                now=now,
            )
            status = applied.status
            if status in {lifecycle.VersionStatus.TOO_LONG, lifecycle.VersionStatus.EMPTY}:
                await self._ask_again(session, before, action_id, text, status, now)
                return status.value
            if status is lifecycle.VersionStatus.APPLIED:

                async def show(_: async_sessionmaker[AsyncSession]) -> None:
                    await self._cards.show_new_version(action_id)

                after_commit(session, show)
        logger.info("revision_finished", action_id=action_id, status=status.value)
        return status.value

    async def _ask_again(
        self,
        session: AsyncSession,
        before: _Revising,
        action_id: int,
        text: str,
        status: lifecycle.VersionStatus,
        now: datetime,
    ) -> None:
        """R7: the row stays `revising`, no version is made (nothing over
        4096 characters ever becomes approvable); the owner is asked again."""
        await lifecycle.set_revision_turn(
            session,
            account_id=before.account_id,
            action_id=action_id,
            expected_version=before.version,
            turn_id=None,
        )
        expires_at = await session.scalar(select(Action.expires_at).where(Action.id == action_id))
        await service.set_pending_input(
            session,
            account_id=before.account_id,
            kind=DRAFT_EDIT,
            clock=self._clock,
            ref_id=action_id,
            ttl=_edit_ttl(expires_at, now),
        )
        message = (
            REVISION_EMPTY_TEXT
            if status is lifecycle.VersionStatus.EMPTY
            else REVISION_TOO_LONG_TEXT.format(
                length=lifecycle.utf16_len(text), limit=MAX_TEXT_LENGTH
            )
        )
        account_id = before.account_id

        async def announce(_: async_sessionmaker[AsyncSession]) -> None:
            await self._cards.notify(account_id, message)
            await self._cards.sync(action_id)

        after_commit(session, announce)

    async def _failed(
        self, before: _Revising, action_id: int, turn_id: int, outcome: DraftOutcome
    ) -> str:
        """The run did not produce a usable text (refused, invalid, no
        consent, over budget, …): the previous version's card is back."""
        async with transaction(self._sf) as uow:
            session = uow.session
            if not await self._still_revising(session, before, action_id, turn_id):
                return "stale"
            token = await lifecycle.revision_failed(
                session,
                account_id=before.account_id,
                action_id=action_id,
                expected_version=before.version,
                now=self._clock.now(),
            )
            if token is not None:
                account_id = before.account_id

                async def announce(_: async_sessionmaker[AsyncSession]) -> None:
                    await self._cards.notify(account_id, REVISION_FAILED_TEXT)
                    await self._cards.sync(action_id)

                after_commit(session, announce)
        logger.info(
            "revision_failed",
            action_id=action_id,
            status=outcome.status.value,
            error_class=outcome.error_class,
        )
        return "failed"
