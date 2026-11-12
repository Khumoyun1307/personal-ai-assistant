"""Inert durable request bookkeeping (C1). Callers own the transaction/time.

No tools, provider calls or outbound effects live here. H2 owns ingress
routing; H10/H11 own revision fences and owner-intent tokens.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, AuditLogEntry, ControlTurn, PendingInput
from assistant.actions.lifecycle import is_warning_card, transition
from assistant.actions.models import Action
from assistant.actions.request_models import (
    TERMINAL_REQUEST_STATES,
    OwnerRequest,
    OwnerRequestSource,
)
from assistant.actions.states import PENDING, TERMINAL, ActionState, StateReason
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.errors import NotInAccountError

Operation = Literal["dictate", "compose", "reply", "revise"]
RequestState = Literal[
    "collecting",
    "ready",
    "preparing",
    "awaiting_approval",
    "suspended",
    "completed",
    "cancelled",
    "superseded",
    "expired",
]
MissingSlot = Literal["recipient", "content", "target", "constraints"]
ContinuationCode = Literal["not_available", "stale", "source_unavailable", "managed_access_off"]
RequestReason = Literal[
    "owner_cancelled",
    "topic_switch",
    "source_unavailable",
    "recipient_changed",
    "superseded",
    "max_age",
    "action_terminal",
    "restored_from_backup",
]
PositiveId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
REQUEST_TTL = timedelta(hours=24)
SUGGESTION_SELECTED = "owner_request_suggestion_selected"


def _check_time(now: datetime) -> None:
    if now.utcoffset() != timedelta(0):
        raise ValueError("request time must be aware UTC")


class StrictValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceSpan(StrictValue):
    turn_id: PositiveId
    field: Literal["text", "quoted_text"]
    start: Annotated[int, Field(strict=True, ge=0)]
    end: Annotated[int, Field(strict=True, gt=0)]
    field_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @model_validator(mode="after")
    def ordered(self) -> SourceSpan:
        if self.end <= self.start:
            raise ValueError("invalid source bounds")
        return self


class RequestSource(StrictValue):
    span: SourceSpan
    purpose: Literal["instruction", "literal_body", "constraint", "quote", "suggestion"]

    @model_validator(mode="after")
    def quote_only(self) -> RequestSource:
        if self.span.field == "quoted_text" and self.purpose != "quote":
            raise ValueError("quoted field is never an instruction")
        return self


class RequestConstraints(StrictValue):
    min_words: Annotated[int, Field(strict=True, ge=1, le=1000)] | None
    max_words: Annotated[int, Field(strict=True, ge=1, le=1000)] | None
    reply_language: Literal["uz", "uz-Latn", "uz-Cyrl", "ru", "en"] | None

    @model_validator(mode="after")
    def ordered(self) -> RequestConstraints:
        if (
            self.min_words is not None
            and self.max_words is not None
            and self.min_words > self.max_words
        ):
            raise ValueError("invalid word bounds")
        return self


class AddSources(StrictValue):
    kind: Literal["add_sources"] = "add_sources"
    sources: Annotated[tuple[RequestSource, ...], Field(min_length=1, max_length=64)]


class SelectRecipient(StrictValue):
    kind: Literal["select_recipient"] = "select_recipient"
    contact_id: PositiveId
    conversation_id: PositiveId
    basis: Literal["unique_strong", "owner_choice", "explicit_reply"]


class CandidatePair(StrictValue):
    contact_id: PositiveId
    conversation_id: PositiveId | None


class ResolutionCandidates(StrictValue):
    request_version: PositiveId
    candidates: Annotated[tuple[CandidatePair, ...], Field(min_length=1, max_length=10)]


class RecordCandidates(StrictValue):
    kind: Literal["record_candidates"] = "record_candidates"
    candidates: Annotated[tuple[CandidatePair, ...], Field(max_length=10)]
    turn_id: PositiveId | None = None


class SelectSuggestion(StrictValue):
    """Bind a classified owner selection to exact retained artifact sources."""

    kind: Literal["select_suggestion"] = "select_suggestion"
    instruction_source_id: PositiveId
    source_ids: Annotated[tuple[PositiveId, ...], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def unique_sources(self) -> SelectSuggestion:
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("duplicate suggestion sources")
        return self


class SetQuestion(StrictValue):
    kind: Literal["set_question"] = "set_question"
    slot: MissingSlot
    input_id: PositiveId


class SetTarget(StrictValue):
    kind: Literal["set_target"] = "set_target"
    message_id: PositiveId


class SetConstraints(StrictValue):
    kind: Literal["set_constraints"] = "set_constraints"
    constraints: RequestConstraints


class LinkAction(StrictValue):
    kind: Literal["link_action"] = "link_action"
    action_id: PositiveId
    version: Annotated[int, Field(strict=True, ge=1)]


class SetState(StrictValue):
    kind: Literal["set_state"] = "set_state"
    state: RequestState
    reason: RequestReason | None


RequestUpdate = Annotated[
    AddSources
    | SelectRecipient
    | RecordCandidates
    | SelectSuggestion
    | SetQuestion
    | SetTarget
    | SetConstraints
    | LinkAction
    | SetState,
    Field(discriminator="kind"),
]


@dataclass(frozen=True)
class OwnerRequestView:
    request_id: int
    version: int
    operation: Operation
    state: RequestState
    contact_id: int | None
    conversation_id: int | None
    missing_slot: MissingSlot | None
    action_id: int | None
    action_version: int | None
    target_message_id: int | None
    sources: tuple[RequestSource, ...]
    requested_at: datetime
    expires_at: datetime
    source_ids: tuple[int, ...] = ()
    selection_basis: str | None = None


@dataclass(frozen=True)
class ValidatedSource:
    text: str
    managed: bool
    untrusted: bool
    turn_id: int
    status: Literal["validated"] = "validated"
    conversation_ids: tuple[int, ...] = ()
    contact_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SourceUnavailable:
    code: Literal["source_unavailable"] = "source_unavailable"
    status: Literal["unavailable"] = "unavailable"


@dataclass(frozen=True)
class RequestApplied:
    view: OwnerRequestView
    status: Literal["applied"] = "applied"


@dataclass(frozen=True)
class RequestStale:
    current_version: int
    status: Literal["stale"] = "stale"


@dataclass(frozen=True)
class RequestUnavailable:
    code: Literal[
        "not_available",
        "source_unavailable",
        "invalid_state",
        "recipient_changed",
        "invalid_reference",
        "too_many_sources",
        "action_changed",
    ]
    status: Literal["unavailable"] = "unavailable"


RequestUpdateResult = RequestApplied | RequestStale | RequestUnavailable


@dataclass(frozen=True)
class Continuation:
    status: Literal["selected", "ambiguous", "none", "unavailable"]
    request_id: int | None = None
    version: int | None = None
    action_id: int | None = None
    action_version: int | None = None
    request_ids: tuple[int, ...] = ()
    code: ContinuationCode | None = None
    basis: Literal["explicit_reply", "pending_question", "focused_request"] | None = None


@dataclass(frozen=True, slots=True)
class OwnerContinuation:
    """Fresh request facts and separately validated text, in memory only."""

    selection: Continuation
    request: OwnerRequestView | None = None
    sources: tuple[ValidatedSource, ...] = ()
    question_input_id: int | None = None

    def as_refs(self) -> dict[str, list[int]]:
        """Structural linkage only; usable on owner and assistant turns."""
        refs: dict[str, list[int]] = {}
        if self.selection.status == "selected":
            if self.selection.request_id is not None:
                refs["owner_request_ids"] = [self.selection.request_id]
            if self.selection.action_id is not None and self.selection.action_version is not None:
                refs["continuation_action_ids"] = [self.selection.action_id]
                refs["continuation_action_versions"] = [self.selection.action_version]
        return refs


async def lock_requests(session: AsyncSession, *, account_id: int) -> bool:
    """Account NO KEY UPDATE first; sorted conversations before any request.

    The foundation deliberately serializes request mutations per account.
    No account SHARE lock may be upgraded while holding a conversation.
    """
    account = await session.scalar(
        select(Account.id).where(Account.id == account_id).with_for_update(key_share=True)
    )
    if account is None:
        return False
    await session.execute(
        select(Conversation.id)
        .where(Conversation.account_id == account_id)
        .order_by(Conversation.id)
        .with_for_update()
    )
    return True


async def load_source(
    session: AsyncSession, *, account_id: int, source: RequestSource
) -> ValidatedSource | SourceUnavailable:
    turn = await session.scalar(
        select(ControlTurn)
        .where(ControlTurn.account_id == account_id, ControlTurn.id == source.span.turn_id)
        .execution_options(populate_existing=True)
    )
    if turn is None:
        return SourceUnavailable()
    field = turn.text if source.span.field == "text" else turn.quoted_text
    text = copy_source_text(field, source)
    if text is None:
        return SourceUnavailable()
    if turn.role == "assistant":
        if source.purpose != "suggestion" or source.span.field != "text":
            return SourceUnavailable()
    elif turn.role != "owner":
        return SourceUnavailable()
    elif source.span.field == "text":
        if source.purpose == "suggestion":
            return SourceUnavailable()
        # Only ingress-classified outer text can be an owner source beside
        # a quote. Legacy mixed turns and forwards remain untrusted.
        if (
            turn.third_party
            and source.purpose != "quote"
            and not (turn.owner_text_verified and turn.quoted_text is not None)
        ):
            return SourceUnavailable()
    return ValidatedSource(
        text=text,
        managed=bool(
            turn.contains_managed
            and not (
                turn.role == "owner" and turn.owner_text_verified and source.span.field == "text"
            )
        ),
        untrusted=(
            turn.role == "assistant"
            or (turn.third_party and not (turn.owner_text_verified and source.span.field == "text"))
            or turn.from_tainted_run
            or source.span.field == "quoted_text"
            or source.purpose == "quote"
        ),
        turn_id=turn.id,
        conversation_ids=_source_ref_ids(turn, "conversation_ids"),
        contact_ids=_source_ref_ids(turn, "contact_ids"),
    )


def _source_ref_ids(turn: ControlTurn, key: str) -> tuple[int, ...]:
    """Deletion links from the scoped source, never from source prose."""
    values = (turn.refs or {}).get(key)
    if not isinstance(values, list):
        return ()
    return tuple(value for value in values if type(value) is int and 0 < value <= 2**63 - 1)


def copy_source_text(field: str | None, source: RequestSource) -> str | None:
    """Validate the whole retained field before copying Unicode code points.

    Hashes detect edits; role/purpose/account/request checks remain required.
    No normalization or invented whitespace is permitted (D-S2, H3).
    """
    span = source.span
    if field is None or not 0 <= span.start < span.end <= len(field):
        return None
    if hashlib.sha256(field.encode("utf-8")).hexdigest() != span.field_sha256:
        return None
    return field[span.start : span.end]


async def _source_rows(
    session: AsyncSession, *, account_id: int, request_id: int
) -> tuple[OwnerRequestSource, ...]:
    rows = await session.scalars(
        select(OwnerRequestSource)
        .where(
            OwnerRequestSource.account_id == account_id,
            OwnerRequestSource.request_id == request_id,
        )
        .order_by(OwnerRequestSource.ordinal)
    )
    return tuple(rows)


def _source_value(row: OwnerRequestSource) -> RequestSource:
    return RequestSource.model_validate(
        {
            "span": {
                "turn_id": row.turn_id,
                "field": row.field,
                "start": row.start_char,
                "end": row.end_char,
                "field_sha256": row.field_sha256,
            },
            "purpose": row.purpose,
        }
    )


async def _sources(
    session: AsyncSession, *, account_id: int, request_id: int
) -> tuple[RequestSource, ...]:
    return tuple(
        _source_value(row)
        for row in await _source_rows(session, account_id=account_id, request_id=request_id)
    )


async def _view(session: AsyncSession, row: OwnerRequest) -> OwnerRequestView:
    sources = await _source_rows(session, account_id=row.account_id, request_id=row.id)
    return OwnerRequestView(
        request_id=row.id,
        version=row.version,
        operation=cast(Operation, row.operation),
        state=cast(RequestState, row.state),
        contact_id=row.contact_id,
        conversation_id=row.conversation_id,
        missing_slot=cast(MissingSlot | None, row.missing_slot),
        action_id=row.action_id,
        action_version=row.action_version,
        target_message_id=row.target_message_id,
        sources=tuple(_source_value(source) for source in sources),
        requested_at=row.requested_at,
        expires_at=row.expires_at,
        source_ids=tuple(source.id for source in sources),
        selection_basis=row.selection_basis,
    )


async def load_request(
    session: AsyncSession, *, account_id: int, request_id: int
) -> OwnerRequestView | None:
    row = await session.scalar(
        select(OwnerRequest)
        .where(OwnerRequest.account_id == account_id, OwnerRequest.id == request_id)
        .execution_options(populate_existing=True)
    )
    return None if row is None else await _view(session, row)


def _append_sources(
    session: AsyncSession, row: OwnerRequest, sources: tuple[RequestSource, ...], offset: int
) -> None:
    for ordinal, source in enumerate(sources, start=offset):
        session.add(
            OwnerRequestSource(
                account_id=row.account_id,
                request_id=row.id,
                ordinal=ordinal,
                turn_id=source.span.turn_id,
                field=source.span.field,
                purpose=source.purpose,
                start_char=source.span.start,
                end_char=source.span.end,
                field_sha256=source.span.field_sha256,
            )
        )


def _opening_digest(operation: Operation, sources: tuple[RequestSource, ...]) -> str:
    encoded = json.dumps(
        {"operation": operation, "sources": [source.model_dump(mode="json") for source in sources]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def open_request(
    session: AsyncSession,
    *,
    account_id: int,
    turn_id: int,
    operation: Operation,
    sources: tuple[RequestSource, ...],
    now: datetime,
) -> OwnerRequestView:
    _check_time(now)
    if not await lock_requests(session, account_id=account_id):
        raise NotInAccountError("request unavailable")
    existing = await session.scalar(
        select(OwnerRequest)
        .where(OwnerRequest.account_id == account_id, OwnerRequest.trigger_turn_id == turn_id)
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        opened = await session.scalar(
            select(AuditLogEntry).where(
                AuditLogEntry.account_id == account_id,
                AuditLogEntry.event == "owner_request_opened",
                AuditLogEntry.subject_type == "owner_request",
                AuditLogEntry.subject_id == existing.id,
            )
        )
        if opened is None or opened.details.get("source_digest") != _opening_digest(
            operation, sources
        ):
            raise ValueError("request trigger conflict")
        return await _view(session, existing)
    turn = await session.scalar(
        select(ControlTurn)
        .where(
            ControlTurn.account_id == account_id,
            ControlTurn.id == turn_id,
            ControlTurn.role == "owner",
        )
        .execution_options(populate_existing=True)
    )
    if (
        turn is None
        or turn.text is None
        or (turn.third_party and not (turn.owner_text_verified and turn.quoted_text is not None))
    ):
        raise NotInAccountError("request unavailable")
    if len(sources) > 64:
        raise ValueError("too many sources")
    for source in sources:
        if isinstance(
            await load_source(session, account_id=account_id, source=source), SourceUnavailable
        ):
            raise ValueError("source unavailable")
    # A distinct trigger never overwrites the previous body's refs/recipient.
    await _suspend_other_focus(session, account_id=account_id, request_id=None, now=now)
    row = OwnerRequest(
        account_id=account_id,
        operation=operation,
        state="collecting",
        focused=True,
        trigger_turn_id=turn.id,
        last_turn_id=turn.id,
        missing_slot="recipient",
        constraints={},
        requested_at=turn.created_at,
        updated_at=now,
        expires_at=turn.created_at + REQUEST_TTL,
    )
    session.add(row)
    await session.flush()
    _append_sources(session, row, sources, 0)
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=now,
            actor="owner",
            event="owner_request_opened",
            subject_type="owner_request",
            subject_id=row.id,
            details={"source_digest": _opening_digest(operation, sources)},
        )
    )
    await session.flush()
    return await _view(session, row)


async def cancel_linked_action(
    session: AsyncSession,
    row: OwnerRequest,
    *,
    now: datetime,
    clear_text: bool = False,
    reason: StateReason = StateReason.OWNER_REJECTED,
) -> None:
    if row.action_id is None:
        return
    action = await session.scalar(
        select(Action)
        .where(Action.account_id == row.account_id, Action.id == row.action_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if action is None:
        return
    if action.state in PENDING or is_warning_card(action):
        target = ActionState.CANCELLED
        if reason is StateReason.MAX_AGE and action.state in (
            "awaiting_approval",
            "revising",
            "approved",
            "outdated",
        ):
            target = ActionState.EXPIRED
        await transition(
            session,
            account_id=row.account_id,
            action_id=action.id,
            from_state=ActionState(action.state),
            to_state=target,
            reason=StateReason.DATA_DELETED if clear_text else reason,
            now=now,
            expected_version=action.version,
        )
    if clear_text:
        action.text = None


async def _ready(session: AsyncSession, row: OwnerRequest) -> bool:
    sources = await _sources(session, account_id=row.account_id, request_id=row.id)
    purposes = {source.purpose for source in sources}
    if row.operation == "revise":
        action_id = await session.scalar(
            select(Action.id).where(
                Action.account_id == row.account_id,
                Action.id == row.action_id,
                Action.version == row.action_version,
                Action.state.in_(("awaiting_approval", "revising", "blocked")),
            )
        )
        return action_id is not None and bool(purposes & {"instruction", "literal_body"})
    if row.conversation_id is None:
        return False
    if row.operation == "dictate":
        return "literal_body" in purposes
    if row.operation == "compose":
        return "instruction" in purposes
    target = await session.scalar(
        select(Message.id).where(
            Message.account_id == row.account_id,
            Message.id == row.target_message_id,
            Message.conversation_id == row.conversation_id,
            Message.direction == "contact",
            Message.deleted_at.is_(None),
            Message.text.is_not(None),
        )
    )
    return target is not None


async def update_request(
    session: AsyncSession,
    *,
    account_id: int,
    request_id: int,
    expected_version: int,
    update: RequestUpdate,
    now: datetime,
) -> RequestUpdateResult:
    _check_time(now)
    if not await lock_requests(session, account_id=account_id):
        return RequestUnavailable("not_available")
    row = await session.scalar(
        select(OwnerRequest)
        .where(OwnerRequest.account_id == account_id, OwnerRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        return RequestUnavailable("not_available")
    if row.version != expected_version:
        return RequestStale(row.version)
    if row.state in TERMINAL_REQUEST_STATES:
        return RequestUnavailable("invalid_state")
    ending = isinstance(update, SetState) and update.state in ("cancelled", "superseded", "expired")
    if row.state != "awaiting_approval" and row.expires_at <= now and not ending:
        return RequestUnavailable("not_available")
    unfinished = isinstance(update, SetState) and update.state in (
        "collecting",
        "ready",
        "preparing",
    )
    if unfinished and row.requested_at + REQUEST_TTL <= now:
        return RequestUnavailable("not_available")
    sources = await _sources(session, account_id=account_id, request_id=request_id)
    for source in () if ending else sources:
        if isinstance(
            await load_source(session, account_id=account_id, source=source), SourceUnavailable
        ):
            return RequestUnavailable("source_unavailable")
    if isinstance(update, AddSources):
        if len(sources) + len(update.sources) > 64:
            return RequestUnavailable("too_many_sources")
        for source in update.sources:
            if isinstance(
                await load_source(session, account_id=account_id, source=source), SourceUnavailable
            ):
                return RequestUnavailable("source_unavailable")
        _append_sources(session, row, update.sources, len(sources))
        owner_turns = await session.scalars(
            select(ControlTurn.id).where(
                ControlTurn.account_id == account_id,
                ControlTurn.role == "owner",
                ControlTurn.id.in_([source.span.turn_id for source in update.sources]),
            )
        )
        row.last_turn_id = max([row.last_turn_id, *owner_turns])
    elif isinstance(update, SelectSuggestion):
        view = await _view(session, row)
        by_id = dict(zip(view.source_ids, view.sources, strict=True))
        instruction = by_id.get(update.instruction_source_id)
        if (
            instruction is None
            or instruction.purpose != "instruction"
            or instruction.span.field != "text"
        ):
            return RequestUnavailable("invalid_reference")
        for source_id in update.source_ids:
            suggestion = by_id.get(source_id)
            if (
                suggestion is None
                or suggestion.purpose != "suggestion"
                or suggestion.span.field != "text"
                or suggestion.span.turn_id >= instruction.span.turn_id
            ):
                return RequestUnavailable("invalid_reference")
        # All sources have already been revalidated above. This explicit
        # classification binds the instruction to one ordered source list;
        # retaining a later instruction alone never selects every artifact.
        session.add(
            AuditLogEntry(
                account_id=account_id,
                at=now,
                actor="owner",
                event=SUGGESTION_SELECTED,
                subject_type="owner_request",
                subject_id=row.id,
                details=update.model_dump(mode="json"),
            )
        )
        row.last_turn_id = max(row.last_turn_id, instruction.span.turn_id)
    elif isinstance(update, RecordCandidates):
        if row.contact_id is not None or row.state not in ("collecting", "suspended"):
            return RequestUnavailable("invalid_state")
        if update.turn_id is not None:
            candidate_turn = await session.scalar(
                select(ControlTurn.id).where(
                    ControlTurn.account_id == account_id,
                    ControlTurn.id == update.turn_id,
                    ControlTurn.role == "owner",
                    ControlTurn.id >= row.last_turn_id,
                )
            )
            if candidate_turn is None:
                return RequestUnavailable("invalid_reference")
        for candidate in update.candidates:
            contact = await session.scalar(
                select(Contact.id).where(
                    Contact.account_id == account_id,
                    Contact.id == candidate.contact_id,
                    Contact.is_bot.is_(False),
                )
            )
            if contact is None:
                return RequestUnavailable("invalid_reference")
            if candidate.conversation_id is not None:
                conversation_id = await session.scalar(
                    select(Conversation.id).where(
                        Conversation.account_id == account_id,
                        Conversation.contact_id == candidate.contact_id,
                        Conversation.id == candidate.conversation_id,
                    )
                )
                if conversation_id is None:
                    return RequestUnavailable("invalid_reference")
        row.missing_slot = "recipient"
        if update.turn_id is not None:
            row.last_turn_id = update.turn_id
        row.resolution_candidates = (
            ResolutionCandidates(
                request_version=row.version + 1, candidates=update.candidates
            ).model_dump(mode="json")
            if update.candidates
            else None
        )
    elif isinstance(update, SelectRecipient):
        # Binding records identity; proposal eligibility is checked downstream
        # so ignored and bot chats retain their specific refusal reasons.
        conversation = await session.scalar(
            select(Conversation)
            .join(Contact, Contact.id == Conversation.contact_id)
            .where(
                Conversation.account_id == account_id,
                Contact.account_id == account_id,
                Conversation.id == update.conversation_id,
                Contact.id == update.contact_id,
            )
        )
        if conversation is None:
            return RequestUnavailable("invalid_reference")
        if row.conversation_id is not None and row.conversation_id != conversation.id:
            return RequestUnavailable("recipient_changed")
        if row.action_id is not None:
            action = await session.scalar(
                select(Action)
                .where(Action.account_id == account_id, Action.id == row.action_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if action is None or action.conversation_id != conversation.id:
                return RequestUnavailable("invalid_reference")
        row.contact_id = update.contact_id
        row.conversation_id = update.conversation_id
        row.selection_basis = row.selection_basis or update.basis
        row.resolution_candidates = None
        if row.state in ("collecting", "suspended"):
            row.missing_slot = "content"
    elif isinstance(update, SetQuestion):
        question = await session.scalar(
            select(PendingInput).where(
                PendingInput.account_id == account_id,
                PendingInput.id == update.input_id,
                PendingInput.kind == "owner_request",
                PendingInput.ref_id == row.id,
                PendingInput.expires_at > now,
            )
        )
        if question is None or row.state not in ("collecting", "suspended"):
            return RequestUnavailable("invalid_reference")
        row.question_input_id = question.id
        row.missing_slot = update.slot
    elif isinstance(update, SetTarget):
        target = await session.scalar(
            select(Message).where(
                Message.account_id == account_id,
                Message.id == update.message_id,
                Message.conversation_id == row.conversation_id,
                Message.direction == "contact",
                Message.text.is_not(None),
                Message.deleted_at.is_(None),
            )
        )
        if target is None:
            return RequestUnavailable("invalid_reference")
        row.target_message_id = target.id
    elif isinstance(update, SetConstraints):
        row.constraints = update.constraints.model_dump(mode="json")
    elif isinstance(update, LinkAction):
        action = await session.scalar(
            select(Action)
            .join(Conversation, Conversation.id == Action.conversation_id)
            .where(
                Action.account_id == account_id,
                Action.id == update.action_id,
                Action.version == update.version,
                Conversation.account_id == account_id,
            )
            .with_for_update(of=Action)
            .execution_options(populate_existing=True)
        )
        if action is None or action.state not in ("awaiting_approval", "revising", "blocked"):
            return RequestUnavailable("action_changed")
        if row.conversation_id is not None and action.conversation_id != row.conversation_id:
            return RequestUnavailable("invalid_reference")
        if row.action_id is not None and row.action_id != action.id:
            return RequestUnavailable("action_changed")
        row.action_id = action.id
        row.action_version = action.version
    else:
        if update.state in ("ready", "preparing") and not await _ready(session, row):
            return RequestUnavailable("invalid_state")
        if update.state in ("awaiting_approval", "completed"):
            action = await session.scalar(
                select(Action)
                .where(
                    Action.account_id == account_id,
                    Action.id == row.action_id,
                    Action.version == row.action_version,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            required = ("succeeded",) if update.state == "completed" else ("awaiting_approval",)
            if action is None or action.state not in required:
                return RequestUnavailable("action_changed")
            if update.state == "awaiting_approval":
                if action.expires_at is None or action.expires_at <= now:
                    return RequestUnavailable("action_changed")
                row.expires_at = action.expires_at
        if update.state in ("cancelled", "superseded", "expired"):
            await cancel_linked_action(session, row, now=now)
        row.state = update.state
        row.reason = update.reason
        if unfinished:
            row.expires_at = row.requested_at + REQUEST_TTL
        if row.state not in ("collecting", "suspended"):
            row.missing_slot = None
        if row.state in TERMINAL_REQUEST_STATES:
            row.focused = False
            row.ended_at = now
        elif row.state == "suspended":
            row.focused = False
            row.expires_at = row.requested_at + REQUEST_TTL
        elif row.state in ("collecting", "ready"):
            await _suspend_other_focus(session, account_id=account_id, request_id=row.id, now=now)
            row.focused = True
        if row.state in (*TERMINAL_REQUEST_STATES, "suspended"):
            await _clear_question(session, row)
    row.version += 1
    if row.resolution_candidates is not None:
        if row.missing_slot != "recipient" or row.state not in ("collecting", "suspended"):
            row.resolution_candidates = None
        else:
            row.resolution_candidates = {
                **row.resolution_candidates,
                "request_version": row.version,
            }
    row.updated_at = now
    await session.flush()
    return RequestApplied(await _view(session, row))


async def _clear_question(session: AsyncSession, row: OwnerRequest) -> None:
    row.resolution_candidates = None
    row.question_input_id = None
    await session.execute(
        delete(PendingInput).where(
            PendingInput.account_id == row.account_id,
            PendingInput.kind == "owner_request",
            PendingInput.ref_id == row.id,
        )
    )


async def _suspend_other_focus(
    session: AsyncSession, *, account_id: int, request_id: int | None, now: datetime
) -> None:
    statement = select(OwnerRequest).where(
        OwnerRequest.account_id == account_id, OwnerRequest.focused.is_(True)
    )
    if request_id is not None:
        statement = statement.where(OwnerRequest.id != request_id)
    for other in await session.scalars(
        statement.order_by(OwnerRequest.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ):
        other.focused = False
        other.state = "suspended"
        other.reason = "topic_switch"
        other.updated_at = now
        other.expires_at = other.requested_at + REQUEST_TTL
        other.version += 1
        await _clear_question(session, other)
    await session.flush()


async def expire_requests(session: AsyncSession, *, account_id: int, now: datetime) -> int:
    _check_time(now)
    if not await lock_requests(session, account_id=account_id):
        return 0
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
    changed = 0
    for row in rows:
        action = None
        if row.action_id is not None:
            action = await session.scalar(
                select(Action)
                .where(Action.account_id == account_id, Action.id == row.action_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        if action is not None and action.state == "succeeded":
            row.state, row.reason, row.ended_at = "completed", "action_terminal", now
        elif row.state != "awaiting_approval" and row.expires_at <= now:
            await cancel_linked_action(session, row, now=now, reason=StateReason.MAX_AGE)
            row.state, row.reason, row.ended_at = "expired", "max_age", now
        elif (
            action is not None
            and action.state in (TERMINAL | {ActionState.DELIVERY_UNCERTAIN})
            and not is_warning_card(action)
        ):
            if row.state == "suspended" and row.reason == "action_terminal":
                continue
            row.state, row.reason = "suspended", "action_terminal"
        elif row.state == "awaiting_approval" and action is None:
            row.state, row.reason = "suspended", "action_terminal"
        elif row.state == "awaiting_approval" and action is not None:
            if action.state in ("executing", "approved", "scheduled"):
                continue
            if (
                (
                    action.state not in ("awaiting_approval", "revising")
                    and not is_warning_card(action)
                )
                or action.version != row.action_version
                or action.expires_at is None
            ):
                row.state, row.reason = "suspended", "action_terminal"
            elif action.expires_at > now:
                continue
            else:
                await cancel_linked_action(session, row, now=now, reason=StateReason.MAX_AGE)
                row.state, row.reason = "suspended", "action_terminal"
        else:
            continue
        row.focused = False
        row.missing_slot = None
        if row.state == "suspended":
            row.expires_at = row.requested_at + REQUEST_TTL
        row.version += 1
        row.updated_at = now
        await _clear_question(session, row)
        changed += 1
    await session.flush()
    return changed


async def resolve_continuation(
    session: AsyncSession,
    *,
    account_id: int,
    turn_id: int,
    reply_to_message_id: int | None,
    now: datetime,
) -> Continuation:
    """Resolve structural control artifacts before implicit request focus."""
    from assistant.actions.continuations import resolve

    _check_time(now)
    return await resolve(
        session,
        account_id=account_id,
        turn_id=turn_id,
        reply_to_message_id=reply_to_message_id,
        now=now,
    )
