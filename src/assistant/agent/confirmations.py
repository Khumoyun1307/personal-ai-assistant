"""The confirmation gate for model-initiated state changes (ARCHITECTURE §7.1;
§6 invariant 3b; M03 D45).

In a tainted run a `configure`/`control` call (other than the calls that only
reduce autonomy) is **held**, not executed:

1. `hold` — in one transaction: a `tool_confirmations` row with the validated
   arguments (unique per `(run_id, tool_call_id)`, so a retried call reuses
   it) and a confirmation card (`pending_inputs`, kind `tool_confirm`). The
   card is sent after the commit (`db.uow.after_commit`), never inside it.
   The model is told `awaiting_confirmation`, which is not "done".
2. A tap on the card (`telegram/control/handlers.py`) claims the card once
   (a double tap is a no-op) and calls `decide`: Confirm → `confirmed` and the
   job `agent.apply_confirmation(confirmation_id)` is deferred in the same
   transaction (lock `acct:{id}:control`, never scheduled); Reject →
   `rejected`, arguments cleared.
3. `ConfirmationApplier.apply` — the job: claims `confirmed` → `applied`
   with a conditional UPDATE (so a re-delivered job applies nothing twice),
   clears the arguments, runs the tool's handler **untainted** (the owner
   has looked and tapped), and reports the factual outcome to the owner;
   a handler error ends as `failed`.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import callbacks, service
from assistant.accounts.models import Account, PendingInput
from assistant.agent.models import ToolConfirmation
from assistant.agent.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRegistry,
    ToolServices,
)
from assistant.core.channel import Button, ControlChannel
from assistant.core.clock import Clock
from assistant.db.uow import after_commit, transaction
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

TOOL_CONFIRM_CARD = "tool_confirm"
OP_CONFIRM = "confirm"
OP_REJECT = "reject"
#: How much of the arguments the card shows the owner.
_CARD_ARGS_LIMIT = 600


class ConfirmationStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    APPLIED = "applied"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"


def card_text(tool: ToolDefinition, args: Mapping[str, Any]) -> str:
    """What the owner is asked. Owner-facing only (the control chat), HTML-escaped."""
    shown = json.dumps(dict(args), ensure_ascii=False, sort_keys=True)
    if len(shown) > _CARD_ARGS_LIMIT:
        shown = shown[:_CARD_ARGS_LIMIT] + "…"
    return (
        "<b>Confirm a change?</b>\n\n"
        "This request included text from other people, so I need your tap before "
        f"changing anything.\n\nAction: <code>{html.escape(tool.name)}</code>\n"
        f"Details: <code>{html.escape(shown)}</code>\n\n"
        "Nothing has been changed yet."
    )


def card_buttons(token: str) -> tuple[Button, ...]:
    return (
        Button("Confirm", callbacks.encode(TOOL_CONFIRM_CARD, token, OP_CONFIRM)),
        Button("Reject", callbacks.encode(TOOL_CONFIRM_CARD, token, OP_REJECT)),
    )


async def hold(
    ctx: ToolContext, tool: ToolDefinition, call_id: str, args: Mapping[str, Any]
) -> ToolOutcome:
    """Hold one call for the owner's tap. Idempotent on `(run_id, call_id)`."""
    services = ctx.services
    channel = services.channel
    if channel is None:
        # No way to ask: fail closed (§6 invariant 7).
        return ToolOutcome("not_executed", {"reason": "confirmation is not available"})
    delivered: dict[str, bool] = {}
    async with transaction(services.session_factory) as uow:
        session = uow.session
        account = await session.get(Account, ctx.account_id)
        if account is None or account.control_chat_id is None:
            return ToolOutcome("not_executed", {"reason": "the owner cannot be reached"})
        inserted = await session.execute(
            insert(ToolConfirmation)
            .values(
                account_id=ctx.account_id,
                run_id=ctx.run_id,
                tool_call_id=call_id,
                tool_name=tool.name,
                args=dict(args),
                status=ConfirmationStatus.PENDING.value,
                created_at=services.clock.now(),
            )
            .on_conflict_do_nothing(constraint="uq_tool_confirmations_run_id_call_id")
            .returning(ToolConfirmation.id)
        )
        confirmation_id = inserted.scalar_one_or_none()
        if confirmation_id is None:
            # The same call again (a retried job): one card is enough.
            return ToolOutcome(
                "awaiting_confirmation", {"card": "already sent to the owner; not done yet"}
            )
        card = await service.create_card(
            session,
            account_id=ctx.account_id,
            kind=TOOL_CONFIRM_CARD,
            clock=services.clock,
            ref_id=confirmation_id,
        )
        assert card.token is not None
        text = card_text(tool, args)
        buttons = card_buttons(card.token)
        chat_id = account.control_chat_id
        card_id = card.id

        async def send_card(factory: async_sessionmaker[AsyncSession]) -> None:
            result = await channel.send(
                account_id=ctx.account_id, chat_id=chat_id, text=text, buttons=buttons
            )
            delivered["ok"] = result.ok
            async with transaction(factory) as follow_up:
                row = await follow_up.session.get(PendingInput, card_id)
                if row is None:
                    return
                if result.ok:
                    row.prompt_message_id = result.message_id
                else:
                    # Never shown: nothing can press it, so nothing will apply.
                    await follow_up.session.delete(row)
                    await _finish(
                        follow_up.session,
                        ctx.account_id,
                        confirmation_id,
                        ConfirmationStatus.EXPIRED,
                        services.clock,
                        only_from=ConfirmationStatus.PENDING,
                    )

        after_commit(session, send_card)
    logger.info(
        "tool_call_held",
        account_id=ctx.account_id,
        run_id=ctx.run_id,
        tool=tool.name,
        confirmation_id=confirmation_id,
        card_delivered=delivered.get("ok", False),
    )
    if not delivered.get("ok", False):
        return ToolOutcome(
            "not_executed", {"reason": "the confirmation card could not be delivered"}
        )
    return ToolOutcome(
        "awaiting_confirmation",
        {"card": "sent to the owner; nothing is changed until they tap Confirm"},
    )


@dataclass(frozen=True, slots=True)
class _Moved:
    #: The arguments the row held before the move (None if already cleared).
    args: dict[str, Any] | None


async def _finish(
    session: AsyncSession,
    account_id: int,
    confirmation_id: int,
    status: ConfirmationStatus,
    clock: Clock,
    *,
    only_from: ConfirmationStatus,
    clear_args: bool = True,
) -> _Moved | None:
    """Move one row `only_from` → `status`. Returns the previous args when the
    move happened, None when it did not (already decided, or not this account's)."""
    values: dict[str, Any] = {"status": status.value, "decided_at": clock.now()}
    if clear_args:
        values["args"] = None
    previous = (
        await session.execute(
            select(ToolConfirmation.args)
            .where(ToolConfirmation.id == confirmation_id)
            .where(ToolConfirmation.account_id == account_id)
            .where(ToolConfirmation.status == only_from.value)
            .with_for_update()
        )
    ).first()
    if previous is None:
        return None
    await session.execute(
        update(ToolConfirmation)
        .where(ToolConfirmation.id == confirmation_id)
        .where(ToolConfirmation.account_id == account_id)
        .where(ToolConfirmation.status == only_from.value)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return _Moved(None if previous[0] is None else dict(previous[0]))


async def decide(
    session: AsyncSession, *, account_id: int, confirmation_id: int, approve: bool, clock: Clock
) -> bool:
    """The owner's tap, in the caller's transaction. True if it changed the
    row (`pending` → `confirmed`/`rejected`); False if already decided."""
    if approve:
        moved = await _finish(
            session,
            account_id,
            confirmation_id,
            ConfirmationStatus.CONFIRMED,
            clock,
            only_from=ConfirmationStatus.PENDING,
            clear_args=False,  # the job still needs them
        )
    else:
        moved = await _finish(
            session,
            account_id,
            confirmation_id,
            ConfirmationStatus.REJECTED,
            clock,
            only_from=ConfirmationStatus.PENDING,
        )
    return moved is not None


async def account_of_confirmation(session: AsyncSession, confirmation_id: int) -> int | None:
    """The account a confirmation belongs to: a job carries only the id."""
    return (
        await session.execute(
            select(ToolConfirmation.account_id).where(ToolConfirmation.id == confirmation_id)
        )
    ).scalar_one_or_none()


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    status: str
    """`applied`, `failed`, or `skipped` (not confirmed, already applied, unknown)."""
    tool: str | None = None
    tool_status: str | None = None


TEXT_APPLIED = "✅ Done: <code>{tool}</code> ({status})."
TEXT_FAILED = "⚠️ I could not apply <code>{tool}</code>: {reason}. Nothing was changed."


class ConfirmationApplier:
    """Runs a confirmed call once (job `agent.apply_confirmation`)."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        channel: ControlChannel,
        registry: ToolRegistry,
        services: ToolServices,
        enabled_flags: frozenset[str],
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._channel = channel
        self._registry = registry
        self._services = services
        self._enabled_flags = enabled_flags

    async def apply(self, confirmation_id: int) -> ApplyOutcome:
        async with transaction(self._session_factory) as uow:
            session = uow.session
            account_id = await account_of_confirmation(session, confirmation_id)
            if account_id is None:
                return ApplyOutcome("skipped")
            row = await session.get(ToolConfirmation, confirmation_id)
            account = await session.get(Account, account_id)
            if row is None or account is None:
                return ApplyOutcome("skipped")
            tool_name, run_id, call_id = row.tool_name, row.run_id, row.tool_call_id
            # The claim: confirmed → applied, exactly once.
            moved = await _finish(
                session,
                account_id,
                confirmation_id,
                ConfirmationStatus.APPLIED,
                self._clock,
                only_from=ConfirmationStatus.CONFIRMED,
            )
            chat_id = account.control_chat_id
        if moved is None:
            return ApplyOutcome("skipped", tool_name)
        args = moved.args

        tool = next((t for t in self._registry.all() if t.name == tool_name), None)
        outcome: ToolOutcome
        if tool is None or tool.release_flag not in self._enabled_flags:
            outcome = ToolOutcome("error", {"reason": "this action is no longer available"})
        elif args is None:
            outcome = ToolOutcome("error", {"reason": "its details were deleted"})
        else:
            try:
                parsed = tool.args_model.model_validate(args)
            except ValidationError:
                outcome = ToolOutcome("error", {"reason": "its details are no longer valid"})
            else:
                context = ToolContext(
                    account_id=account_id,
                    run_id=run_id or 0,
                    run_type="owner_request",
                    # The owner looked and tapped: this is the owner's act now.
                    tainted=False,
                    services=replace(self._services, channel=self._channel),
                )
                try:
                    outcome = await tool.handler(context, parsed)
                except LookupError:
                    outcome = ToolOutcome("error", {"reason": "not found"})
        if outcome.is_error:
            async with transaction(self._session_factory) as uow:
                await uow.session.execute(
                    update(ToolConfirmation)
                    .where(ToolConfirmation.id == confirmation_id)
                    .values(status=ConfirmationStatus.FAILED.value)
                    .execution_options(synchronize_session=False)
                )
            text = TEXT_FAILED.format(
                tool=html.escape(tool_name),
                reason=html.escape(str(outcome.data.get("reason", "an error"))),
            )
        else:
            text = TEXT_APPLIED.format(
                tool=html.escape(tool_name), status=html.escape(outcome.status)
            )
        if chat_id is not None:
            await self._channel.send(account_id=account_id, chat_id=chat_id, text=text)
        logger.info(
            "tool_confirmation_applied",
            account_id=account_id,
            confirmation_id=confirmation_id,
            tool=tool_name,
            call_id_present=bool(call_id),
            status="failed" if outcome.is_error else "applied",
        )
        return ApplyOutcome("failed" if outcome.is_error else "applied", tool_name, outcome.status)
