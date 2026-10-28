"""Monitor card presses: `mon:<token>:stop` (S05.4 §3.2; F0-interfaces §7).

The "Monitor on" notice carries a Stop button: a `pending_inputs` card of
kind `mon` whose `ref_id` is the monitor. A press claims the card once,
within the pressing owner's account only — an unknown, foreign, expired or
already pressed card is "no longer active" (§6 invariant 12) — and stops the
monitor (`cancelled`), or says it had already ended. Database work happens
in the press's transaction; the answer and the message edit run after its
commit (ARCHITECTURE §6.7).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.accounts.service import claim_card
from assistant.automation import monitors
from assistant.automation.monitors import MON_CARD, OP_STOP, StopResult
from assistant.core.clock import Clock
from assistant.core.jobs import JobScheduler
from assistant.db.uow import after_commit
from assistant.telegram.control.handlers import CALLBACK_EXPIRED_TEXT, CardPress, HandlerResult
from assistant.telegram.gateway import ControlChat, OutboundGateway

STOPPED_ANSWER = "Monitor stopped"
STOPPED_TEXT = "⏹ Monitor stopped. It will not alert you again."
ENDED_ANSWER = "Already ended"
ENDED_TEXT = "This monitor had already ended; nothing changed."


async def handle_mon_press(
    session: AsyncSession,
    gateway: OutboundGateway,
    clock: Clock,
    scheduler: JobScheduler | None,
    account: Account,
    press: CardPress,
) -> HandlerResult:
    target = ControlChat(account_id=account.id, chat_id=press.chat_id)
    card = await claim_card(session, account_id=account.id, token=press.token, clock=clock)
    if card is None or card.kind != MON_CARD or card.ref_id is None or press.op != OP_STOP:
        _after(session, gateway, press, target, None, answer=CALLBACK_EXPIRED_TEXT)
        return HandlerResult(False, "mon", None, note="no_longer_active")
    message_id = press.message_id if press.message_id is not None else card.prompt_message_id
    result = await monitors.stop(
        session, account_id=account.id, automation_id=card.ref_id, clock=clock, actor="owner"
    )
    if result is StopResult.STOPPED:
        _after(
            session, gateway, press, target, message_id, answer=STOPPED_ANSWER, text=STOPPED_TEXT
        )
        return HandlerResult(True, "mon", None, note="stopped")
    if result is StopResult.ALREADY_ENDED:
        _after(session, gateway, press, target, message_id, answer=ENDED_ANSWER, text=ENDED_TEXT)
        return HandlerResult(False, "mon", None, note="already_ended")
    _after(session, gateway, press, target, None, answer=CALLBACK_EXPIRED_TEXT)
    return HandlerResult(False, "mon", None, note="no_longer_active")


def _after(
    session: AsyncSession,
    gateway: OutboundGateway,
    press: CardPress,
    target: ControlChat,
    message_id: int | None,
    *,
    answer: str,
    text: str | None = None,
) -> None:
    """After the commit: answer the press, then replace the notice's text
    (and its button) with the outcome."""

    async def run(_factory: async_sessionmaker[AsyncSession]) -> None:
        await gateway.answer_callback(press.callback_id, answer)
        if text is None:
            return
        if message_id is None:
            await gateway.send_text(target, text)
        else:
            await gateway.edit_control_text(target, message_id, text)

    after_commit(session, run)
