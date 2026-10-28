"""Action state changes inside the caller's transaction (ARCHITECTURE §9.4).

Bulk helpers (F0): the conversation locks held by ingestion/deletion
serialize them against execution. Single-action transitions (S04.1): one
primitive, `transition`, used by approval, editing, expiry and the executor.
Every change is a conditional UPDATE that refuses a row whose state changed
meanwhile, writes one content-free audit row, and never alters a committed
send claim.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.callbacks import new_token
from assistant.accounts.models import AuditLogEntry
from assistant.actions.models import Action
from assistant.actions.states import (
    LIVE_CARD,
    PENDING,
    TRANSITIONS,
    ActionOrigin,
    ActionState,
    StateReason,
)
from assistant.conversations.models import Conversation
from assistant.core.delivery import MAX_TEXT_LENGTH
from assistant.core.reply_window import SAFETY_MARGIN, closes_at, is_open


async def _change_pending(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_ids: Sequence[int] | None,
    states: frozenset[ActionState],
    target: ActionState,
    reason: StateReason,
    now: datetime,
    origins: frozenset[ActionOrigin] | None = None,
) -> list[int]:
    previous = select(Action.id.label("old_id"), Action.state.label("old_state")).where(
        Action.account_id == account_id, Action.state.in_(states)
    )
    if conversation_ids is not None:
        previous = previous.where(Action.conversation_id.in_(conversation_ids))
    if origins is not None:
        previous = previous.where(Action.origin.in_([origin.value for origin in origins]))
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


# S04.4: origin-filtered and account-wide variants, with the same guarantees.
# The callers hold the locks their path already takes (§9.4 order); a claimed
# action (`executing`) is never touched: its send has its own outcome.
async def cancel_autonomous_pending(
    session: AsyncSession, *, account_id: int, now: datetime
) -> list[int]:
    """`/pause` (P2-T23): every pending autonomous action of the account is
    cancelled; drafts and owner approvals stay what they are."""
    return await _change_pending(
        session,
        account_id=account_id,
        conversation_ids=None,
        states=frozenset(PENDING),
        target=ActionState.CANCELLED,
        reason=StateReason.AUTONOMY_PAUSED,
        now=now,
        origins=frozenset({ActionOrigin.DELEGATION_AUTO}),
    )


async def outdate_pending_for_account(
    session: AsyncSession,
    *,
    account_id: int,
    reason: StateReason,
    include_approved: bool,
    now: datetime,
) -> list[int]:
    """`outdate_pending` for every conversation of the account (a rights
    change: what was approved under the old rights must be looked at again)."""
    states = LIVE_CARD | ({ActionState.APPROVED} if include_approved else set())
    return await _change_pending(
        session,
        account_id=account_id,
        conversation_ids=None,
        states=frozenset(states),
        target=ActionState.OUTDATED,
        reason=reason,
        now=now,
    )


async def cancel_pending_ai_generated(
    session: AsyncSession, *, account_id: int, reason: StateReason, now: datetime
) -> list[int]:
    """`ai_managed` revoked: pending AI drafts are cancelled; texts the owner
    dictated are the owner's own words, not AI content, and stay."""
    return await _change_pending(
        session,
        account_id=account_id,
        conversation_ids=None,
        states=frozenset(PENDING),
        target=ActionState.CANCELLED,
        reason=reason,
        now=now,
        origins=frozenset({ActionOrigin.AGENT_PROPOSAL}),
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


# ------------------------------------------------------- single-action changes
# S04.1 §3.6. Every state change of one action goes through `transition`: a
# conditional UPDATE (never read-modify-write) plus exactly one content-free
# audit row whose event follows from the pair (decisions R15).

_NO_VALUES: Mapping[str, Any] = MappingProxyType({})
#: Columns a caller may never set through `values`: the primitive owns them.
_PRIMITIVE_COLUMNS = frozenset({"id", "account_id", "state", "state_reason", "updated_at"})
#: Pairs only the owner's own press or message causes (the audit `actor`).
_OWNER_PAIRS = frozenset(
    {
        (ActionState.AWAITING_APPROVAL, ActionState.APPROVED),
        (ActionState.AWAITING_APPROVAL, ActionState.REJECTED),
        (ActionState.AWAITING_APPROVAL, ActionState.REVISING),
    }
)
_OUTCOME_EVENTS = MappingProxyType(
    {
        ActionState.SUCCEEDED: "action_succeeded",
        ActionState.FAILED: "action_failed",
        ActionState.DELIVERY_UNCERTAIN: "action_delivery_uncertain",
    }
)
_TARGET_EVENTS = MappingProxyType(
    {
        ActionState.REVISING: "action_revision_requested",
        ActionState.REJECTED: "action_rejected",
        ActionState.BLOCKED: "action_blocked",
        ActionState.EXPIRED: "action_expired",
        ActionState.CANCELLED: "action_cancelled",
    }
)


class IllegalTransitionError(AssertionError):
    """A pair outside `TRANSITIONS`, or one P2 never takes: a bug, not an outcome."""


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units: conservative against Telegram's count."""
    return len(text.encode("utf-16-le")) // 2


def sendable_length(text: str) -> int:
    """`utf16_len`, but 0 for whitespace-only text: Telegram rejects it as empty."""
    return utf16_len(text) if text.strip() else 0


def text_hash(text: str) -> str:
    """sha256 hex of the UTF-8 text; equals `_SQL_TEXT_HASH` of the stored row."""
    return hashlib.sha256(text.encode()).hexdigest()


#: The stored text's hash, computed in the UPDATE itself: the hash is of the
#: exact text the conditional update approved, never of an earlier read.
_SQL_TEXT_HASH = func.encode(func.sha256(func.convert_to(Action.text, "UTF8")), "hex")


def _event(
    from_state: ActionState, to_state: ActionState, *, reason: StateReason | None, new_version: bool
) -> str:
    pair = (from_state, to_state)
    if pair == (ActionState.PROPOSED, ActionState.AWAITING_APPROVAL):
        return "action_proposed"
    if pair == (ActionState.REVISING, ActionState.AWAITING_APPROVAL):
        return "action_edited" if new_version else "action_revision_cancelled"
    if pair == (ActionState.AWAITING_APPROVAL, ActionState.APPROVED):
        return "action_approved"
    if pair == (ActionState.EXECUTING, ActionState.APPROVED):
        return "action_rate_limited"
    if pair == (ActionState.APPROVED, ActionState.EXECUTING):
        return "action_claimed"
    if from_state is ActionState.DELIVERY_UNCERTAIN:
        return "action_late_result"
    if from_state is ActionState.EXECUTING and to_state in _OUTCOME_EVENTS:
        return _OUTCOME_EVENTS[to_state]
    if to_state is ActionState.OUTDATED:
        return "action_superseded" if reason is StateReason.SUPERSEDED else "action_outdated"
    if to_state in _TARGET_EVENTS:
        return _TARGET_EVENTS[to_state]
    # proposed → approved, approved → scheduled, scheduled → approved belong to
    # autonomy (M06), which defines their events; nothing in P2 takes them.
    raise IllegalTransitionError(f"{from_state} -> {to_state} has no P2 audit event")


async def transition(
    session: AsyncSession,
    *,
    account_id: int,
    action_id: int,
    from_state: ActionState,
    to_state: ActionState,
    reason: StateReason | None,
    now: datetime,
    expected_version: int | None = None,
    token: str | None = None,
    values: Mapping[str, Any] = _NO_VALUES,
    details: Mapping[str, Any] = _NO_VALUES,
) -> bool:
    """Move one action `from_state → to_state` if it is still there.

    Conditional on the account, the state and (when given) the version and
    approval token; returns whether it applied. Clears the token unless the
    new state is `awaiting_approval`. Raises `IllegalTransitionError` for a
    pair outside `TRANSITIONS` and for an approval without a token. `details`
    adds ids or codes to the audit row (never content); the event name is
    chosen here, never by the caller.
    """
    if (from_state, to_state) not in TRANSITIONS:
        raise IllegalTransitionError(f"{from_state} -> {to_state} is not a transition")
    if (
        from_state is ActionState.AWAITING_APPROVAL
        and to_state is ActionState.APPROVED
        and token is None
    ):
        # Only the owner's press approves a card (invariant 1); `approve` passes
        # its token. Tx B's 429 return (`executing → approved`) is not a decision.
        raise IllegalTransitionError("an approval needs the card's token")
    if reserved := _PRIMITIVE_COLUMNS & values.keys():
        raise IllegalTransitionError(f"values may not set {sorted(reserved)}")
    event = _event(from_state, to_state, reason=reason, new_version="version" in values)
    statement = update(Action).where(
        Action.id == action_id, Action.account_id == account_id, Action.state == from_state.value
    )
    if expected_version is not None:
        statement = statement.where(Action.version == expected_version)
    if token is not None:
        statement = statement.where(Action.approval_token == token)
    changes: dict[str, Any] = {
        "state": to_state.value,
        "state_reason": None if reason is None else reason.value,
        "updated_at": now,
        **values,
    }
    if to_state is not ActionState.AWAITING_APPROVAL:
        # A token is a live card's Approve button; no other state may keep one.
        changes["approval_token"] = None
    row = (
        await session.execute(
            statement.values(**changes)
            .returning(Action.version)
            .execution_options(synchronize_session=False)
        )
    ).one_or_none()
    if row is None:
        return False
    audit: dict[str, Any] = {
        "from": from_state.value,
        "to": to_state.value,
        "version": row.version,
        "reason": None if reason is None else reason.value,
        **details,
    }
    if values.get("attempt_id") is not None:
        # The claim's new attempt; a transition that clears it (a 429's
        # return to `approved`) names the closed attempt through `details`.
        audit["attempt_id"] = values["attempt_id"]
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=now,
            actor="owner" if (from_state, to_state) in _OWNER_PAIRS else "system",
            event=event,
            subject_type="action",
            subject_id=action_id,
            details=audit,
        )
    )
    await session.flush()
    return True


class ApproveStatus(StrEnum):
    APPROVED = "approved"
    EXPIRED = "expired"
    NOT_ACTIVE = "not_active"


@dataclass(frozen=True, slots=True)
class ApproveResult:
    status: ApproveStatus
    action_id: int | None = None
    version: int | None = None


class VersionStatus(StrEnum):
    APPLIED = "applied"
    TOO_LONG = "too_long"
    EMPTY = "empty"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class NewVersion:
    status: VersionStatus
    version: int | None = None
    token: str | None = None


@dataclass(frozen=True, slots=True)
class _Live:
    id: int
    conversation_id: int
    version: int
    expires_at: datetime | None


async def _live_by_token(session: AsyncSession, *, account_id: int, token: str) -> _Live | None:
    row = (
        await session.execute(
            select(Action.id, Action.conversation_id, Action.version, Action.expires_at).where(
                Action.account_id == account_id,
                Action.approval_token == token,
                Action.state == ActionState.AWAITING_APPROVAL.value,
            )
        )
    ).one_or_none()
    return None if row is None else _Live(*row)


async def _expiry_reason(
    session: AsyncSession, *, account_id: int, conversation_id: int, now: datetime
) -> StateReason:
    anchor = await session.scalar(
        select(Conversation.last_contact_msg_at).where(
            Conversation.id == conversation_id, Conversation.account_id == account_id
        )
    )
    return StateReason.MAX_AGE if is_open(anchor, now) else StateReason.WINDOW_CLOSED


def _is_past(expires_at: datetime | None, now: datetime) -> bool:
    return expires_at is not None and now >= expires_at


async def _expire_live(
    session: AsyncSession, *, account_id: int, live: _Live, token: str, now: datetime
) -> bool:
    reason = await _expiry_reason(
        session, account_id=account_id, conversation_id=live.conversation_id, now=now
    )
    return await transition(
        session,
        account_id=account_id,
        action_id=live.id,
        from_state=ActionState.AWAITING_APPROVAL,
        to_state=ActionState.EXPIRED,
        reason=reason,
        now=now,
        expected_version=live.version,
        token=token,
    )


async def approve(
    session: AsyncSession, *, account_id: int, token: str, now: datetime
) -> ApproveResult:
    """The owner's Approve: valid only for the card's current token and version.

    A card past `expires_at` is expired here instead (`window_closed` or
    `max_age`). The caller defers `actions.execute` in the same transaction.
    """
    live = await _live_by_token(session, account_id=account_id, token=token)
    if live is None:
        return ApproveResult(ApproveStatus.NOT_ACTIVE)
    if _is_past(live.expires_at, now):
        expired = await _expire_live(
            session, account_id=account_id, live=live, token=token, now=now
        )
        return ApproveResult(ApproveStatus.EXPIRED if expired else ApproveStatus.NOT_ACTIVE)
    applied = await transition(
        session,
        account_id=account_id,
        action_id=live.id,
        from_state=ActionState.AWAITING_APPROVAL,
        to_state=ActionState.APPROVED,
        reason=None,
        now=now,
        expected_version=live.version,
        token=token,
        values={"decided_at": now, "approved_text_hash": _SQL_TEXT_HASH},
    )
    if not applied:
        return ApproveResult(ApproveStatus.NOT_ACTIVE)
    return ApproveResult(ApproveStatus.APPROVED, live.id, live.version)


async def reject(session: AsyncSession, *, account_id: int, token: str, now: datetime) -> bool:
    """The owner's Reject; terminal."""
    live = await _live_by_token(session, account_id=account_id, token=token)
    if live is None:
        return False
    return await transition(
        session,
        account_id=account_id,
        action_id=live.id,
        from_state=ActionState.AWAITING_APPROVAL,
        to_state=ActionState.REJECTED,
        reason=StateReason.OWNER_REJECTED,
        now=now,
        expected_version=live.version,
        token=token,
        values={"decided_at": now},
    )


async def start_edit(
    session: AsyncSession, *, account_id: int, token: str, now: datetime
) -> int | None:
    """The owner's Edit: the version being edited, or None when the card is no
    longer active (a card past `expires_at` is expired instead). The token is
    voided, so Approve while revising answers "no longer active"."""
    live = await _live_by_token(session, account_id=account_id, token=token)
    if live is None:
        return None
    if _is_past(live.expires_at, now):
        await _expire_live(session, account_id=account_id, live=live, token=token, now=now)
        return None
    applied = await transition(
        session,
        account_id=account_id,
        action_id=live.id,
        from_state=ActionState.AWAITING_APPROVAL,
        to_state=ActionState.REVISING,
        reason=None,
        now=now,
        expected_version=live.version,
        token=token,
    )
    return live.version if applied else None


async def set_revision_turn(
    session: AsyncSession,
    *,
    account_id: int,
    action_id: int,
    expected_version: int,
    turn_id: int | None,
) -> bool:
    """Record (or clear) the owner's revision message; no state change, no audit."""
    row = (
        await session.execute(
            update(Action)
            .where(
                Action.id == action_id,
                Action.account_id == account_id,
                Action.state == ActionState.REVISING.value,
                Action.version == expected_version,
            )
            .values(revision_turn_id=turn_id)
            .returning(Action.id)
            .execution_options(synchronize_session=False)
        )
    ).one_or_none()
    return row is not None


async def _back_to_card(
    session: AsyncSession, *, account_id: int, action_id: int, expected_version: int, now: datetime
) -> str | None:
    token = new_token()
    applied = await transition(
        session,
        account_id=account_id,
        action_id=action_id,
        from_state=ActionState.REVISING,
        to_state=ActionState.AWAITING_APPROVAL,
        reason=None,
        now=now,
        expected_version=expected_version,
        values={"approval_token": token, "revision_turn_id": None},
    )
    return token if applied else None


async def cancel_edit(
    session: AsyncSession, *, account_id: int, action_id: int, expected_version: int, now: datetime
) -> str | None:
    """Back to the same version's card with a new token; None when stale."""
    return await _back_to_card(
        session,
        account_id=account_id,
        action_id=action_id,
        expected_version=expected_version,
        now=now,
    )


async def revision_failed(
    session: AsyncSession, *, account_id: int, action_id: int, expected_version: int, now: datetime
) -> str | None:
    """A revision run failed: the same version and text, with a new token."""
    return await _back_to_card(
        session,
        account_id=account_id,
        action_id=action_id,
        expected_version=expected_version,
        now=now,
    )


async def apply_new_version(
    session: AsyncSession,
    *,
    account_id: int,
    action_id: int,
    expected_version: int,
    text: str,
    draft_run_id: int | None,
    draft_flags: Mapping[str, Any] | None,
    shown_through_message_id: int | None,
    base_message_id: int | None,
    now: datetime,
) -> NewVersion:
    """A revised text becomes version + 1 with a new token.

    `expires_at` is never extended: it only shortens when the reply window
    now closes earlier (decisions R7). A stale version, or a too-long or
    empty text, writes nothing and leaves the row `revising`.
    """
    conversation_id = await session.scalar(
        select(Action.conversation_id).where(
            Action.id == action_id,
            Action.account_id == account_id,
            Action.state == ActionState.REVISING.value,
            Action.version == expected_version,
        )
    )
    if conversation_id is None:
        return NewVersion(VersionStatus.STALE)
    length = sendable_length(text)
    if length == 0:
        return NewVersion(VersionStatus.EMPTY)
    if length > MAX_TEXT_LENGTH:
        return NewVersion(VersionStatus.TOO_LONG)
    anchor = await session.scalar(
        select(Conversation.last_contact_msg_at).where(
            Conversation.id == conversation_id, Conversation.account_id == account_id
        )
    )
    window_end = closes_at(anchor)
    expires_at: Any = Action.expires_at
    if window_end is not None:
        expires_at = func.least(Action.expires_at, window_end - SAFETY_MARGIN)
    token = new_token()
    applied = await transition(
        session,
        account_id=account_id,
        action_id=action_id,
        from_state=ActionState.REVISING,
        to_state=ActionState.AWAITING_APPROVAL,
        reason=None,
        now=now,
        expected_version=expected_version,
        values={
            "version": expected_version + 1,
            "text": text,
            "draft_run_id": draft_run_id,
            "draft_flags": None if draft_flags is None else dict(draft_flags),
            "shown_through_message_id": shown_through_message_id,
            "base_message_id": base_message_id,
            "approval_token": token,
            "revision_turn_id": None,
            "expires_at": expires_at,
        },
    )
    if not applied:
        return NewVersion(VersionStatus.STALE)
    return NewVersion(VersionStatus.APPLIED, expected_version + 1, token)


_EXPIRABLE = (ActionState.AWAITING_APPROVAL, ActionState.REVISING, ActionState.APPROVED)


async def expire_due(session: AsyncSession, *, now: datetime) -> list[int]:
    """Expire every live card and unclaimed approval past `expires_at`.

    Reason `window_closed` when the reply window is closed now, else
    `max_age`. Never touches `executing`: a claimed send has its own outcome.
    """
    rows = (
        await session.execute(
            select(
                Action.id,
                Action.account_id,
                Action.state,
                Action.version,
                Conversation.last_contact_msg_at,
            )
            .join(Conversation, Conversation.id == Action.conversation_id)
            .where(
                Action.state.in_([state.value for state in _EXPIRABLE]),
                Action.expires_at <= now,
            )
            .order_by(Action.id)
        )
    ).all()
    expired: list[int] = []
    for action_id, account_id, state, version, anchor in rows:
        reason = StateReason.MAX_AGE if is_open(anchor, now) else StateReason.WINDOW_CLOSED
        if await transition(
            session,
            account_id=account_id,
            action_id=action_id,
            from_state=ActionState(state),
            to_state=ActionState.EXPIRED,
            reason=reason,
            now=now,
            expected_version=version,
        ):
            expired.append(action_id)
    return expired
