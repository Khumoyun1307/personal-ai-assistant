"""Alert card presses: `ntf:<token>:<op>` (S05.2 §3.2; F0-interfaces §7).

An alert about a chat carries three buttons. "Not useful" and "Useful"
record the owner's feedback on the notification (the last press wins; the
derived mute reads it, `inbox/notification_policy.muted_pairs`), so the card
is looked up, never claimed: it stays answerable. "Open chat" sends that
chat's panel after re-checking that the chat is the presser's.

The card is resolved only within the pressing owner's account; an unknown,
foreign or expired token is "no longer active" (§6 invariant 12). Database
work happens in the press's transaction; Telegram calls run after its
commit (ARCHITECTURE §6.7).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, PendingInput
from assistant.conversations import visibility
from assistant.core.clock import Clock
from assistant.core.jobs import JobScheduler
from assistant.db.uow import after_commit, transaction
from assistant.inbox.models import Notification
from assistant.inbox.notifications import NTF_CARD, OP_NOT_USEFUL, OP_OPEN_CHAT, OP_USEFUL
from assistant.telegram.control.handlers import CALLBACK_EXPIRED_TEXT, CardPress, HandlerResult
from assistant.telegram.control.panels import Screen, panel_screen
from assistant.telegram.gateway import ControlChat, OutboundGateway

NOT_USEFUL_TEXT = "Noted — fewer alerts like this"
USEFUL_TEXT = "Noted"
OPENING_TEXT = "Opening the chat"
_FEEDBACK = {OP_NOT_USEFUL: ("not_useful", NOT_USEFUL_TEXT), OP_USEFUL: ("useful", USEFUL_TEXT)}


async def handle_ntf_press(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    scheduler: JobScheduler | None,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    notification = await _notification(session, clock, account, press.token)
    if notification is None:
        _after(session, gateway, press, target, answer=CALLBACK_EXPIRED_TEXT)
        return HandlerResult(False, "ntf", None, note="no_longer_active")
    if press.op in _FEEDBACK:
        feedback, answer = _FEEDBACK[press.op]
        notification.feedback = feedback
        notification.updated_at = clock.now()  # when the mark was made (derived mute)
        _after(session, gateway, press, target, answer=answer)
        return HandlerResult(True, "ntf", None, note=feedback)
    if press.op == OP_OPEN_CHAT:
        panel = None
        if notification.conversation_id is not None:
            panel = await visibility.conversation_panel(
                session,
                account_id=account.id,
                conversation_id=notification.conversation_id,
                now=clock.now(),
            )
        if panel is None:  # the chat's data is gone, or never was this owner's
            _after(session, gateway, press, target, answer=CALLBACK_EXPIRED_TEXT)
            return HandlerResult(False, "ntf", None, note="no_longer_active")
        screen = await panel_screen(session, clock, account, panel)
        _after(session, gateway, press, target, answer=OPENING_TEXT, send=screen)
        return HandlerResult(True, "ntf", None, note="panel")
    _after(session, gateway, press, target, answer=CALLBACK_EXPIRED_TEXT)
    return HandlerResult(False, "ntf", None, note="unknown_op")


async def _notification(
    session: AsyncSession, clock: Clock, account: Account, token: str
) -> Notification | None:
    """The live card's notification, both scoped to the pressing account."""
    card = (
        await session.execute(
            select(PendingInput)
            .where(PendingInput.account_id == account.id)
            .where(PendingInput.kind == NTF_CARD)
            .where(PendingInput.token == token)
        )
    ).scalar_one_or_none()
    if card is None or card.ref_id is None or card.expires_at <= clock.now():
        return None
    return (
        await session.execute(
            select(Notification)
            .where(Notification.account_id == account.id)
            .where(Notification.id == card.ref_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


def _after(
    session: AsyncSession,
    gateway: OutboundGateway,
    press: CardPress,
    target: ControlChat,
    *,
    answer: str,
    send: Screen | None = None,
) -> None:
    """After the commit: answer the press; send the panel, if any, and let
    its card remember the message (or go, if it never arrived)."""

    async def run(factory: async_sessionmaker[AsyncSession]) -> None:
        await gateway.answer_callback(press.callback_id, answer)
        if send is None:
            return
        result = await gateway.send_text(target, send.text, keyboard=send.markup())
        if send.card_id is None:
            return
        async with transaction(factory) as uow:
            card = await uow.session.get(PendingInput, send.card_id)
            if card is None:
                return
            if result.ok:
                card.prompt_message_id = result.message_id
            else:
                await uow.session.delete(card)

    after_commit(session, run)
