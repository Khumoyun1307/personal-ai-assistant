"""Build the policy engine's input from the database (S04.1 §3.4; ARCHITECTURE §9.2).

`decide` is pure; everything it knows comes from here. The caller holds the
§9.4 locks (account `FOR SHARE`, the conversation, the action `FOR UPDATE`
when it exists), so these reads see the state the decision will act on.
Every read is account-scoped; another account's conversation is
`NotInAccountError` (invariant 12).

Freshness compares Telegram dates only with Telegram dates (the owner's
newest manual message against the base message, or `created_at` for owner
requests) and ingestion-order ids only with ingestion-order ids.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.connections import Tier, capability_tier, connection_for_account
from assistant.accounts.models import Account
from assistant.accounts.service import CONSENT_AI_MANAGED, active_consent
from assistant.actions.config import ActionsConfig
from assistant.actions.states import ActionKind
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.errors import NotInAccountError
from assistant.core.model_routes import processor_for_model
from assistant.db.models import SystemState
from assistant.inbox.models import InboundUpdate
from assistant.policy.context import (
    AccountSnapshot,
    ActionFacts,
    CheckResult,
    ConnectionSnapshot,
    ConversationSnapshot,
    Decision,
    Freshness,
    IngestionState,
    Outcome,
    Phase,
    PolicyContext,
)

#: `system_state` key of the ingestion barrier (decisions R1). Value:
#: `{"process_run_id": str, "caught_up_at": iso | null}`. F0's
#: `bootstrap.running_process` resets it for each run; S04.4's poller sets
#: `caught_up_at`.
KEY_INGESTION_BARRIER = "ingestion_barrier"

_REPLY_TIERS = frozenset({Tier.CAN_REPLY, Tier.EXTENDED})
_TARGETED_KINDS = frozenset({ActionKind.EDIT_MESSAGE, ActionKind.DELETE_OWN})


def decision_record(decision: Decision, phase: Phase) -> dict[str, Any]:
    """`actions.policy_decision`: the whole decision, as codes (no content)."""
    return {
        "phase": phase.value,
        "outcome": decision.outcome.value,
        "checks": [
            {"name": check.name, "passed": check.passed, "reason": check.reason}
            for check in decision.checks
        ],
    }


def decision_from_record(record: dict[str, Any] | None) -> Decision:
    """The stored decision back as a `Decision` (a reused proposal's result)."""
    if record is None:
        return Decision(Outcome.DENY, ())
    return Decision(
        Outcome(record["outcome"]),
        tuple(
            CheckResult(check["name"], bool(check["passed"]), check["reason"])
            for check in record.get("checks", ())
        ),
    )


async def ingestion_state(session: AsyncSession, *, process_run_id: str | None) -> IngestionState:
    """Whether the inbox has caught up, for this process run only.

    `pending_inbound` counts every account's pending rows: a pending row has
    no account yet (ARCHITECTURE §9.3). The barrier passes only when it was
    set by this very process run after its poller caught up; a missing key,
    another run's key or no run id at all fail closed.
    """
    pending = await session.scalar(
        select(func.count()).select_from(InboundUpdate).where(InboundUpdate.status == "pending")
    )
    value = await session.scalar(
        select(SystemState.value).where(SystemState.key == KEY_INGESTION_BARRIER)
    )
    passed = (
        process_run_id is not None
        and isinstance(value, dict)
        and value.get("process_run_id") == process_run_id
        and value.get("caught_up_at") is not None
    )
    return IngestionState(pending_inbound=int(pending or 0), barrier_passed=passed)


async def _account(
    session: AsyncSession, account_id: int, config: ActionsConfig
) -> AccountSnapshot:
    row = (
        await session.execute(
            select(Account.status, Account.autonomy_paused).where(Account.id == account_id)
        )
    ).one_or_none()
    if row is None:
        raise NotInAccountError("account not found")
    consent = await active_consent(session, account_id, kind=CONSENT_AI_MANAGED)
    # The draft route's processor must be one the owner agreed to (§14.3).
    processor = processor_for_model(config.draft_route_model)
    consent_ok = consent.granted and processor is not None and processor in consent.processors
    return AccountSnapshot(
        account_id=account_id,
        status=row.status,
        autonomy_paused=row.autonomy_paused,
        managed_consent_ok=consent_ok,
    )


async def _connection(session: AsyncSession, account_id: int) -> ConnectionSnapshot:
    connection = await connection_for_account(session, account_id)
    if connection is None:
        return ConnectionSnapshot(exists=False, status=None, is_enabled=False, can_reply=False)
    tier = capability_tier(connection.is_enabled, frozenset(connection.rights or ()))
    return ConnectionSnapshot(
        exists=True,
        status=connection.status,
        is_enabled=connection.is_enabled,
        can_reply=tier in _REPLY_TIERS,
    )


async def _conversation(
    session: AsyncSession, account_id: int, conversation_id: int
) -> ConversationSnapshot:
    row = (
        await session.execute(
            select(Conversation, Contact.is_bot)
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(Conversation.id == conversation_id, Conversation.account_id == account_id)
        )
    ).one_or_none()
    if row is None:
        raise NotInAccountError("conversation not found")
    conversation, is_bot = row
    return ConversationSnapshot(
        conversation_id=conversation.id,
        account_id=conversation.account_id,
        mode=conversation.mode,
        availability=conversation.availability,
        is_bot=is_bot,
        last_contact_msg_at=conversation.last_contact_msg_at,
        last_owner_msg_at=conversation.last_owner_msg_at,
        autonomy_paused_reason=conversation.autonomy_paused_reason,
    )


def _in_conversation(account_id: int, conversation_id: int) -> tuple[ColumnElement[bool], ...]:
    return (Message.account_id == account_id, Message.conversation_id == conversation_id)


async def _freshness(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    kind: ActionKind,
    base_message_id: int | None,
    target_message_id: int | None,
    owner_requested: bool,
) -> Freshness:
    scope = _in_conversation(account_id, conversation_id)
    # Tombstones included: an owner message the owner later deleted still
    # means the owner acted; a deleted contact message was still seen.
    newest_owner = await session.scalar(
        select(func.max(Message.sent_at)).where(*scope, Message.direction == "owner_manual")
    )
    newest_contact = await session.scalar(
        select(func.max(Message.id)).where(*scope, Message.direction == "contact")
    )
    base_present = True
    base_sent_at: datetime | None = None
    if base_message_id is not None:
        base = (
            await session.execute(
                select(Message.sent_at, Message.deleted_at, Message.purged_at).where(
                    *scope, Message.id == base_message_id
                )
            )
        ).one_or_none()
        base_present = base is not None and base.deleted_at is None and base.purged_at is None
        if base is not None and not owner_requested:
            # Decisions R3: only a proactive draft is measured against its base
            # message; an owner request is newer than the owner's own messages.
            base_sent_at = base.sent_at
    target_is_assistant_sent: bool | None = None
    if kind in _TARGETED_KINDS:
        target = (
            await session.execute(
                select(Message.direction, Message.deleted_at).where(
                    *scope, Message.id == target_message_id
                )
            )
        ).one_or_none()
        target_is_assistant_sent = (
            target is not None
            and target.direction == "assistant_sent"
            and target.deleted_at is None
        )
    return Freshness(
        newest_owner_manual_sent_at=newest_owner,
        newest_contact_message_id=newest_contact,
        base_message_sent_at=base_sent_at,
        base_message_present=base_present,
        target_is_assistant_sent=target_is_assistant_sent,
    )


async def load_context(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    facts: ActionFacts,
    base_message_id: int | None,
    target_message_id: int | None,
    owner_requested: bool,
    phase: Phase,
    now: datetime,
    config: ActionsConfig,
    ai_generated: bool,
    process_run_id: str | None,
) -> PolicyContext:
    """The `PolicyContext` for one action, read under the caller's locks."""
    conversation = await _conversation(session, account_id, conversation_id)
    return PolicyContext(
        now=now,
        phase=phase,
        account=await _account(session, account_id, config),
        connection=await _connection(session, account_id),
        conversation=conversation,
        freshness=await _freshness(
            session,
            account_id=account_id,
            conversation_id=conversation_id,
            kind=facts.kind,
            base_message_id=base_message_id,
            target_message_id=target_message_id,
            owner_requested=owner_requested,
        ),
        ingestion=await ingestion_state(session, process_run_id=process_run_id),
        action=facts,
        ai_generated=ai_generated,
        sending_enabled=config.sending_enabled,
    )
