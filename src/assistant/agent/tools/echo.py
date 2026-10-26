"""Echo with a one-tap Undo (ARCHITECTURE §8.1; FR-MEM-01; S03.3).

A `configure` call that executes (an untainted run, or after the owner
confirmed a held call) is shown to the owner at once: what changed, in our
own words around the owner's text, with an Undo button. The card row
(`pending_inputs`, kind `undo`, `ref_id` = the change's audit row) is written
in the change's transaction; the message is sent only after the commit (a
rolled-back change announces nothing), and a card whose send failed is
removed, so no tap can reach it. What the model is told says plainly whether
the Undo button reached the owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import callbacks, service
from assistant.accounts.models import Account, PendingInput
from assistant.agent.tools.registry import ToolContext
from assistant.core.channel import Button
from assistant.db.uow import after_commit, transaction
from assistant.memory.undo import OP_UNDO, UNDO_CARD
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

SHOWN = "shown to the owner with an Undo button"
NOT_SHOWN = "the change is saved, but its Undo button could not be shown to the owner"
NO_CHANNEL = "the change is saved (no echo channel in this context)"
#: A change with no Undo (a retention period, M03 D85).
SHOWN_NO_UNDO = "shown to the owner; it has no Undo (the owner changes it in /settings)"
NOT_SHOWN_NO_UNDO = "the change is saved, but its notice could not be shown to the owner"


@dataclass
class Echo:
    """Filled after the commit: whether the echo reached the owner."""

    delivered: dict[str, bool] = field(default_factory=dict)
    undo: bool = True

    @property
    def note(self) -> str:
        if "ok" not in self.delivered:
            return NO_CHANNEL
        if not self.undo:
            return SHOWN_NO_UNDO if self.delivered["ok"] else NOT_SHOWN_NO_UNDO
        return SHOWN if self.delivered["ok"] else NOT_SHOWN


async def queue_echo(
    ctx: ToolContext, session: AsyncSession, change_id: int, text: str, *, undo: bool = True
) -> Echo:
    """Create the Undo card now; send `text` (HTML, already escaped) with it
    after the commit. `undo=False`: the notice alone, no card (a change
    Undo must not reverse, M03 D85)."""
    echo = Echo(undo=undo)
    channel = ctx.services.channel
    account = await session.get(Account, ctx.account_id)
    if channel is None or account is None or account.control_chat_id is None:
        return echo
    if not undo:
        notice_chat, notice_account = account.control_chat_id, ctx.account_id

        async def send_notice(factory: async_sessionmaker[AsyncSession]) -> None:
            ok = False
            try:
                result = await channel.send(
                    account_id=notice_account, chat_id=notice_chat, text=text
                )
                ok = result.ok
            except Exception as error:
                logger.error("echo_send_failed", exc_type=type(error).__name__)
            echo.delivered["ok"] = ok

        after_commit(session, send_notice)
        return echo
    card = await service.create_card(
        session,
        account_id=ctx.account_id,
        kind=UNDO_CARD,
        clock=ctx.services.clock,
        ref_id=change_id,
    )
    assert card.token is not None
    button = Button("Undo", callbacks.encode(UNDO_CARD, card.token, OP_UNDO))
    chat_id, card_id, account_id = account.control_chat_id, card.id, ctx.account_id

    async def send(factory: async_sessionmaker[AsyncSession]) -> None:
        ok, message_id = False, None
        try:
            result = await channel.send(
                account_id=account_id, chat_id=chat_id, text=text, buttons=(button,)
            )
            ok, message_id = result.ok, result.message_id
        except Exception as error:
            logger.error("echo_send_failed", exc_type=type(error).__name__)
        echo.delivered["ok"] = ok
        async with transaction(factory) as follow_up:
            row = await follow_up.session.get(PendingInput, card_id)
            if row is None:
                return
            if ok:
                row.prompt_message_id = message_id
            else:
                # Possibly never shown: the change stands, the button is gone.
                await follow_up.session.delete(row)

    after_commit(session, send)
    return echo
