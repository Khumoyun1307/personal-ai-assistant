"""The digest in the control chat: rendering, `/digest` and its "Open"
buttons (S05.3b §3.4, §3.6; P2-T20; F0-interfaces §7).

- `render_digest` turns `inbox.digest.DigestContent` into the message: HTML
  with every contact name, reason and title escaped, times in the owner's
  timezone, English fixed texts (P2-O11); the gateway splits it and puts the
  buttons on the last chunk.
- `/digest` (a `ControlRoutes` command) defers `digest.build` for the period
  ending now, rounded down to the minute — a double tap within the minute is
  one digest; it works whether or not flag `digest.daily` is on.
- `dg:<token>:op` opens the chat's panel after re-checking, within the
  pressing owner's account, that the card and the chat are theirs; anything
  else is "no longer active" (§6 invariant 12).

Database work happens in the handler's transaction; Telegram calls and the
job defer run after its commit (ARCHITECTURE §6.7; F0-interfaces §6.1: a
queueing-locked defer never runs inside the caller's transaction).
"""

from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, PendingInput
from assistant.conversations import visibility
from assistant.core.clock import Clock
from assistant.core.jobs import DIGEST_TASK, JobScheduler, digest_queueing_lock
from assistant.db.uow import after_commit, transaction
from assistant.inbox.digest import (
    DIGEST_CARD,
    OP_OPEN_CHAT,
    ChatDeletions,
    DigestContent,
    OwnerDeletion,
    TaskLine,
)
from assistant.observability.logging import get_logger
from assistant.telegram.control.handlers import (
    CALLBACK_EXPIRED_TEXT,
    CardPress,
    HandlerResult,
    IncomingMessage,
)
from assistant.telegram.control.panels import Screen, panel_screen
from assistant.telegram.gateway import ControlChat, OutboundGateway

logger = get_logger(__name__)

QUIET_DAY_TEXT = "Quiet day — nothing needs you."
NOT_AVAILABLE_TEXT = "The digest is not available right now. Please try again later."
SET_UP_FIRST_TEXT = "Send /start to finish setting me up first."
OPENING_TEXT = "Opening the chat"
UNNAMED = "A contact"
_ALERT_LABELS = {
    "triage_needs_reply": "chat(s) waiting for a reply",
    "triage_suspicious": "message burst(s) that looked like instructions to the assistant",
    "task_closed": "task(s) closed automatically",
    "commitment_due": "commitment(s) due",
    "monitor_match": "monitor match(es)",
}
_SCOPES = {
    "conversation": "one chat",
    "contact": "one contact's chats",
    "everything": "everything",
    "managed": "data from managed chats",
}


# ------------------------------------------------------------ rendering
def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError, ValueError:
        return ZoneInfo("UTC")


def _when(moment: datetime, zone: ZoneInfo) -> str:
    return moment.astimezone(zone).strftime("%d %b %H:%M")


def _name(name: str | None) -> str:
    return html.escape(name or UNNAMED)


def _commitment(task: TaskLine, zone: ZoneInfo, period_end: datetime) -> str:
    who = _name(task.name)
    # Who promised what (S03.7 attribution): the task's kind says.
    side = f"you promised {who}" if task.kind == "owner_commitment" else f"{who} promised"
    due = ""
    if task.due_at is not None:
        due = f", due {_when(task.due_at, zone)}"
        if task.due_at < period_end:
            due += " (overdue)"
    return f"• {html.escape(task.title)} — {side}{due}"


def _deleted(chat: ChatDeletions) -> str:
    parts = [
        f"{count} {label}"
        for count, label in (
            (chat.contact, "of theirs"),
            (chat.owner, "of yours"),
            (chat.assistant, "sent by the assistant"),
            (chat.unknown, "never stored"),
        )
        if count
    ]
    return f"• {_name(chat.name)}: {chat.total} message(s) deleted ({', '.join(parts)})"


def _owner_deleted(deletion: OwnerDeletion, zone: ZoneInfo) -> str:
    scope = _SCOPES.get(deletion.scope, "stored data")
    count = "" if deletion.messages is None else f", {deletion.messages} message(s)"
    return f"• At your request, at {_when(deletion.at, zone)}: {scope}{count}"


def render_digest(content: DigestContent) -> str:
    """The digest message (S05.3b §3.4). Names, reasons and titles are the
    ones the product already shows, escaped; never message text."""
    zone = _zone(content.timezone)
    lines = [
        f"📋 <b>Digest</b> · {_when(content.period_start, zone)} to "
        f"{_when(content.period_end, zone)}"
    ]

    def section(title: str, body: list[str]) -> None:
        if body:
            lines.extend(("", f"<b>{title}</b>", *body))

    section(
        "Needs you",
        [
            f"• {_name(line.name)}" + (f" — {html.escape(line.reason)}" if line.reason else "")
            for line in content.needs_you
        ],
    )
    section(
        "Waiting for your reply",
        [
            f"• {_name(line.name)}"
            + (f" — since {_when(line.since, zone)}" if line.since else "")
            + (f" — {html.escape(line.reason)}" if line.reason else "")
            for line in content.waiting
        ],
    )
    commitments = [_commitment(task, zone, content.period_end) for task in content.commitments]
    if content.commitments_more:
        commitments.append(f"• …and {content.commitments_more} more")
    section("Commitments due", commitments)
    closed = [
        f"• {html.escape(task.title)}" + (f" ({_name(task.name)})" if task.name else "")
        for task in content.closed
    ]
    if content.closed_more:
        closed.append(f"• …and {content.closed_more} more")
    section("Closed automatically", closed)
    section(
        "Other alerts",
        [
            f"• {count} {_ALERT_LABELS.get(kind, html.escape(kind))}"
            for kind, count in content.other_alerts
        ],
    )
    deletions = [_deleted(chat) for chat in content.deletions]
    deletions += [_owner_deleted(item, zone) for item in content.owner_deletions]
    if content.deletions:
        deletions.append("Telegram does not say who deleted a message.")
    section("Deletions", deletions)
    section("Coverage", [f"• {html.escape(caveat)}" for caveat in content.caveats])
    if content.quiet:
        lines.extend(("", QUIET_DAY_TEXT))
    return "\n".join(lines)


# ------------------------------------------------------------ /digest
async def handle_digest_command(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    scheduler: JobScheduler | None,
    account: Account,
    message: IncomingMessage,
    argument: str,
) -> HandlerResult:
    """`/digest` (P2-T20): the digest now, through `digest.build`."""
    target = ControlChat(account_id=account.id, chat_id=message.chat_id)
    if account.status != "active":
        _after(session, gateway, target, text=SET_UP_FIRST_TEXT)
        return HandlerResult(False, "digest", None, note="not_onboarded")
    if scheduler is None:
        _after(session, gateway, target, text=NOT_AVAILABLE_TEXT)
        return HandlerResult(False, "digest", None, note="no_scheduler")
    period_end = int(clock.now().replace(second=0, microsecond=0).timestamp())
    account_id = account.id

    async def defer(_factory: async_sessionmaker[AsyncSession]) -> None:
        job = await scheduler.defer(
            DIGEST_TASK,
            args={"account_id": account_id, "period_end": period_end},
            queueing_lock=digest_queueing_lock(account_id, period_end),
        )
        logger.info("digest_requested", account_id=account_id, queued=job is not None)

    after_commit(session, defer)
    return HandlerResult(True, "digest", None, note="queued")


# ------------------------------------------------------------ "Open" buttons
async def handle_digest_press(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    scheduler: JobScheduler | None,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    """`dg:<token>:op`: the chat's panel, only for the presser's own card
    and chat. The card is not single-use: it can be opened again."""
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    card = (
        await session.execute(
            select(PendingInput)
            .where(PendingInput.account_id == account.id)
            .where(PendingInput.kind == DIGEST_CARD)
            .where(PendingInput.token == press.token)
        )
    ).scalar_one_or_none()
    panel = None
    if (
        press.op == OP_OPEN_CHAT
        and card is not None
        and card.ref_id is not None
        and card.expires_at > clock.now()
    ):
        panel = await visibility.conversation_panel(
            session, account_id=account.id, conversation_id=card.ref_id, now=clock.now()
        )
    if panel is None:  # unknown, foreign, expired, or the chat's data is gone
        _after(session, gateway, target, answer=(press.callback_id, CALLBACK_EXPIRED_TEXT))
        return HandlerResult(False, "dg", None, note="no_longer_active")
    screen = await panel_screen(session, clock, account, panel)
    _after(session, gateway, target, answer=(press.callback_id, OPENING_TEXT), screen=screen)
    return HandlerResult(True, "dg", None, note="panel")


def _after(
    session: AsyncSession,
    gateway: OutboundGateway,
    target: ControlChat,
    *,
    text: str | None = None,
    answer: tuple[str, str] | None = None,
    screen: Screen | None = None,
) -> None:
    """After the commit: answer the press, then send a text or a panel (whose
    own card remembers the message, or goes if it never arrived)."""

    async def run(factory: async_sessionmaker[AsyncSession]) -> None:
        if answer is not None:
            await gateway.answer_callback(*answer)
        if text is not None:
            await gateway.send_text(target, text)
        if screen is None:
            return
        result = await gateway.send_text(target, screen.text, keyboard=screen.markup())
        if screen.card_id is None:
            return
        async with transaction(factory) as uow:
            card = await uow.session.get(PendingInput, screen.card_id)
            if card is None:
                return
            if result.ok:
                card.prompt_message_id = result.message_id
            else:
                await uow.session.delete(card)

    after_commit(session, run)
