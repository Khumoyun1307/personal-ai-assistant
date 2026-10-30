"""Every owner-voice reply starts here (S04.1 §3.5; ARCHITECTURE §8.3, §9.4).

`propose` turns a draft or a dictated text into an action row and decides,
with the proposal-phase policy, whether it becomes an approval card
(`awaiting_approval`) or is `blocked` with copy-ready text. It never sends,
never presents a card and never defers a job: callers do that after commit.

Locks follow the global order (account `FOR SHARE`, then the conversation
`FOR UPDATE`), so concurrent proposals for one chat serialize and the chat
keeps at most one live card.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.callbacks import new_token
from assistant.accounts.models import Account
from assistant.actions.config import ActionsConfig
from assistant.actions.lifecycle import sendable_length, transition
from assistant.actions.models import Action
from assistant.actions.policy_loader import decision_from_record, decision_record, load_context
from assistant.actions.states import LIVE_CARD, ActionKind, ActionOrigin, ActionState, StateReason
from assistant.conversations.models import Conversation, Message
from assistant.core.clock import Clock
from assistant.core.errors import NotInAccountError
from assistant.policy.context import ActionFacts, Decision, Outcome, Phase
from assistant.policy.engine import decide, execution_target


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
    await _lock_conversation_update(session, request.account_id, request.conversation_id)
    # A retried owner turn reuses its live card instead of superseding it.
    if (live := await _live_for_turn(session, request)) is not None:
        return _reused(live)

    shown_through = request.shown_through_message_id
    # Only the owner's own words are fresh through "now". An AI draft is fresh
    # through what it saw; without that id `not_stale` fails closed.
    if shown_through is None and request.origin is ActionOrigin.OWNER_DICTATED:
        shown_through = await _newest_contact_id(session, request)
    expires_at = now + config.approval_max_age  # owner-approved: max age only (P2-O20)
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
    )
    # Decisions R3: an owner request is measured from its own creation, and a
    # dictated message has no base message.
    owner_requested = (
        request.origin is ActionOrigin.OWNER_DICTATED or request.trigger_turn_id is not None
    )
    base_message_id = (
        None if request.origin is ActionOrigin.OWNER_DICTATED else request.base_message_id
    )
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
        ai_generated=request.origin is ActionOrigin.AGENT_PROPOSAL,
        process_run_id=None,
    )
    decision = decide(context)

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
        superseded = await _supersede_live(session, request, new_action_id=action_id, now=now)
        await transition(
            session,
            account_id=request.account_id,
            action_id=action_id,
            from_state=ActionState.PROPOSED,
            to_state=ActionState.AWAITING_APPROVAL,
            reason=None,
            now=now,
            values={"approval_token": new_token()},
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
        details={"failed_rules": failed},
    )
    return ProposalResult(action_id, ActionState.BLOCKED, False, None, decision)


def _blocked_reason(decision: Decision) -> StateReason:
    target = execution_target(decision)
    return StateReason.POLICY_DENIED if target is None else target[1]


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
            Action.state.in_([state.value for state in LIVE_CARD]),
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
            text=request.text,
            base_message_id=base_message_id,
            shown_through_message_id=facts.shown_through_message_id,
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
        return None
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
