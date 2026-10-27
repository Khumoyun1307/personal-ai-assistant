"""Account-scoped bulk action changes inside the caller's transaction.

The conversation locks held by ingestion/deletion serialize these helpers
against execution. Conditional updates still refuse any row whose state
changed meanwhile, and never alter a committed send claim.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.actions.models import Action
from assistant.actions.states import LIVE_CARD, PENDING, ActionState, StateReason


async def _change_pending(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_ids: Sequence[int] | None,
    states: frozenset[ActionState],
    target: ActionState,
    reason: StateReason,
    now: datetime,
) -> list[int]:
    previous = select(Action.id.label("old_id"), Action.state.label("old_state")).where(
        Action.account_id == account_id, Action.state.in_(states)
    )
    if conversation_ids is not None:
        previous = previous.where(Action.conversation_id.in_(conversation_ids))
    source = previous.cte("previous_actions")
    rows = (
        await session.execute(
            update(Action)
            .where(Action.account_id == account_id)
            .where(Action.id == source.c.old_id, Action.state == source.c.old_state)
            .values(
                state=target.value, state_reason=reason.value, updated_at=now, approval_token=None
            )
            .returning(Action.id, source.c.old_state, Action.version)
            .execution_options(synchronize_session=False)
        )
    ).all()
    event = "action_cancelled" if target is ActionState.CANCELLED else "action_outdated"
    if reason is StateReason.SUPERSEDED:
        event = "action_superseded"
    for action_id, from_state, version in rows:
        session.add(
            AuditLogEntry(
                account_id=account_id,
                at=now,
                actor="system",
                event=event,
                subject_type="action",
                subject_id=action_id,
                details={
                    "from": from_state,
                    "to": target.value,
                    "version": version,
                    "reason": reason.value,
                },
            )
        )
    await session.flush()
    return sorted(int(row[0]) for row in rows)


async def outdate_pending(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    reason: StateReason,
    include_approved: bool,
    now: datetime,
) -> list[int]:
    """Invalidate live drafts and, on takeover, approved actions too."""
    states = LIVE_CARD | ({ActionState.APPROVED} if include_approved else set())
    return await _change_pending(
        session,
        account_id=account_id,
        conversation_ids=[conversation_id],
        states=frozenset(states),
        target=ActionState.OUTDATED,
        reason=reason,
        now=now,
    )


async def cancel_pending(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_ids: Sequence[int] | None,
    reason: StateReason,
    now: datetime,
) -> list[int]:
    """Cancel only unclaimed actions; None selects the whole account."""
    return await _change_pending(
        session,
        account_id=account_id,
        conversation_ids=conversation_ids,
        states=frozenset(PENDING),
        target=ActionState.CANCELLED,
        reason=reason,
        now=now,
    )


async def clear_texts(
    session: AsyncSession, *, account_id: int, conversation_ids: Sequence[int] | None
) -> int:
    """Erase retained action text without changing delivery state."""
    statement = update(Action).where(Action.account_id == account_id, Action.text.is_not(None))
    if conversation_ids is not None:
        statement = statement.where(Action.conversation_id.in_(conversation_ids))
    result = await session.execute(statement.values(text=None).returning(Action.id))
    return len(result.all())
