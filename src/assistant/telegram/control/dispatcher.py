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
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts import callbacks, connections, service
from assistant.accounts.models import Account
from assistant.agent.confirmations import TOOL_CONFIRM_CARD
from assistant.conversations import read, visibility
from assistant.core.clock import Clock
from assistant.core.jobs import (
    CONNECTION_NOTIFY_TASK,
    OWNER_REQUEST_TASK,
    JobScheduler,
    owner_control_lock,
)
from assistant.inbox.connection_alerts import VISIBILITY
from assistant.memory.undo import UNDO_CARD
from assistant.observability.logging import get_logger
from assistant.observability.status import build_status
from assistant.telegram.control import deeplinks, handlers, panels
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

    def __init__(
        self,
        gateway: OutboundGateway,
        clock: Clock,
        scheduler: JobScheduler | None = None,
        *,
        policy_url: str | None = None,
        monthly_budget_usd: Decimal | None = None,
    ) -> None:
        self._gateway = gateway
        self._clock = clock
        # None only in tests that exercise routing without a job queue.
        self._scheduler = scheduler
        self._policy_url = policy_url
        self._budget = monthly_budget_usd

    async def dispatch(self, session: AsyncSession, message: IncomingMessage) -> DispatchOutcome:
        # A forwarded message is someone else's text: it is never a command,
        # a stop word or an answer to a pending question — it goes to the
        # agent as a tainted turn (§6 invariant 3b; M03 D49).
        command, argument = ("", "") if message.forwarded else handlers.parse_command(message.text)

        # /start is special: it is the only entry point that may create an
        # account, so it runs before any account lookup.
        if command == "start":
            if deeplinks.is_deep_link(argument):
                # "Manage Bot" (TF §12 LV-10): a panel for an onboarded owner;
                # for anyone else it is just /start.
                account = await self._load_account(session, message)
                if account is not None and account.status == "active":
                    return DispatchOutcome(
                        await self._conversation_panel(session, account, message, argument),
                        account.id,
                    )
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
        if not message.forwarded and handlers.is_stop_word(message.text):
            account.autonomy_paused = True
            await session.flush()
            await service.clear_pending_input(session, account_id=account.id)
            send = await self._gateway.send_text(
                ControlChat(account_id=account.id, chat_id=message.chat_id),
                "Paused. I will not act on your behalf until you send /resume.",
            )
            return DispatchOutcome(HandlerResult(True, "pause", send, note="stop_word"), account.id)

        # Precedence rule 3: an open question consumes this message.
        pending = (
            None
            if message.third_party
            else await service.get_pending_input(session, account_id=account.id, clock=self._clock)
        )
        if pending is not None and pending.kind == "onboarding_consent":
            result = await handlers.handle_consent_reply(
                session, self._gateway, self._clock, message, account
            )
            if result.note == "granted":
                await self._activate_pending_connection(session, account)
            return DispatchOutcome(result, account.id)
        # The managed-conversation consent is a card (Agree / Decline), never
        # a text question: a plain message is never read as that answer.

        # Rule 4: free text. Record the turn and queue the agent run in the
        # SAME transaction: a crash between the two can lose neither, and a
        # rollback leaves neither (M00 spike E1). One account's turns run one
        # at a time, in order (lock `acct:{id}:control`).
        origin = (
            await read.managed_origin(
                session,
                account.id,
                user_ids=tuple(message.origin_user_ids),
                chat_ids=tuple(message.origin_chat_ids),
                names=tuple(message.origin_names),
            )
            if message.third_party
            else read.ManagedOrigin()
        )
        turn = await service.record_control_turn(
            session,
            account_id=account.id,
            role="owner",
            text=message.text,
            telegram_message_id=message.telegram_message_id,
            created_at=message.received_at,
            third_party=message.third_party,
            quoted_text=message.quoted_text,
            # Forwarded/quoted text from one of the owner's managed chats is
            # managed content: it needs `ai_managed` to reach a model, and is
            # cleared by managed-data deletion and by purging that chat, which
            # finds it through `refs` (§6 invariant 9, M03 D53).
            contains_managed=origin.managed,
            refs=origin.as_refs() if origin.managed else None,
        )
        if self._scheduler is None:
            logger.warning("owner_turn_not_queued", account_id=account.id, turn_id=turn.id)
            note = "recorded_for_agent"
        else:
            connection = await session.connection()
            raw = await connection.get_raw_connection()
            await self._scheduler.defer(
                OWNER_REQUEST_TASK,
                args={"turn_id": turn.id},
                lock=owner_control_lock(account.id),
                connection=raw.driver_connection,
            )
            note = "queued_for_agent"
        return DispatchOutcome(HandlerResult(True, "free_text", None, note=note), account.id)

    async def _activate_pending_connection(self, session: AsyncSession, account: Account) -> None:
        """A connection that arrived before onboarding goes live now (J1 edge case).

        The notice — with the managed-consent question — is a job deferred in
        this transaction, exactly as for a connection that arrives later.
        """
        result = await connections.activate_pending(session, account, self._clock)
        if not result.notify or result.event_id is None:
            return
        if self._scheduler is None:
            logger.warning("connection_notice_not_queued", account_id=account.id)
            return
        connection = await session.connection()
        raw = await connection.get_raw_connection()
        await self._scheduler.defer(
            CONNECTION_NOTIFY_TASK,
            args={"event_id": result.event_id},
            connection=raw.driver_connection,
        )

    async def dispatch_callback(
        self,
        session: AsyncSession,
        *,
        callback_id: str,
        from_id: int,
        data: str | None,
        chat_id: int,
        message_id: int | None,
    ) -> HandlerResult:
        """A button press. Everything in it came from the client: parse, then
        resolve the token only within the presser's own account (§6.7)."""
        try:
            parsed = callbacks.parse(data)
        except callbacks.CallbackDataError:
            await self._gateway.answer_callback(callback_id, handlers.CALLBACK_EXPIRED_TEXT)
            return HandlerResult(False, "card", None, note="malformed")
        account = await self._load_account_by_id(session, from_id)
        if account is None:
            await self._gateway.answer_callback(callback_id, handlers.CALLBACK_EXPIRED_TEXT)
            return HandlerResult(False, "card", None, note="no_account")
        press = handlers.CardPress(
            kind=parsed.kind,
            token=parsed.token,
            op=parsed.op,
            callback_id=callback_id,
            chat_id=chat_id,
            message_id=message_id,
        )
        if parsed.kind == TOOL_CONFIRM_CARD:
            return await handlers.handle_tool_confirmation_press(
                session, self._gateway, self._clock, self._scheduler, account, press
            )
        if parsed.kind == UNDO_CARD:
            return await handlers.handle_undo_press(
                session, self._gateway, self._clock, account, press
            )
        if parsed.kind in panels.KINDS:
            return await panels.handle_press(
                session, self._gateway, self._clock, account, press, self._scheduler
            )
        return await handlers.handle_card_press(
            session, self._gateway, self._clock, account, press, self._scheduler
        )

    async def _load_account_by_id(self, session: AsyncSession, from_id: int) -> Account | None:
        from sqlalchemy import select

        result = await session.execute(select(Account).where(Account.telegram_user_id == from_id))
        return result.scalar_one_or_none()

    async def _conversation_panel(
        self, session: AsyncSession, account: Account, message: IncomingMessage, argument: str
    ) -> HandlerResult:
        chat_id = deeplinks.parse_biz_chat(argument)
        panel = None
        if chat_id is not None:
            # A malformed parameter never reaches the database.
            panel = await visibility.conversation_panel(
                session, account_id=account.id, business_chat_id=chat_id, now=self._clock.now()
            )
        if panel is None:
            logger.info("panel_refused", account_id=account.id, malformed=chat_id is None)
        return await panels.handle_panel_command(
            session, self._gateway, self._clock, account, message, panel
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
                if argument.strip().lower() == "revoke":
                    return await handlers.handle_revoke(
                        session, self._gateway, self._clock, account, message
                    )
                if argument.strip().lower() == "delete":
                    # S03.5: the "delete everything" card; it asks first.
                    return await panels.handle_delete_all_command(
                        session, self._gateway, self._clock, account, message
                    )
                if argument.strip().lower() == "managed":
                    return await handlers.handle_managed_consent_request(
                        session, self._gateway, self._clock, account, message
                    )
                return await handlers.handle_privacy(
                    session, self._gateway, account, message, policy_url=self._policy_url
                )
            case "status":
                report = await build_status(
                    session,
                    account,
                    self._clock.now(),
                    monthly_budget_usd=self._budget,
                    in_flight_rows=1,  # this command's own inbox row
                )
                return await handlers.handle_status(
                    self._gateway, account, message, report, visibility=VISIBILITY
                )
            case "settings":
                if argument.strip().partition(" ")[2].strip():
                    return await handlers.apply_setting(
                        session, self._gateway, account, message, argument, self._clock
                    )
                return await panels.handle_settings_command(
                    session, self._gateway, self._clock, account, message
                )
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
