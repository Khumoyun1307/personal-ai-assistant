"""Every owner-voice reply starts here (S04.1 §3.5; ARCHITECTURE §8.3, §9.4).

`propose` turns a draft or a dictated text into an action row and decides,
with the proposal-phase policy, whether it becomes an approval card
(`awaiting_approval`) or is `blocked` with copy-ready text. It never sends,
never presents a card and never defers a job: callers do that after commit.

Locks follow the global order: account SHARE, sorted dependency/recipient
conversations UPDATE, then the linked request. Concurrent proposals for a
chat serialize and it keeps at most one live card.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.callbacks import new_token
from assistant.accounts.models import Account, AuditLogEntry, ControlTurn
from assistant.actions.authorship import VersionAuthorship, generated_authorship, is_ai_generated
from assistant.actions.config import ActionsConfig
from assistant.actions.lifecycle import (
    legacy_warning_card_filter,
    sendable_length,
    transition,
    warning_card_filter,
)
from assistant.actions.models import Action, ContextFenceRecord
from assistant.actions.policy_loader import decision_from_record, decision_record, load_context
from assistant.actions.request_models import OwnerRequest
from assistant.actions.states import LIVE_CARD, ActionKind, ActionOrigin, ActionState, StateReason
from assistant.conversations.fences import ContextFence, FenceReason, lock_fence_conversations
from assistant.conversations.intent import advance_intent, select_legacy_intent
from assistant.conversations.models import Conversation, Message
from assistant.core.clock import Clock
from assistant.core.errors import NotInAccountError
from assistant.policy.context import ActionFacts, Decision, Outcome, Phase
from assistant.policy.engine import decide, execution_target


@dataclass(frozen=True, slots=True)
class CurrentOwnerFence:
    """Application-validated snapshot, never authority supplied by the model."""

    fence_id: int
    shown_through_message_id: int | None
    warning: FenceReason | None = None


@dataclass(frozen=True, slots=True)
class UnavailableOwnerFence:
    reason: Literal[
        "not_found",
        "source_edited",
        "source_deleted",
        "new_contact_message",
        "takeover",
        "owner_intent_superseded",
        "request_changed",
        "source_unavailable",
    ]


class OwnerProposalFence(Protocol):
    """H10/H11 seam. There is deliberately no permissive implementation.

    Caller holds the account lock. check_fence must verify account/run/request
    binding and lock ALL dependency/recipient conversations in sorted order,
    before the caller locks the request/action. Locks last through settlement.
    bind_action atomically associates the checked snapshot and original request
    time with the committed version; H10 owns its persistence and execution hook.
    Neither method performs network I/O or changes approval/send authority.
    """

    async def check_fence(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        run_id: int,
        fence_id: int | None,
        request_id: int,
        expected_request_version: int,
        conversation_id: int,
        requested_at: datetime,
    ) -> CurrentOwnerFence | UnavailableOwnerFence: ...

    async def bind_action(
        self,
        session: AsyncSession,
        *,
        account_id: int,
        action_id: int,
        version: int,
        fence: CurrentOwnerFence,
        request_id: int,
        requested_at: datetime,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ProposalRequest:
    account_id: int
    conversation_id: int
    origin: ActionOrigin
    kind: ActionKind
    text: str
    base_message_id: int | None
    shown_through_message_id: int | None
    run_id: int | None
    tool_call_id: str | None
    trigger_turn_id: int | None
    draft_run_id: int | None
    draft_flags: Mapping[str, Any] | None = None
    authorship: VersionAuthorship | None = None
    context_fence_id: int | None = None
    requested_at: datetime | None = None
    owner_request_id: int | None = None
    expected_request_version: int | None = None
    explicit_owner_work: bool = False


@dataclass(frozen=True, slots=True)
class ProposalResult:
    action_id: int
    state: ActionState
    reused: bool
    superseded_action_id: int | None
    decision: Decision


class ProposalRefused(Exception):  # noqa: N818 - the name is the F0 contract's
    """A proposal this phase never accepts; tools report it as "not available"."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


async def propose(
    session: AsyncSession, request: ProposalRequest, *, clock: Clock, config: ActionsConfig
) -> ProposalResult:
    now = clock.now()
    if request.authorship is not None and request.authorship.version != 1:
        raise ProposalRefused("authorship_version_invalid")
    if request.origin is ActionOrigin.DELEGATION_AUTO:
        raise ProposalRefused("autonomous_not_available")  # no delegations in P2
    if request.kind is not ActionKind.SEND_MESSAGE:
        raise ProposalRefused("kind_not_proposable")  # edit/delete: RG-12 mechanics only
    if (
        request.origin is ActionOrigin.AGENT_PROPOSAL
        and request.trigger_turn_id is None
        and request.base_message_id is None
    ):
        # A proactive draft's takeover reference is its base message's date;
        # without one, an owner reply sent before the draft would not count.
        raise ProposalRefused("base_required")
    if (existing := await _by_call(session, request)) is not None:
        return _reused(existing)
    await _lock_account_share(session, request.account_id)
    # Privacy erasure may have removed only some of this run's proofs.
    # Surviving fences must never authorize recreating erased derived text.
    for generating_run_id in {request.run_id, request.draft_run_id} - {None}:
        invalidated = await session.scalar(
            text(
                "SELECT sources_invalidated FROM app.agent_runs "
                "WHERE account_id = :account_id AND id = :run_id"
            ),
            {"account_id": request.account_id, "run_id": generating_run_id},
        )
        if invalidated:
            raise ProposalRefused("source_unavailable")
    await lock_fence_conversations(
        session,
        account_id=request.account_id,
        conversation_id=request.conversation_id,
        fence_id=request.context_fence_id,
    )
    owned_request: OwnerRequest | None = None
    if request.owner_request_id is not None:
        owned_request = await session.scalar(
            select(OwnerRequest)
            .where(
                OwnerRequest.account_id == request.account_id,
                OwnerRequest.id == request.owner_request_id,
                OwnerRequest.conversation_id == request.conversation_id,
            )
            .with_for_update()
        )
        if owned_request is None:
            raise ProposalRefused("source_unavailable")
        if (
            request.expected_request_version is not None
            and owned_request.version != request.expected_request_version
        ):
            raise ProposalRefused("request_changed")
    if request.base_message_id is not None:
        base = await session.scalar(
            select(Message).where(
                Message.account_id == request.account_id,
                Message.conversation_id == request.conversation_id,
                Message.id == request.base_message_id,
            )
        )
        if base is None or base.purged_at is not None:
            raise ProposalRefused("source_unavailable")
        if base.deleted_at is not None and request.context_fence_id is None:
            raise ProposalRefused("source_unavailable")
    # A retried owner turn reuses its live card instead of superseding it.
    if (live := await _live_for_turn(session, request)) is not None:
        return _reused(live)

    shown_through = request.shown_through_message_id
    # Only the owner's own words are fresh through "now". An AI draft is fresh
    # through what it saw; without that id `not_stale` fails closed.
    if shown_through is None and request.origin is ActionOrigin.OWNER_DICTATED:
        shown_through = await _newest_contact_id(session, request)
    expires_at = now + config.approval_max_age  # owner-approved: max age only (P2-O20)
    requested_at = request.requested_at
    if request.context_fence_id is not None:
        record = await session.scalar(
            select(ContextFenceRecord).where(
                ContextFenceRecord.account_id == request.account_id,
                ContextFenceRecord.id == request.context_fence_id,
            )
        )
        if record is None:
            # Explicit privacy erasure may remove an in-flight generation's
            # proof. It must not recreate content derived from the erased scope.
            raise ProposalRefused("source_unavailable")
        requested_at = ContextFence.model_validate(record.facts).requested_at
    elif request.owner_request_id is not None:
        assert owned_request is not None
        requested_at = owned_request.requested_at
    elif request.trigger_turn_id is not None:
        requested_at = await session.scalar(
            select(ControlTurn.created_at).where(
                ControlTurn.account_id == request.account_id,
                ControlTurn.id == request.trigger_turn_id,
            )
        )
    facts = ActionFacts(
        action_id=None,
        kind=request.kind,
        origin=request.origin,
        state=ActionState.PROPOSED,
        version=1,
        text_length=sendable_length(request.text),
        created_at=now,
        expires_at=expires_at,
        shown_through_message_id=shown_through,
        owner_retry=False,
        requested_at=requested_at,
        context_fence_id=request.context_fence_id,
        generating_run_id=request.draft_run_id or request.run_id,
        owner_request_id=request.owner_request_id,
        expected_request_version=request.expected_request_version,
        # Source freshness is advisory for an owner-reviewable proposal.
        freshness_override=True,
    )
    # Decisions R3: an owner request is measured from its own creation, never
    # from its base message. A dictated message has a base message only when
    # the owner named the message it answers (PF-F); the card then quotes it.
    owner_requested = (
        request.origin is ActionOrigin.OWNER_DICTATED
        or request.trigger_turn_id is not None
        or request.owner_request_id is not None
        or request.explicit_owner_work
    )
    base_message_id = request.base_message_id
    context = await load_context(
        session,
        account_id=request.account_id,
        conversation_id=request.conversation_id,
        facts=facts,
        base_message_id=base_message_id,
        target_message_id=None,
        owner_requested=owner_requested,
        phase=Phase.PROPOSAL,
        now=now,
        config=config,
        ai_generated=is_ai_generated(_authorship(request), version=1),
        process_run_id=None,
    )
    decision = decide(context)

    if not owner_requested:
        # D-S10: background work yields before inserting/presenting anything.
        # D-S17's warning override belongs to owner review, not to Assist's
        # authority to replace an explicit task. No owner approval is vetoed.
        if context.freshness.source_fence_reason in {"takeover", "owner_intent_superseded"}:
            raise ProposalRefused(context.freshness.source_fence_reason)
        explicit = await session.scalar(
            select(Action.id).where(
                Action.account_id == request.account_id,
                Action.conversation_id == request.conversation_id,
                or_(
                    Action.origin == ActionOrigin.OWNER_DICTATED.value,
                    Action.trigger_turn_id.is_not(None),
                    Action.owner_request_id.is_not(None),
                    Action.state == ActionState.REVISING.value,
                    Action.state.in_([ActionState.APPROVED.value, ActionState.EXECUTING.value]),
                    select(AuditLogEntry.id)
                    .where(
                        AuditLogEntry.account_id == request.account_id,
                        AuditLogEntry.subject_type == "action",
                        AuditLogEntry.subject_id == Action.id,
                        or_(
                            AuditLogEntry.event == "action_revision_requested",
                            (AuditLogEntry.event == "action_proposed")
                            & AuditLogEntry.details["owner_requested"].as_boolean().is_(True),
                        ),
                    )
                    .correlate(Action)
                    .exists(),
                ),
                or_(
                    Action.state.in_([state.value for state in LIVE_CARD]),
                    warning_card_filter(),
                    Action.state.in_([ActionState.APPROVED.value, ActionState.EXECUTING.value]),
                ),
            )
        )
        selected_request = await session.scalar(
            select(OwnerRequest.id).where(
                OwnerRequest.account_id == request.account_id,
                OwnerRequest.conversation_id == request.conversation_id,
                OwnerRequest.state.in_(["collecting", "ready", "preparing"]),
                OwnerRequest.expires_at > now,
            )
        )
        if explicit is not None or selected_request is not None:
            raise ProposalRefused("owner_intent_superseded")

    action_id = await _insert(
        session, request, facts, decision, base_message_id=base_message_id, now=now
    )
    if action_id is None:
        # The same model call raced us and committed first.
        existing = await _by_call(session, request)
        if existing is None:  # pragma: no cover - the conflict implies the row
            raise RuntimeError("conflicting proposal row not found")
        return _reused(existing)

    if decision.outcome is Outcome.REQUIRE_APPROVAL:
        if (
            owner_requested
            and request.owner_request_id is None
            and request.draft_run_id is None
            and not request.explicit_owner_work
        ):
            if request.trigger_turn_id is None:
                await advance_intent(
                    session, account_id=request.account_id, conversation_id=request.conversation_id
                )
            else:
                await select_legacy_intent(
                    session,
                    account_id=request.account_id,
                    conversation_id=request.conversation_id,
                    turn_id=request.trigger_turn_id,
                    now=now,
                )
        superseded = await _supersede_live(session, request, new_action_id=action_id, now=now)
        await transition(
            session,
            account_id=request.account_id,
            action_id=action_id,
            from_state=ActionState.PROPOSED,
            to_state=ActionState.AWAITING_APPROVAL,
            reason=None,
            now=now,
            values={
                "approval_token": new_token(),
                "freshness_warning": context.freshness.source_fence_reason,
            },
            details={
                "authorship": _authorship(request),
                **({"owner_requested": True} if request.explicit_owner_work else {}),
            },
        )
        return ProposalResult(action_id, ActionState.AWAITING_APPROVAL, False, superseded, decision)
    # A draft that cannot be sent never destroys a card that can.
    failed = [check.name for check in decision.checks if not check.passed]
    await transition(
        session,
        account_id=request.account_id,
        action_id=action_id,
        from_state=ActionState.PROPOSED,
        to_state=ActionState.BLOCKED,
        reason=_blocked_reason(decision),
        now=now,
        details={"failed_rules": failed, "authorship": _authorship(request)},
    )
    return ProposalResult(action_id, ActionState.BLOCKED, False, None, decision)


def _blocked_reason(decision: Decision) -> StateReason:
    target = execution_target(decision)
    return StateReason.POLICY_DENIED if target is None else target[1]


def _authorship(request: ProposalRequest) -> dict[str, Any] | None:
    if request.authorship is not None:
        return request.authorship.model_dump(mode="json")
    if request.origin is ActionOrigin.AGENT_PROPOSAL:
        return generated_authorship(1).model_dump(mode="json")
    # The pre-H3 dictation path used fuzzy provenance. Do not turn that
    # immutable trigger into source proof; H3 supplies validated C1 ids.
    return None


def _reused(action: Action) -> ProposalResult:
    return ProposalResult(
        action.id,
        ActionState(action.state),
        True,
        None,
        decision_from_record(action.policy_decision),
    )


async def _by_call(session: AsyncSession, request: ProposalRequest) -> Action | None:
    """The action an earlier attempt of the same model call created, if any."""
    if request.run_id is None or request.tool_call_id is None:
        return None
    return await session.scalar(
        select(Action).where(
            Action.account_id == request.account_id,
            Action.run_id == request.run_id,
            Action.tool_call_id == request.tool_call_id,
        )
    )


async def _lock_account_share(session: AsyncSession, account_id: int) -> None:
    found = await session.scalar(
        select(Account.id).where(Account.id == account_id).with_for_update(read=True)
    )
    if found is None:
        raise NotInAccountError("account not found")


async def _lock_conversation_update(
    session: AsyncSession, account_id: int, conversation_id: int
) -> None:
    """Lock the chat (one live card per chat)."""
    found = await session.scalar(
        select(Conversation.id)
        .where(Conversation.id == conversation_id, Conversation.account_id == account_id)
        .with_for_update()
    )
    if found is None:
        raise NotInAccountError("conversation not found")


async def _live_for_turn(session: AsyncSession, request: ProposalRequest) -> Action | None:
    """The live card an earlier run of the same owner turn created, if any."""
    if request.trigger_turn_id is None:
        return None
    return await session.scalar(
        select(Action).where(
            Action.account_id == request.account_id,
            Action.conversation_id == request.conversation_id,
            Action.trigger_turn_id == request.trigger_turn_id,
            or_(Action.state.in_([state.value for state in LIVE_CARD]), warning_card_filter()),
        )
    )


async def _newest_contact_id(session: AsyncSession, request: ProposalRequest) -> int | None:
    return await session.scalar(
        select(func.max(Message.id)).where(
            Message.account_id == request.account_id,
            Message.conversation_id == request.conversation_id,
            Message.direction == "contact",
        )
    )


async def _insert(
    session: AsyncSession,
    request: ProposalRequest,
    facts: ActionFacts,
    decision: Decision,
    *,
    base_message_id: int | None,
    now: datetime,
) -> int | None:
    """Insert as `proposed` (outside the live-card index); None when the same
    `(run_id, tool_call_id)` already exists. Writes no audit row: those come
    from the transitions (decisions R15)."""
    statement = (
        insert(Action)
        .values(
            account_id=request.account_id,
            conversation_id=request.conversation_id,
            kind=request.kind.value,
            origin=request.origin.value,
            state=ActionState.PROPOSED.value,
            version=1,
            authorship=_authorship(request),
            text=request.text,
            base_message_id=base_message_id,
            shown_through_message_id=facts.shown_through_message_id,
            context_fence_id=request.context_fence_id,
            requested_at=facts.requested_at,
            owner_request_id=request.owner_request_id,
            run_id=request.run_id,
            tool_call_id=request.tool_call_id,
            draft_run_id=request.draft_run_id,
            trigger_turn_id=request.trigger_turn_id,
            # Decisions R8: the draft's non-content flags live apart from the
            # decision, which Tx A overwrites.
            draft_flags=None if request.draft_flags is None else dict(request.draft_flags),
            policy_decision=decision_record(decision, Phase.PROPOSAL),
            expires_at=facts.expires_at,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["run_id", "tool_call_id"])
        .returning(Action.id)
    )
    return await session.scalar(statement)


async def _supersede_live(
    session: AsyncSession, request: ProposalRequest, *, new_action_id: int, now: datetime
) -> int | None:
    """Outdate the chat's live card, if any (the index allows at most one)."""
    # A new valid proposal retires any older warning card's approval path.
    # Its stored body remains copy-ready; it cannot approve a newer version.
    retired = await session.execute(
        update(Action)
        .where(
            Action.account_id == request.account_id,
            Action.conversation_id == request.conversation_id,
            Action.state == ActionState.OUTDATED.value,
            or_(
                Action.freshness_warning.is_not(None),
                legacy_warning_card_filter(include_replaced=True),
            ),
        )
        .values(
            state_reason=StateReason.SUPERSEDED.value,
            freshness_warning=None,
            approval_token=None,
            card_state=None,
            updated_at=now,
        )
        .returning(Action.id, Action.version)
    )
    retired_ids: list[int] = []
    for old_id, version in retired:
        retired_ids.append(old_id)
        session.add(
            AuditLogEntry(
                account_id=request.account_id,
                at=now,
                actor="system",
                event="action_superseded",
                subject_type="action",
                subject_id=old_id,
                details={
                    "from": "outdated",
                    "to": "outdated",
                    "version": version,
                    "reason": "superseded",
                    "superseded_by": new_action_id,
                },
            )
        )
    live = (
        await session.execute(
            select(Action.id, Action.state, Action.version)
            .where(
                Action.account_id == request.account_id,
                Action.conversation_id == request.conversation_id,
                Action.state.in_([state.value for state in LIVE_CARD]),
            )
            .with_for_update()
        )
    ).one_or_none()
    if live is None:
        return min(retired_ids, default=None)
    applied = await transition(
        session,
        account_id=request.account_id,
        action_id=live.id,
        from_state=ActionState(live.state),
        to_state=ActionState.OUTDATED,
        reason=StateReason.SUPERSEDED,
        now=now,
        expected_version=live.version,
        details={"superseded_by": new_action_id},
    )
    return live.id if applied else None
