"""Request privacy helpers, used before source clearing/deletion in the same transaction."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import ControlTurn, PendingInput
from assistant.actions.models import Action
from assistant.actions.request_models import (
    TERMINAL_REQUEST_STATES,
    OwnerRequest,
    OwnerRequestSource,
)
from assistant.actions.requests import cancel_linked_action, expire_requests, lock_requests
from assistant.actions.states import StateReason


async def purge_request_sources(
    session: AsyncSession,
    *,
    account_id: int,
    now: datetime,
    turn_ids: Sequence[int] = (),
    conversation_ids: Sequence[int] = (),
    message_ids: Sequence[int] = (),
    everything: bool = False,
    remove: bool = False,
    locked: bool = False,
) -> int:
    """Invalidate first; never reset a committed action claim.

    `locked` callers already hold their account/conversation locks in the
    deletion/ingestion order. They must not upgrade an account SHARE lock.
    Standalone callers take the full C1 lock order here.
    """
    if not locked and not await lock_requests(session, account_id=account_id):
        return 0
    source_requests = select(OwnerRequestSource.request_id).where(
        OwnerRequestSource.account_id == account_id,
        OwnerRequestSource.turn_id.in_(turn_ids),
    )
    statement = select(OwnerRequest).where(OwnerRequest.account_id == account_id)
    if not everything:
        linked_actions = select(Action.id).where(
            Action.account_id == account_id, Action.conversation_id.in_(conversation_ids)
        )
        statement = statement.where(
            or_(
                OwnerRequest.id.in_(source_requests),
                OwnerRequest.trigger_turn_id.in_(turn_ids),
                OwnerRequest.last_turn_id.in_(turn_ids),
                OwnerRequest.conversation_id.in_(conversation_ids),
                OwnerRequest.action_id.in_(linked_actions),
                OwnerRequest.target_message_id.in_(message_ids),
            )
        )
    rows = list(
        await session.scalars(
            statement.order_by(OwnerRequest.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    for row in rows:
        await cancel_linked_action(session, row, now=now, clear_text=True)
        await session.execute(
            delete(PendingInput).where(
                PendingInput.account_id == account_id,
                PendingInput.kind == "owner_request",
                PendingInput.ref_id == row.id,
            )
        )
        await session.execute(
            delete(OwnerRequestSource).where(
                OwnerRequestSource.account_id == account_id,
                OwnerRequestSource.request_id == row.id,
            )
        )
        row.question_input_id = None
        row.action_id = None
        row.action_version = None
        row.target_message_id = None
        row.focused = False
        row.missing_slot = None
        row.constraints = {}
        if row.state not in TERMINAL_REQUEST_STATES:
            row.state = "cancelled"
            row.ended_at = now
        row.reason = "source_unavailable"
        row.updated_at = now
        row.version += 1
        if remove:
            await session.delete(row)
    await session.flush()
    return len(rows)


async def delete_source_turns(
    session: AsyncSession, *, account_id: int, turn_ids: Sequence[int], now: datetime
) -> None:
    """Physical source-row deletion has the same invalidation as text purge."""
    await purge_request_sources(session, account_id=account_id, turn_ids=turn_ids, now=now)
    await session.execute(
        delete(ControlTurn).where(
            ControlTurn.account_id == account_id, ControlTurn.id.in_(turn_ids)
        )
    )


async def unlink_request_actions(
    session: AsyncSession, *, account_id: int, action_ids: Sequence[int]
) -> None:
    """Clear the paired proof before an action's SET NULL FK can fire."""
    await session.execute(
        update(OwnerRequest)
        .where(OwnerRequest.account_id == account_id, OwnerRequest.action_id.in_(action_ids))
        .values(action_id=None, action_version=None, version=OwnerRequest.version + 1)
    )


async def sweep_requests(session: AsyncSession, *, account_id: int, now: datetime) -> int:
    """Hourly TTL/terminal reconciliation and one-year metadata ceiling."""
    await expire_requests(session, account_id=account_id, now=now)
    # Expiring metadata may not strand pending derived work or pin content.
    old = list(
        await session.scalars(
            select(OwnerRequest)
            .where(
                OwnerRequest.account_id == account_id,
                OwnerRequest.requested_at <= now - timedelta(days=365),
            )
            .order_by(OwnerRequest.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    for row in old:
        await cancel_linked_action(session, row, now=now, reason=StateReason.MAX_AGE)
        await session.execute(
            delete(PendingInput).where(
                PendingInput.account_id == account_id,
                PendingInput.kind == "owner_request",
                PendingInput.ref_id == row.id,
            )
        )
        await session.delete(row)
    await session.flush()
    return len(old)


async def cancel_restored_requests(
    session: AsyncSession, *, account_id: int, now: datetime
) -> None:
    """Restored request metadata is never automatic resumption authority."""
    await lock_requests(session, account_id=account_id)
    rows = list(
        await session.scalars(
            select(OwnerRequest)
            .where(
                OwnerRequest.account_id == account_id,
                OwnerRequest.state.not_in(TERMINAL_REQUEST_STATES),
            )
            .order_by(OwnerRequest.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    for row in rows:
        # post_restore owns action settlement and its recovery receipt.
        row.state = "cancelled"
        row.focused = False
        row.missing_slot = None
        row.question_input_id = None
        row.reason = "restored_from_backup"
        row.ended_at = now
        row.updated_at = now
        row.version += 1
        await session.execute(
            delete(PendingInput).where(
                PendingInput.account_id == account_id,
                PendingInput.kind == "owner_request",
                PendingInput.ref_id == row.id,
            )
        )
    await session.flush()
