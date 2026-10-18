"""Control-chat dispatch (ARCHITECTURE §6.7).

Routing precedence, which is the whole subtlety of this module:

1. **A `/command` always wins**, and cancels any pending input. Otherwise an
   owner who asked a question, changed their mind and typed `/status` would
   have `/status` swallowed as the answer.
2. **A stop word is treated as `/pause`** before any model is involved, so the
   kill switch works when the provider is down, out of budget, or wrong.
3. **A pending input consumes the next plain message** — that is what makes
   multi-step flows possible without aiogram FSM state (which would not
   survive a restart).
4. Anything else becomes a control turn for the agent (S01.4).

The handlers are plain functions; this module decides which one runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts import service
from assistant.accounts.models import Account
from assistant.core.clock import Clock
from assistant.observability.logging import get_logger
from assistant.telegram.control import handlers
from assistant.telegram.control.handlers import HandlerResult, IncomingMessage
from assistant.telegram.gateway import ControlChat, OutboundGateway

logger = get_logger(__name__)

#: Commands that work before onboarding completes.
PRE_ONBOARDING_COMMANDS = frozenset({"start", "help", "privacy"})


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    """What dispatch did, for tests and the activity log."""

    result: HandlerResult
    account_id: int | None = None


class ControlDispatcher:
    """Routes one control-chat message to a handler."""

    def __init__(self, gateway: OutboundGateway, clock: Clock) -> None:
        self._gateway = gateway
        self._clock = clock

    async def dispatch(self, session: AsyncSession, message: IncomingMessage) -> DispatchOutcome:
        command, argument = handlers.parse_command(message.text)

        # /start is special: it is the only entry point that may create an
        # account, so it runs before any account lookup.
        if command == "start":
            result = await handlers.handle_start(session, self._gateway, self._clock, message)
            return DispatchOutcome(result)

        account = await self._load_account(session, message)
        if account is None:
            # Allowlisted but never started: we have nowhere to reply and no
            # consent. Telling them to press Start is the only honest move.
            result = await self._prompt_start(message)
            return DispatchOutcome(result)

        if command:
            # Precedence rule 1: a command cancels a pending question.
            cleared = await service.clear_pending_input(session, account_id=account.id)
            if cleared:
                logger.info(
                    "pending_input_cancelled_by_command",
                    account_id=account.id,
                    command=command,
                )
            return DispatchOutcome(
                await self._run_command(session, account, message, command, argument),
                account.id,
            )

        # Precedence rule 2: the kill switch never depends on the model.
        if handlers.is_stop_word(message.text):
            account.autonomy_paused = True
            await session.flush()
            await service.clear_pending_input(session, account_id=account.id)
            send = await self._gateway.send_text(
                ControlChat(account_id=account.id, chat_id=message.chat_id),
                "Paused. I will not act on your behalf until you say <b>resume</b>.",
            )
            return DispatchOutcome(HandlerResult(True, "pause", send, note="stop_word"), account.id)

        # Precedence rule 3: an open question consumes this message.
        pending = await service.get_pending_input(session, account_id=account.id, clock=self._clock)
        if pending is not None and pending.kind == "onboarding_consent":
            result = await handlers.handle_consent_reply(
                session, self._gateway, self._clock, message, account
            )
            return DispatchOutcome(result, account.id)

        # Rule 4: free text. The agent runtime picks this up in S01.4.
        await service.record_control_turn(
            session,
            account_id=account.id,
            role="owner",
            text=message.text,
            telegram_message_id=message.telegram_message_id,
        )
        return DispatchOutcome(
            HandlerResult(True, "free_text", None, note="recorded_for_agent"), account.id
        )

    async def _load_account(
        self, session: AsyncSession, message: IncomingMessage
    ) -> Account | None:
        from sqlalchemy import select

        result = await session.execute(
            select(Account).where(Account.telegram_user_id == message.from_id)
        )
        return result.scalar_one_or_none()

    async def _prompt_start(self, message: IncomingMessage) -> HandlerResult:
        send = await self._gateway.send_text(
            ControlChat(account_id=0, chat_id=message.chat_id),
            "Send /start to set me up.",
        )
        return HandlerResult(True, "start_required", send, note="no_account")

    async def _run_command(
        self,
        session: AsyncSession,
        account: Account,
        message: IncomingMessage,
        command: str,
        argument: str,
    ) -> HandlerResult:
        match command:
            case "help":
                return await handlers.handle_help(self._gateway, account, message)
            case "privacy":
                return await handlers.handle_privacy(session, self._gateway, account, message)
            case "status":
                return await handlers.handle_status(self._gateway, account, message)
            case "settings":
                if argument:
                    return await handlers.apply_setting(
                        session, self._gateway, account, message, argument
                    )
                return await handlers.handle_settings(self._gateway, account, message)
            case "pause":
                account.autonomy_paused = True
                await session.flush()
                send = await self._gateway.send_text(
                    ControlChat(account_id=account.id, chat_id=message.chat_id),
                    "Paused. Send /resume to continue.",
                )
                return HandlerResult(True, "pause", send)
            case "resume":
                account.autonomy_paused = False
                await session.flush()
                send = await self._gateway.send_text(
                    ControlChat(account_id=account.id, chat_id=message.chat_id),
                    "Resumed.",
                )
                return HandlerResult(True, "resume", send)
            case _:
                send = await self._gateway.send_text(
                    ControlChat(account_id=account.id, chat_id=message.chat_id),
                    handlers.describe_unknown_command(command),
                )
                return HandlerResult(False, command, send, note="unknown_command")
