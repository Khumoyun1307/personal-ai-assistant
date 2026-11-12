"""Activity read model over actions and audit_log (S04.5; PRD FR-ACT-01, FR-ACT-02)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.actions.authorship import AuthorshipKind, current_authorship, is_ai_generated
from assistant.actions.card_facts import (
    Presentation,
    PresentationFact,
    ReceiptPresentation,
    draft_mode,
    presentation_facts,
    receipt_presentations,
    state_reason_fact,
    warning_reason_fact,
)
from assistant.actions.lifecycle import warning_card_filter
from assistant.actions.models import Action
from assistant.actions.request_models import OwnerRequest
from assistant.actions.states import LIVE_CARD, PENDING, TERMINAL, ActionState
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
    updated_at: datetime | None = None
    mode: str | None = None
    presentation_outcome: str | None = None
    presentation_version: int | None = None
    card_state: str | None = None
    has_card_message: bool = False

    @property
    def state_reason_explanation(self) -> str | None:
        return state_reason_fact(self.state_reason)[1]


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

    presentations = await presentation_facts(
        session, account_id=account_id, action_ids=[action.id for action, _, _, _ in rows]
    )
    entries: list[ActivityEntry] = []
    for action, contact_id, display_name, username in rows:
        group = group_of(action.state)
        excerpt = action.text[:120] if action.text is not None else None
        auth = auth_by_action.get(action.id) if group in ("sent", "uncertain", "failed") else None
        del_at = deleted_at_by_action.get(action.id) if group == "sent" else None
        presentation = presentations.get(action.id, PresentationFact())

        entries.append(
            ActivityEntry(
                action_id=action.id,
                conversation_id=action.conversation_id,
                contact_id=contact_id,
                contact_display_name=display_name,
                contact_username=username,
                origin=action.origin,
                state=action.state,
                state_reason=state_reason_fact(action.state_reason)[0],
                group=group,
                created_at=action.created_at,
                decided_at=action.decided_at,
                executed_at=action.executed_at,
                version=action.version,
                excerpt=excerpt,
                authorization=auth,
                deleted_in_chat_at=del_at,
                updated_at=action.updated_at,
                mode=draft_mode(action.draft_flags),
                presentation_outcome=presentation.outcome,
                presentation_version=presentation.version,
                card_state=action.card_state,
                has_card_message=action.card_message_id is not None,
            )
        )

    return entries


@dataclass(frozen=True, slots=True)
class LiveCardFact:
    """One live or recent terminal card, without text or contact names."""

    action_id: int
    conversation_id: int
    version: int
    state: str
    origin: str
    #: `draft_flags["mode"]` as stored (jsonb): the context shows it only when
    #: it is one of the fixed modes.
    mode: str | None
    answers_message: bool
    state_reason: str | None = None
    updated_at: datetime | None = None
    created_at: datetime | None = None
    presentation_outcome: str | None = None
    presentation_version: int | None = None
    authorship_kind: AuthorshipKind | None = None
    ai_generated: bool = True

    @property
    def state_reason_explanation(self) -> str | None:
        return state_reason_fact(self.state_reason)[1]


#: Shared budget for live cards and recent terminal history in one owner run.
LIVE_CARDS_LOADED = 50


async def live_cards(
    session: AsyncSession, *, account_id: int, limit: int = LIVE_CARDS_LOADED
) -> list[LiveCardFact]:
    """Live cards first, then the most recently updated terminal cards.

    The shared budget is capped at 50; terminal history cannot hide live
    cards. No draft text or contact names are read for this context.
    """
    limit = max(0, min(limit, LIVE_CARDS_LOADED))
    if not limit:
        return []
    rows = await session.execute(
        select(
            Action.id,
            Action.conversation_id,
            Action.version,
            Action.state,
            Action.origin,
            Action.draft_flags["mode"].astext,
            Action.base_message_id,
            Action.state_reason,
            Action.updated_at,
            Action.created_at,
            Action.authorship,
        )
        .join(
            Conversation,
            (Conversation.id == Action.conversation_id) & (Conversation.account_id == account_id),
        )
        .where(Action.account_id == account_id)
        .where(
            Action.state.in_(
                [state.value for state in LIVE_CARD | TERMINAL | {ActionState.DELIVERY_UNCERTAIN}]
            )
        )
        .order_by(
            Action.state.in_([state.value for state in LIVE_CARD]).desc(),
            Action.updated_at.desc(),
            Action.id.desc(),
        )
        .limit(limit)
    )
    card_rows = rows.all()
    presentations = await presentation_facts(
        session, account_id=account_id, action_ids=[row[0] for row in card_rows]
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
            state_reason=state_reason_fact(state_reason)[0],
            updated_at=updated_at,
            created_at=created_at,
            presentation_outcome=presentations.get(action_id, PresentationFact()).outcome,
            presentation_version=presentations.get(action_id, PresentationFact()).version,
            authorship_kind=(
                None
                if (proof := current_authorship(authorship, version=version)) is None
                else proof.kind
            ),
            ai_generated=is_ai_generated(authorship, version=version),
        )
        for (
            action_id,
            conversation_id,
            version,
            state,
            origin,
            mode,
            base_message_id,
            state_reason,
            updated_at,
            created_at,
            authorship,
        ) in card_rows
    ]


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    """An ephemeral C3 observation. Never contains text, names or previews."""

    action_id: int
    conversation_id: int
    request_id: int | None
    state: str
    state_reason: str | None
    version: int
    authorship: AuthorshipKind | None
    mode: str | None
    answered_message_id: int | None
    text_available: bool
    presentation: Presentation
    presented_version: int | None
    presented_state: str | None
    replaced_by_action_id: int | None
    observed_at: datetime
    warning_reason: str | None = None
    can_approve_anyway: bool = False


@dataclass(frozen=True, slots=True)
class ReceiptPage:
    receipts: tuple[ActionReceipt, ...]
    omitted_live: int = 0
    omitted_terminal: int = 0


async def refresh_receipts(
    session: AsyncSession, *, account_id: int, action_ids: tuple[int, ...], now: datetime
) -> ReceiptPage:
    """Read only actual scoped artifacts; missing/foreign IDs return no facts."""
    if not action_ids:
        return ReceiptPage(())
    request_id = (
        select(OwnerRequest.id)
        .where(
            OwnerRequest.account_id == account_id,
            (OwnerRequest.action_id == Action.id) | (OwnerRequest.id == Action.owner_request_id),
        )
        .order_by(OwnerRequest.id.desc())
        .limit(1)
        .correlate(Action)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(
                Action.id,
                Action.conversation_id,
                request_id,
                Action.state,
                Action.state_reason,
                Action.version,
                Action.authorship,
                Action.draft_flags["mode"].astext,
                Action.base_message_id,
                Action.text.is_not(None),
                Action.freshness_warning,
                warning_card_filter() & Action.approval_token.is_not(None),
                Action.card_state,
            )
            .join(
                Conversation,
                (Conversation.id == Action.conversation_id)
                & (Conversation.account_id == account_id),
            )
            .where(Action.account_id == account_id, Action.id.in_(action_ids))
            .order_by(Action.id)
        )
    ).all()
    presentations = await receipt_presentations(
        session, account_id=account_id, action_ids=[row[0] for row in rows], now=now
    )
    replacements: dict[int, int] = {}
    # Never trust an unscoped audit ID as an artifact reference.
    audits = await session.execute(
        select(AuditLogEntry.subject_id, AuditLogEntry.details)
        .where(
            AuditLogEntry.account_id == account_id,
            AuditLogEntry.subject_type == "action",
            AuditLogEntry.subject_id.in_(action_ids),
            AuditLogEntry.details["superseded_by"].astext.is_not(None),
        )
        .order_by(AuditLogEntry.id)
    )
    for subject_id, details in audits:
        replacement = details.get("superseded_by")
        if type(replacement) is int:
            scoped = await session.scalar(
                select(Action.id).where(Action.account_id == account_id, Action.id == replacement)
            )
            if scoped is not None:
                replacements[subject_id] = scoped
    receipts: list[ActionReceipt] = []
    for row in rows:
        (
            action_id,
            conversation_id,
            linked_request,
            state,
            reason,
            version,
            authorship,
            mode,
            answered,
            available,
            warning,
            approve_anyway,
            card_state,
        ) = row
        proof = current_authorship(authorship, version=version)
        presentation = presentations.get(action_id, ReceiptPresentation())
        outcome = presentation.outcome
        if (presentation.version is not None and presentation.version != version) or (
            presentation.state is not None and presentation.state != state
        ):
            outcome = "not_attempted"
        if (
            outcome == "shown"
            and presentation.shown_version == version
            and presentation.shown_state == state
            and card_state != state
        ):
            outcome = "unknown"
        receipts.append(
            ActionReceipt(
                action_id=action_id,
                conversation_id=conversation_id,
                request_id=linked_request,
                state=state,
                state_reason=warning_reason_fact(reason)[0],
                version=version,
                authorship=None if proof is None else proof.kind,
                mode=mode if mode in {"reply", "new_message", "revise"} else None,
                answered_message_id=answered,
                text_available=available,
                presentation=outcome,
                presented_version=presentation.shown_version,
                presented_state=presentation.shown_state,
                replaced_by_action_id=replacements.get(action_id),
                observed_at=now,
                warning_reason=warning_reason_fact(warning)[0],
                can_approve_anyway=bool(approve_anyway),
            )
        )
    return ReceiptPage(tuple(receipts))


async def conversation_names(
    session: AsyncSession, *, account_id: int, conversation_ids: tuple[int, ...]
) -> dict[int, str | None]:
    """Contact display names for owner-visible wording only (never logs or model)."""
    if not conversation_ids:
        return {}
    rows = await session.execute(
        select(Conversation.id, Contact.display_name)
        .join(Contact, Contact.id == Conversation.contact_id)
        .where(
            Conversation.account_id == account_id,
            Contact.account_id == account_id,
            Conversation.id.in_(conversation_ids),
        )
    )
    return dict(rows.tuples().all())


async def action_receipts(
    session: AsyncSession,
    *,
    account_id: int,
    request_ids: tuple[int, ...] = (),
    conversation_ids: tuple[int, ...] = (),
    action_ids: tuple[int, ...] = (),
    now: datetime,
) -> ReceiptPage:
    """Explicit refs bypass the fallback cap; relevant recent outcomes survive.

    Only the unselected live-work fallback is capped (20). Counts describe
    omissions rather than implying that an absent action was sent or deleted.
    """
    scoped = (
        select(Action.id)
        .join(
            Conversation,
            (Conversation.id == Action.conversation_id) & (Conversation.account_id == account_id),
        )
        .where(Action.account_id == account_id)
    )
    selected = set(await session.scalars(scoped.where(Action.id.in_(action_ids))))
    linked = select(OwnerRequest.action_id).where(
        OwnerRequest.account_id == account_id,
        OwnerRequest.id.in_(request_ids),
    )
    selected.update(await session.scalars(scoped.where(Action.id.in_(linked))))
    live = (
        Action.state.in_([s.value for s in PENDING | {ActionState.EXECUTING}])
        | warning_card_filter()
    )
    selected.update(
        await session.scalars(scoped.where(Action.conversation_id.in_(conversation_ids), live))
    )
    terminal = ~live
    recent = (
        scoped.where(
            Action.conversation_id.in_(conversation_ids),
            terminal,
            Action.updated_at >= now - timedelta(hours=24),
        )
        .order_by(Action.conversation_id, Action.updated_at.desc(), Action.id.desc())
        .ext(distinct_on(Action.conversation_id))
    )
    selected.update(await session.scalars(recent))
    fallback = list(
        await session.scalars(
            scoped.where(live, Action.id.not_in(selected))
            .order_by(Action.updated_at.desc(), Action.id.desc())
            .limit(20)
        )
    )
    selected.update(fallback)
    counts = await session.execute(
        select(live, func.count(Action.id))
        .join(
            Conversation,
            (Conversation.id == Action.conversation_id) & (Conversation.account_id == account_id),
        )
        .where(Action.account_id == account_id, Action.id.not_in(selected))
        .group_by(live)
    )
    omitted = {bool(is_live): count for is_live, count in counts}
    page = await refresh_receipts(
        session, account_id=account_id, action_ids=tuple(sorted(selected)), now=now
    )
    return ReceiptPage(page.receipts, omitted.get(True, 0), omitted.get(False, 0))
