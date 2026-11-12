"""Application-issued source observations and C6 settlement checks.

Observe before loading content in the same short transaction. SHARE locks
serialize reads with ingestion/retention and end before any provider call.
Never turn a model-supplied revision into an observation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account
from assistant.actions.lifecycle import transition, warn_pending
from assistant.actions.models import Action, ContextFenceRecord
from assistant.actions.request_models import TERMINAL_REQUEST_STATES, OwnerRequest
from assistant.actions.requests import OwnerRequestView
from assistant.actions.states import PENDING, ActionState, StateReason
from assistant.conversations.models import Conversation, Message
from assistant.core.errors import NotInAccountError

Id = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
Revision = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
FenceReason = Literal[
    "source_edited",
    "source_deleted",
    "new_contact_message",
    "takeover",
    "owner_intent_superseded",
    "request_changed",
    "source_unavailable",
]


class ConversationFence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: Id
    content_revision: Revision
    owner_intent_revision: Revision
    shown_through_message_id: Id | None
    newest_owner_manual_id: Id | None


# Constructed alongside content reads, never from tool arguments.
ObservedConversationRead = ConversationFence


class ContextFence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: Id | None
    request_version: Id | None
    requested_at: datetime
    conversations: list[ConversationFence]

    @field_validator("requested_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("fence time must be aware UTC")
        return value


@dataclass(frozen=True, slots=True)
class FenceResult:
    reason: FenceReason | None = None

    @property
    def current(self) -> bool:
        return self.reason is None


async def observe_conversations(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_ids: Sequence[int],
) -> tuple[ObservedConversationRead, ...]:
    """Lock sorted chats before associated content reads; caller commits."""
    await session.flush()
    await session.execute(
        select(Account.id).where(Account.id == account_id).with_for_update(read=True)
    )
    rows = list(
        await session.scalars(
            select(Conversation)
            .where(Conversation.account_id == account_id, Conversation.id.in_(conversation_ids))
            .order_by(Conversation.id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
    )
    if len(rows) != len(set(conversation_ids)):
        raise NotInAccountError("conversation not found")
    observed: list[ObservedConversationRead] = []
    for row in rows:
        through, manual = (
            await session.execute(
                select(
                    func.max(Message.id),
                    func.max(Message.id).filter(Message.direction == "owner_manual"),
                ).where(Message.account_id == account_id, Message.conversation_id == row.id)
            )
        ).one()
        observed.append(
            ObservedConversationRead(
                conversation_id=row.id,
                content_revision=row.content_revision,
                owner_intent_revision=row.owner_intent_revision,
                shown_through_message_id=through,
                newest_owner_manual_id=manual,
            )
        )
    return tuple(observed)


async def capture_fence(
    session: AsyncSession,
    *,
    account_id: int,
    run_id: int,
    request: OwnerRequestView | None,
    reads: tuple[ObservedConversationRead, ...],
    requested_at: datetime,
) -> int:
    """Persist observed facts, never a revision reread after generation.

    Different revisions of the same chat remain contradictory. Settlement
    cannot certify old text with a newer revision. No content is stored.
    """
    run = await session.scalar(
        text(
            "SELECT id FROM app.agent_runs WHERE account_id = :account_id AND id = :run_id "
            "AND NOT sources_invalidated"
        ),
        {"account_id": account_id, "run_id": run_id},
    )
    if run is None:
        raise NotInAccountError("run not found")
    combined: list[ConversationFence] = []
    previous = await session.scalars(
        select(ContextFenceRecord)
        .where(ContextFenceRecord.account_id == account_id, ContextFenceRecord.run_id == run_id)
        .order_by(ContextFenceRecord.id)
    )
    for record in previous:
        old = ContextFence.model_validate(record.facts)
        combined.extend(old.conversations)
        requested_at = min(requested_at, old.requested_at)
    combined.extend(reads)
    for observation in combined:
        owned = await session.scalar(
            select(Conversation.id).where(
                Conversation.account_id == account_id,
                Conversation.id == observation.conversation_id,
            )
        )
        if owned is None:
            raise NotInAccountError("conversation not found")
    facts = ContextFence(
        request_id=None if request is None else request.request_id,
        request_version=None if request is None else request.version,
        requested_at=requested_at if request is None else request.requested_at,
        conversations=list(dict.fromkeys(combined)),
    )
    if request is not None:
        owned_request = await session.scalar(
            select(OwnerRequest.id).where(
                OwnerRequest.account_id == account_id, OwnerRequest.id == request.request_id
            )
        )
        if owned_request is None:
            raise NotInAccountError("request not found")
    record = ContextFenceRecord(
        account_id=account_id,
        run_id=run_id,
        facts=facts.model_dump(mode="json"),
        created_at=requested_at,
    )
    session.add(record)
    await session.flush()
    return record.id


async def fence_conversation_ids(
    session: AsyncSession,
    *,
    account_id: int,
    fence_id: int | None,
) -> tuple[int, ...]:
    if fence_id is None:
        return ()
    record = await session.scalar(
        select(ContextFenceRecord).where(
            ContextFenceRecord.account_id == account_id, ContextFenceRecord.id == fence_id
        )
    )
    if record is None:
        return ()
    try:
        facts = ContextFence.model_validate(record.facts)
    except ValidationError:
        return ()
    return tuple(sorted({item.conversation_id for item in facts.conversations}))


async def lock_fence_conversations(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    fence_id: int | None,
    read: bool = False,
    require_recipient: bool = True,
) -> None:
    """After the account lock, before the request/action locks."""
    dependencies = await fence_conversation_ids(session, account_id=account_id, fence_id=fence_id)
    ids = sorted({conversation_id, *dependencies})
    rows = list(
        await session.scalars(
            select(Conversation.id)
            .where(Conversation.account_id == account_id, Conversation.id.in_(ids))
            .order_by(Conversation.id)
            .with_for_update(read=read)
        )
    )
    if require_recipient and conversation_id not in rows:
        raise NotInAccountError("conversation not found")


async def check_fence(
    session: AsyncSession,
    *,
    account_id: int,
    fence_id: int,
    request_id: int | None,
    expected_request_version: int | None,
    run_id: int | None = None,
    required_conversation_id: int | None = None,
) -> FenceResult:
    """Reload facts under caller's sorted conversation and request locks."""
    await session.flush()
    record = await session.scalar(
        select(ContextFenceRecord).where(
            ContextFenceRecord.account_id == account_id, ContextFenceRecord.id == fence_id
        )
    )
    if record is None or record.run_id is None or (run_id is not None and record.run_id != run_id):
        return FenceResult("source_unavailable")
    usable_run = await session.scalar(
        text(
            "SELECT id FROM app.agent_runs WHERE account_id = :account_id AND id = :run_id "
            "AND NOT sources_invalidated"
        ),
        {"account_id": account_id, "run_id": record.run_id},
    )
    if usable_run is None:
        return FenceResult("source_unavailable")
    try:
        facts = ContextFence.model_validate(record.facts)
    except ValidationError:
        return FenceResult("source_unavailable")
    if required_conversation_id is not None and required_conversation_id not in {
        item.conversation_id for item in facts.conversations
    }:
        return FenceResult("source_unavailable")
    if facts.request_id != request_id or facts.request_version != expected_request_version:
        return FenceResult("request_changed")
    if request_id is not None:
        request = await session.scalar(
            select(OwnerRequest)
            .where(OwnerRequest.account_id == account_id, OwnerRequest.id == request_id)
            .execution_options(populate_existing=True)
        )
        if (
            request is None
            or request.version != expected_request_version
            or request.state in TERMINAL_REQUEST_STATES
        ):
            return FenceResult("request_changed")
    for observed in facts.conversations:
        row = (
            await session.execute(
                select(Conversation.content_revision, Conversation.owner_intent_revision).where(
                    Conversation.account_id == account_id,
                    Conversation.id == observed.conversation_id,
                )
            )
        ).one_or_none()
        if row is None:
            return FenceResult("source_unavailable")
        newest_contact, newest_manual, manual_at = (
            await session.execute(
                select(
                    func.max(Message.id).filter(Message.direction == "contact"),
                    func.max(Message.id).filter(Message.direction == "owner_manual"),
                    func.max(Message.sent_at).filter(Message.direction == "owner_manual"),
                ).where(
                    Message.account_id == account_id,
                    Message.conversation_id == observed.conversation_id,
                )
            )
        ).one()
        if (
            newest_manual is not None and newest_manual > (observed.newest_owner_manual_id or 0)
        ) or (manual_at is not None and manual_at > facts.requested_at):
            return FenceResult("takeover")
        if newest_contact is not None and newest_contact > (observed.shown_through_message_id or 0):
            return FenceResult("new_contact_message")
        if row.owner_intent_revision != observed.owner_intent_revision:
            return FenceResult("owner_intent_superseded")
        if row.content_revision != observed.content_revision:
            lost = await session.scalar(
                select(Message.id)
                .where(
                    Message.account_id == account_id,
                    Message.conversation_id == observed.conversation_id,
                    (Message.deleted_at.is_not(None)) | (Message.purged_at.is_not(None)),
                    func.coalesce(Message.deleted_at, Message.purged_at) >= record.created_at,
                )
                .limit(1)
            )
            return FenceResult("source_deleted" if lost is not None else "source_edited")
    return FenceResult()


async def invalidate_fences(
    session: AsyncSession,
    *,
    account_id: int,
    conversation_id: int,
    reason: StateReason,
    now: datetime,
    clear: bool = False,
    include_approved: bool = True,
) -> list[int]:
    """Invalidate dependencies even when the output recipient is another chat.

    The changed conversation is already locked. Never reset a committed claim.
    Scope deletion clears derived text and removes structural references.
    """
    fences = select(ContextFenceRecord.id).where(
        ContextFenceRecord.account_id == account_id,
        ContextFenceRecord.facts["conversations"].contains([{"conversation_id": conversation_id}]),
    )
    states = ["awaiting_approval", "revising", "outdated"]
    if include_approved:
        states.append("approved")
    rows = (
        await session.execute(
            select(Action.id, Action.state, Action.version, Action.origin)
            .where(
                Action.account_id == account_id,
                Action.context_fence_id.in_(fences),
                Action.state.in_(states),
            )
            .order_by(Action.id)
        )
    ).all()
    if not clear:
        warned = await warn_pending(
            session,
            account_id=account_id,
            action_ids=[row[0] for row in rows],
            reason=reason.value,
            now=now,
        )
    else:
        warned = []
    changed: list[int] = []
    for action_id, state, version, origin in rows:
        if state == "outdated" or (not clear and origin != "delegation_auto"):
            continue
        if await transition(
            session,
            account_id=account_id,
            action_id=action_id,
            from_state=ActionState(state),
            to_state=ActionState.OUTDATED,
            reason=reason,
            now=now,
            expected_version=version,
        ):
            changed.append(action_id)
    if clear:
        await session.execute(
            update(Action)
            .where(Action.account_id == account_id, Action.context_fence_id.in_(fences))
            .values(text=None, context_fence_id=None)
        )
        await session.execute(
            delete(ContextFenceRecord).where(
                ContextFenceRecord.account_id == account_id, ContextFenceRecord.id.in_(fences)
            )
        )
    return sorted({*warned, *changed})


async def sweep_fences(session: AsyncSession, *, account_id: int, now: datetime) -> int:
    """One-year metadata ceiling; pending references never retain source text."""
    removed = await session.scalars(
        delete(ContextFenceRecord)
        .where(
            ContextFenceRecord.account_id == account_id,
            ContextFenceRecord.created_at < now - timedelta(days=365),
            ContextFenceRecord.id.not_in(
                select(Action.context_fence_id).where(
                    Action.account_id == account_id,
                    Action.context_fence_id.is_not(None),
                    Action.state.in_(PENDING),
                )
            ),
        )
        .returning(ContextFenceRecord.id)
    )
    return len(list(removed))
