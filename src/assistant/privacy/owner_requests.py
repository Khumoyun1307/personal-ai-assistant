"""Request privacy helpers, used before source clearing/deletion in the same transaction."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import ColumnElement, case, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry, ControlTurn, PendingInput
from assistant.actions.lifecycle import clear_authorship_sources
from assistant.actions.models import Action
from assistant.actions.request_models import (
    TERMINAL_REQUEST_STATES,
    OwnerRequest,
    OwnerRequestSource,
)
from assistant.actions.requests import (
    SUGGESTION_SELECTED,
    cancel_linked_action,
    expire_requests,
    lock_requests,
)
from assistant.actions.states import StateReason
from assistant.memory.items import clear_owner_capture_sources


async def purge_request_sources(
    session: AsyncSession,
    *,
    account_id: int,
    now: datetime,
    turn_ids: Sequence[int] = (),
    conversation_ids: Sequence[int] = (),
    contact_ids: Sequence[int] = (),
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
    await clear_owner_capture_sources(
        session, account_id=account_id, turn_ids=turn_ids, everything=everything
    )
    source_requests = select(OwnerRequestSource.request_id).where(
        OwnerRequestSource.account_id == account_id,
        OwnerRequestSource.turn_id.in_(turn_ids),
    )
    statement = select(OwnerRequest).where(OwnerRequest.account_id == account_id)
    if not everything:
        linked_actions = select(Action.id).where(
            Action.account_id == account_id, Action.conversation_id.in_(conversation_ids)
        )
        candidate_scopes = [
            OwnerRequest.resolution_candidates.contains(
                {"candidates": [{"conversation_id": conversation_id}]}
            )
            for conversation_id in conversation_ids
        ]
        candidate_scopes.extend(
            OwnerRequest.resolution_candidates.contains(
                {"candidates": [{"contact_id": contact_id}]}
            )
            for contact_id in contact_ids
        )
        statement = statement.where(
            or_(
                *candidate_scopes,
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
    # Presentation history contains no prose, but scoped erasure must also
    # remove its control-message associations, including pre-C1 cards.
    action_scope = select(Action.id).where(Action.account_id == account_id)
    if not everything:
        action_scope = action_scope.where(
            or_(
                Action.conversation_id.in_(conversation_ids),
                Action.id.in_([row.action_id for row in rows if row.action_id is not None]),
            )
        )
    action_scope = action_scope.correlate(None)
    reply_scope = update(ControlTurn).where(ControlTurn.account_id == account_id)
    if not everything:
        card_messages = select(Action.card_message_id).where(
            Action.account_id == account_id, Action.id.in_(action_scope)
        )
        presented_messages = select(AuditLogEntry.details["control_message_id"].as_integer()).where(
            AuditLogEntry.account_id == account_id,
            AuditLogEntry.event == "card_presented",
            AuditLogEntry.subject_type == "action",
            AuditLogEntry.subject_id.in_(action_scope),
        )
        source_messages = (
            select(ControlTurn.telegram_message_id)
            .where(ControlTurn.account_id == account_id, ControlTurn.id.in_(turn_ids))
            .correlate(None)
        )
        refers: list[ColumnElement[bool]] = [
            ControlTurn.id.in_(turn_ids),
            ControlTurn.reply_to_telegram_message_id.in_(card_messages),
            ControlTurn.reply_to_telegram_message_id.in_(presented_messages),
            ControlTurn.reply_to_telegram_message_id.in_(source_messages),
        ]
        refers.extend(
            ControlTurn.refs.contains({"conversation_ids": [conversation_id]})
            for conversation_id in conversation_ids
        )
        refers.extend(ControlTurn.refs.contains({"owner_request_ids": [row.id]}) for row in rows)
        reply_scope = reply_scope.where(or_(*refers))
    # A queued explicit reply may not have conversation refs yet. Erasing its
    # pointer must not let runtime reinterpret its text against another focus.
    await session.execute(
        reply_scope.values(
            reply_to_telegram_message_id=None,
            refs=case(
                (
                    ControlTurn.reply_to_telegram_message_id.is_not(None),
                    ControlTurn.refs.op("||")({"explicit_target_unavailable": True}),
                ),
                else_=ControlTurn.refs,
            ),
        )
    )
    await session.execute(
        delete(AuditLogEntry).where(
            AuditLogEntry.account_id == account_id,
            AuditLogEntry.event == "card_presented",
            AuditLogEntry.subject_type == "action",
            AuditLogEntry.subject_id.in_(action_scope),
        )
    )
    for row in rows:
        await cancel_linked_action(session, row, now=now, clear_text=True)
        await _delete_selection(session, account_id=account_id, request_id=row.id)
        await _clear_authorship_sources(session, account_id=account_id, request_id=row.id)
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
        row.resolution_candidates = None
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


async def _clear_authorship_sources(
    session: AsyncSession, *, account_id: int, request_id: int
) -> None:
    source_ids = list(
        await session.scalars(
            select(OwnerRequestSource.id).where(
                OwnerRequestSource.account_id == account_id,
                OwnerRequestSource.request_id == request_id,
            )
        )
    )
    await clear_authorship_sources(session, account_id=account_id, source_ids=source_ids)


async def _delete_selection(session: AsyncSession, *, account_id: int, request_id: int) -> None:
    await session.execute(
        delete(AuditLogEntry).where(
            AuditLogEntry.account_id == account_id,
            AuditLogEntry.event == SUGGESTION_SELECTED,
            AuditLogEntry.subject_type == "owner_request",
            AuditLogEntry.subject_id == request_id,
        )
    )


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
    await session.execute(
        delete(AuditLogEntry).where(
            AuditLogEntry.account_id == account_id,
            AuditLogEntry.event == "card_presented",
            AuditLogEntry.subject_type == "action",
            AuditLogEntry.subject_id.in_(action_ids),
        )
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
        await _delete_selection(session, account_id=account_id, request_id=row.id)
        await _clear_authorship_sources(session, account_id=account_id, request_id=row.id)
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
        row.resolution_candidates = None
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
