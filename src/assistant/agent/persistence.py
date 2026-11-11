"""Persistence for agent runs (ARCHITECTURE §10.3).

Account-scoped repositories over `control_turns`, `agent_runs`,
`agent_run_steps` and `llm_calls`, plus the database-backed implementations
of the two ports the LLM wrappers need (`ConsentReader`, `CallRecorder`).

Every write is a short transaction of its own: none is held across an LLM or
Telegram call (AGENTS §13).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import ControlTurn, PendingInput
from assistant.accounts.service import ConsentState, active_consent
from assistant.actions.models import Action
from assistant.actions.recipient_resolution import select_candidate_answer
from assistant.actions.request_models import OwnerRequest
from assistant.actions.requests import (
    Continuation,
    OwnerContinuation,
    RequestSource,
    SetState,
    SourceSpan,
    SourceUnavailable,
    ValidatedSource,
    load_request,
    load_source,
    lock_requests,
    resolve_continuation,
    update_request,
)
from assistant.agent.models import AgentRun, AgentRunStep, LlmCall
from assistant.core.clock import Clock
from assistant.core.errors import NotInAccountError
from assistant.db.repository import AccountScopedRepository
from assistant.db.uow import transaction
from assistant.llm.metering import estimate_cost
from assistant.llm.port import CallRecord


class ControlTurnRepository(AccountScopedRepository[ControlTurn]):
    model = ControlTurn

    async def recent_before(
        self, turn_id: int, *, limit: int, include_managed: bool
    ) -> list[ControlTurn]:
        """The `limit` turns before `turn_id`, oldest first.

        Managed turns are excluded without consent, except classified mixed
        quotes: the context selector retains only their owner-text component.
        Unknown legacy provenance never qualifies for that exception (D-S3).
        """
        statement = self._select().where(ControlTurn.id < turn_id)
        if not include_managed:
            statement = statement.where(
                or_(
                    ControlTurn.contains_managed.is_(False),
                    and_(
                        ControlTurn.role == "owner",
                        ControlTurn.third_party.is_(True),
                        ControlTurn.owner_text_verified.is_(True),
                        ControlTurn.quoted_text.is_not(None),
                        ControlTurn.from_tainted_run.is_(False),
                    ),
                )
            )
        statement = statement.order_by(ControlTurn.id.desc()).limit(limit)
        result = await self.session.execute(statement)
        rows = cast("list[ControlTurn]", list(result.scalars().all()))
        rows.reverse()
        return rows


async def load_preference_reply_source(
    session: AsyncSession, *, account_id: int, turn: ControlTurn
) -> ValidatedSource | None:
    """Reload only an explicitly selected, retained owner-text component.

    A preference needs no messaging request. C1 validates source authority
    independently of continuation selection and the recent history window.
    """
    if turn.account_id != account_id or turn.reply_to_telegram_message_id is None:
        return None
    selected = await session.scalar(
        select(ControlTurn).where(
            ControlTurn.account_id == account_id,
            ControlTurn.role == "owner",
            ControlTurn.telegram_message_id == turn.reply_to_telegram_message_id,
        )
    )
    if selected is None or not selected.text:
        return None
    loaded = await load_source(
        session,
        account_id=account_id,
        source=RequestSource(
            span=SourceSpan(
                turn_id=selected.id,
                field="text",
                start=0,
                end=len(selected.text),
                field_sha256=hashlib.sha256(selected.text.encode("utf-8")).hexdigest(),
            ),
            purpose="instruction",
        ),
    )
    if isinstance(loaded, SourceUnavailable) or loaded.untrusted or loaded.managed:
        return None
    return loaded


async def load_owner_continuation(
    session: AsyncSession,
    *,
    account_id: int,
    turn: ControlTurn,
    now: datetime,
    managed_allowed: bool,
) -> OwnerContinuation:
    """Reload durable state, including source spans outside recent history.

    Caller commits before model I/O. Rebuilding a lost UI question changes
    no source, request version, action version or original expiry.
    """
    if turn.account_id != account_id or not await lock_requests(session, account_id=account_id):
        return OwnerContinuation(Continuation("unavailable", code="not_available"))
    selection = await resolve_continuation(
        session,
        account_id=account_id,
        turn_id=turn.id,
        reply_to_message_id=turn.reply_to_telegram_message_id,
        now=now,
    )
    if selection.status != "selected":
        return OwnerContinuation(selection)
    if selection.request_id is None:
        if selection.action_id is not None and not managed_allowed:
            return OwnerContinuation(Continuation("unavailable", code="managed_access_off"))
        loaded = OwnerContinuation(selection)
        turn.refs = {**(turn.refs or {}), **loaded.as_refs()}
        action = await session.scalar(
            select(Action).where(Action.account_id == account_id, Action.id == selection.action_id)
        )
        if action is not None:
            turn.refs = {
                **turn.refs,
                "conversation_ids": sorted(
                    {*turn.refs.get("conversation_ids", []), action.conversation_id}
                ),
            }
        return loaded
    request = await load_request(session, account_id=account_id, request_id=selection.request_id)
    if request is None:
        return OwnerContinuation(Continuation("unavailable", code="not_available"))
    sources: list[ValidatedSource] = []
    source_withheld = False
    for source in request.sources:
        value = await load_source(session, account_id=account_id, source=source)
        if isinstance(value, SourceUnavailable):
            return OwnerContinuation(Continuation("unavailable", code="source_unavailable"))
        if not value.managed or managed_allowed:
            sources.append(value)
        else:
            source_withheld = True
    if source_withheld or (
        (request.conversation_id is not None or selection.action_id is not None)
        and not managed_allowed
    ):
        # Eligible owner-only words survive a closed managed gate, but none
        # of the recipient/card facts or managed source text do.
        return OwnerContinuation(
            Continuation("unavailable", code="managed_access_off"), sources=tuple(sources)
        )
    row = await session.scalar(
        select(OwnerRequest)
        .where(OwnerRequest.account_id == account_id, OwnerRequest.id == request.request_id)
        .with_for_update()
    )
    if (
        row is not None
        and row.state == "suspended"
        and row.reason == "topic_switch"
        and turn.reply_to_telegram_message_id is not None
    ):
        resumed = await update_request(
            session,
            account_id=account_id,
            request_id=row.id,
            expected_version=row.version,
            update=SetState(state="collecting" if row.missing_slot else "ready", reason=None),
            now=now,
        )
        if resumed.status != "applied":
            return OwnerContinuation(Continuation("unavailable", code="not_available"))
        request = resumed.view
        selection = Continuation(
            "selected",
            request_id=request.request_id,
            version=request.version,
            action_id=request.action_id,
            action_version=request.action_version,
            basis="explicit_reply",
        )
    question_id = None
    if managed_allowed:
        answered = await select_candidate_answer(
            session, account_id=account_id, request_id=request.request_id, turn=turn, now=now
        )
        if answered is not None and answered.status == "applied":
            request = answered.view
            selection = Continuation(
                "selected",
                request_id=request.request_id,
                version=request.version,
                action_id=request.action_id,
                action_version=request.action_version,
                basis=selection.basis,
            )
    if row is not None and row.missing_slot is not None and row.state == "collecting":
        question = await session.scalar(
            select(PendingInput).where(
                PendingInput.account_id == account_id,
                PendingInput.id == row.question_input_id,
                PendingInput.kind == "owner_request",
                PendingInput.ref_id == row.id,
                PendingInput.expires_at > now,
            )
        )
        if question is None:
            question = PendingInput(
                account_id=account_id,
                kind="owner_request",
                ref_id=row.id,
                expires_at=row.expires_at,
                created_at=now,
            )
            session.add(question)
            await session.flush()
            row.question_input_id = question.id
            # Autoflush is disabled. Persist the FK before a later pending
            # input cleanup can delete the question and SET NULL this link.
            await session.flush()
        question_id = question.id
    # Structural linkage makes a later explicit reply to either this owner
    # turn or its delivered assistant answer select the same request.
    loaded = OwnerContinuation(selection, request, tuple(sources), question_id)
    turn.refs = {**(turn.refs or {}), **loaded.as_refs()}
    if request.conversation_id is not None and request.contact_id is not None:
        turn.refs = {
            **turn.refs,
            "conversation_ids": sorted(
                {*turn.refs.get("conversation_ids", []), request.conversation_id}
            ),
            "contact_ids": sorted({*turn.refs.get("contact_ids", []), request.contact_id}),
        }
    return loaded


class AgentRunRepository(AccountScopedRepository[AgentRun]):
    model = AgentRun

    async def for_trigger(self, trigger_ref: str) -> list[AgentRun]:
        """Runs started for one trigger (e.g. `turn:42`), oldest first."""
        result = await self.session.execute(
            self._select().where(AgentRun.trigger_ref == trigger_ref).order_by(AgentRun.id)
        )
        return cast("list[AgentRun]", list(result.scalars().all()))

    async def add_step(
        self,
        run_id: int,
        *,
        tool_name: str,
        args_digest: str | None,
        status: str,
        latency_ms: int,
    ) -> AgentRunStep:
        """Append a content-free step. The run must belong to this account."""
        if await self.get(run_id) is None:
            raise NotInAccountError("run not found in this account")
        next_seq = (
            await self.session.execute(
                select(func.coalesce(func.max(AgentRunStep.seq), 0) + 1).where(
                    AgentRunStep.run_id == run_id
                )
            )
        ).scalar_one()
        step = AgentRunStep(
            run_id=run_id,
            seq=int(next_seq),
            tool_name=tool_name,
            args_digest=args_digest,
            status=status,
            latency_ms=latency_ms,
        )
        self.session.add(step)
        await self.session.flush()
        return step


class LlmCallRepository(AccountScopedRepository[LlmCall]):
    model = LlmCall

    async def spend_since(self, since: datetime) -> Decimal:
        """Estimated USD spent since `since` (NFR-10 budget checks)."""
        statement = select(func.sum(LlmCall.cost_estimate)).where(
            self._scope(), LlmCall.created_at >= since
        )
        total = (await self.session.execute(statement)).scalar_one()
        return Decimal(0) if total is None else Decimal(total)


async def account_of_turn(session: AsyncSession, turn_id: int) -> int | None:
    """Which account a control turn belongs to.

    The one unscoped lookup in the agent: a job carries only `turn_id`
    (AGENTS §13), and the account has to come from somewhere before the scoped
    repositories can be built. It returns an id, never a row.
    """
    result = await session.execute(select(ControlTurn.account_id).where(ControlTurn.id == turn_id))
    return result.scalar_one_or_none()


def args_digest(arguments: Any) -> str:
    """A stable hash of tool arguments, so a step can be matched without storing them."""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def month_start(moment: datetime) -> datetime:
    """Start of the UTC calendar month. The budget month is UTC for now."""
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class DbConsentReader:
    """`ConsentReader` over the database: a fresh read on every call."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(self, account_id: int, kind: str) -> ConsentState:
        async with transaction(self._session_factory) as uow:
            return await active_consent(uow.session, account_id, kind=kind)


class DbCallRecorder:
    """`CallRecorder` over the database: one `llm_calls` row per call."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def __call__(self, record: CallRecord) -> None:
        async with transaction(self._session_factory) as uow:
            LlmCallRepository(uow.session, record.account_id).add(
                LlmCall(
                    run_id=record.run_id,
                    purpose=record.purpose,
                    model=record.model,
                    input_tokens=record.usage.input_tokens,
                    output_tokens=record.usage.output_tokens,
                    cache_read_tokens=record.usage.cache_read_tokens,
                    cache_write_tokens=record.usage.cache_write_tokens,
                    cost_estimate=estimate_cost(record.model, record.usage),
                    request_id=record.request_id,
                    created_at=self._clock.now(),
                )
            )
