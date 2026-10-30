"""Activity read model over actions and audit_log (S04.5; PRD FR-ACT-01, FR-ACT-02)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.actions.models import Action
from assistant.actions.states import LIVE_CARD, ActionState
from assistant.conversations.models import Contact, Conversation, Message

_GROUP_STATES: dict[str, set[str]] = {
    "sent": {ActionState.SUCCEEDED.value},
    "uncertain": {ActionState.DELIVERY_UNCERTAIN.value},
    "failed": {ActionState.FAILED.value},
    "blocked": {ActionState.BLOCKED.value},
    "not_sent": {
        ActionState.REJECTED.value,
        ActionState.OUTDATED.value,
        ActionState.EXPIRED.value,
        ActionState.CANCELLED.value,
    },
    "awaiting": {
        ActionState.PROPOSED.value,
        ActionState.AWAITING_APPROVAL.value,
        ActionState.REVISING.value,
        ActionState.APPROVED.value,
        ActionState.SCHEDULED.value,
        ActionState.EXECUTING.value,
    },
}

_STATE_TO_GROUP: dict[str, str] = {}
for _group, _states in _GROUP_STATES.items():
    for _s in _states:
        _STATE_TO_GROUP[_s] = _group


def group_of(state: str) -> str:
    """Return the activity group for an action state string.

    Raises ValueError on an unknown state.
    """
    if state not in _STATE_TO_GROUP:
        raise ValueError(f"Unknown action state: {state!r}")
    return _STATE_TO_GROUP[state]


@dataclass(frozen=True, slots=True)
class Authorization:
    kind: Literal["owner_approval"]
    approved_at: datetime
    version: int


@dataclass(frozen=True, slots=True)
class ActivityEntry:
    action_id: int
    conversation_id: int
    contact_id: int
    contact_display_name: str | None
    contact_username: str | None
    origin: str
    state: str
    state_reason: str | None
    group: str
    created_at: datetime
    decided_at: datetime | None
    executed_at: datetime | None
    version: int
    excerpt: str | None
    authorization: Authorization | None
    deleted_in_chat_at: datetime | None


async def count_activity(
    session: AsyncSession,
    *,
    account_id: int,
    since: datetime,
    until: datetime,
    conversation_ids: Sequence[int] | None = None,
    groups: Sequence[str] | None = None,
) -> int:
    """Return the total count of actions for an account within [since, until] matching filters."""
    entry_time = func.coalesce(Action.executed_at, Action.decided_at, Action.created_at)

    statement = (
        select(func.count(Action.id))
        .where(Action.account_id == account_id)
        .where(entry_time >= since)
        .where(entry_time <= until)
    )

    if conversation_ids is not None:
        statement = statement.where(Action.conversation_id.in_(list(conversation_ids)))

    if groups is not None:
        allowed_states: set[str] = set()
        for g in groups:
            allowed_states.update(_GROUP_STATES.get(g, set()))
        statement = statement.where(Action.state.in_(allowed_states))

    return int((await session.execute(statement)).scalar_one())


async def activity(
    session: AsyncSession,
    *,
    account_id: int,
    since: datetime,
    until: datetime,
    conversation_ids: Sequence[int] | None = None,
    groups: Sequence[str] | None = None,
    limit: int = 50,
) -> list[ActivityEntry]:
    """List activity entries for an account within [since, until] ordered oldest first."""
    entry_time = func.coalesce(Action.executed_at, Action.decided_at, Action.created_at)

    statement = (
        select(Action, Conversation.contact_id, Contact.display_name, Contact.username)
        .join(
            Conversation,
            (Conversation.id == Action.conversation_id) & (Conversation.account_id == account_id),
        )
        .join(
            Contact,
            (Contact.id == Conversation.contact_id) & (Contact.account_id == account_id),
        )
        .where(Action.account_id == account_id)
        .where(entry_time >= since)
        .where(entry_time <= until)
    )

    if conversation_ids is not None:
        statement = statement.where(Action.conversation_id.in_(list(conversation_ids)))

    if groups is not None:
        allowed_states: set[str] = set()
        for g in groups:
            allowed_states.update(_GROUP_STATES.get(g, set()))
        statement = statement.where(Action.state.in_(allowed_states))

    statement = statement.order_by(entry_time.asc(), Action.id.asc()).limit(limit)
    rows = (await session.execute(statement)).all()

    if not rows:
        return []

    # Look up deleted_in_chat_at for sent actions
    deleted_at_by_action: dict[int, datetime | None] = {}
    sent_action_ids = [action.id for action, _, _, _ in rows if group_of(action.state) == "sent"]
    if sent_action_ids:
        msg_stmt = select(Message.action_id, Message.deleted_at).where(
            Message.account_id == account_id,
            Message.action_id.in_(sent_action_ids),
            Message.direction == "assistant_sent",
        )
        msg_rows = (await session.execute(msg_stmt)).all()
        for act_id, del_at in msg_rows:
            if act_id is not None:
                deleted_at_by_action[act_id] = del_at

    # Look up authorization for sent / uncertain / failed actions
    auth_action_ids = [
        action.id
        for action, _, _, _ in rows
        if group_of(action.state) in ("sent", "uncertain", "failed")
    ]
    auth_by_action: dict[int, Authorization] = {}
    if auth_action_ids:
        audit_stmt = (
            select(AuditLogEntry)
            .where(
                AuditLogEntry.account_id == account_id,
                AuditLogEntry.event == "action_approved",
                AuditLogEntry.subject_type == "action",
                AuditLogEntry.subject_id.in_(auth_action_ids),
            )
            .order_by(AuditLogEntry.at.desc(), AuditLogEntry.id.desc())
        )
        audit_rows = (await session.execute(audit_stmt)).scalars().all()
        for audit in audit_rows:
            if audit.subject_id is not None and audit.subject_id not in auth_by_action:
                details = audit.details if isinstance(audit.details, dict) else {}
                if "version" in details:
                    auth_by_action[audit.subject_id] = Authorization(
                        kind="owner_approval",
                        approved_at=audit.at,
                        version=int(details["version"]),
                    )

    entries: list[ActivityEntry] = []
    for action, contact_id, display_name, username in rows:
        group = group_of(action.state)
        excerpt = action.text[:120] if action.text is not None else None
        auth = auth_by_action.get(action.id) if group in ("sent", "uncertain", "failed") else None
        del_at = deleted_at_by_action.get(action.id) if group == "sent" else None

        entries.append(
            ActivityEntry(
                action_id=action.id,
                conversation_id=action.conversation_id,
                contact_id=contact_id,
                contact_display_name=display_name,
                contact_username=username,
                origin=action.origin,
                state=action.state,
                state_reason=action.state_reason,
                group=group,
                created_at=action.created_at,
                decided_at=action.decided_at,
                executed_at=action.executed_at,
                version=action.version,
                excerpt=excerpt,
                authorization=auth,
                deleted_in_chat_at=del_at,
            )
        )

    return entries


@dataclass(frozen=True, slots=True)
class LiveCardFact:
    """One live approval card as an owner run may see it (PF-15): ids,
    version, state, origin, the draft mode and whether it answers a chosen
    message — never its text, never a name (content-free)."""

    action_id: int
    conversation_id: int
    version: int
    state: str
    origin: str
    #: `draft_flags["mode"]` as stored (jsonb): the context shows it only when
    #: it is one of the fixed modes.
    mode: str | None
    answers_message: bool


#: At most this many live cards are loaded for one owner run (newest first).
LIVE_CARDS_LOADED = 50


async def live_cards(
    session: AsyncSession, *, account_id: int, limit: int = LIVE_CARDS_LOADED
) -> list[LiveCardFact]:
    """The account's live approval cards (awaiting approval or being
    revised), newest first: at most one per chat (`ux_actions_live_card`)."""
    rows = await session.execute(
        select(
            Action.id,
            Action.conversation_id,
            Action.version,
            Action.state,
            Action.origin,
            Action.draft_flags["mode"].astext,
            Action.base_message_id,
        )
        .join(
            Conversation,
            (Conversation.id == Action.conversation_id) & (Conversation.account_id == account_id),
        )
        .where(Action.account_id == account_id)
        .where(Action.state.in_([state.value for state in LIVE_CARD]))
        .order_by(Action.id.desc())
        .limit(limit)
    )
    return [
        LiveCardFact(
            action_id=action_id,
            conversation_id=conversation_id,
            version=version,
            state=state,
            origin=origin,
            mode=mode,
            answers_message=base_message_id is not None,
        )
        for action_id, conversation_id, version, state, origin, mode, base_message_id in rows
    ]
