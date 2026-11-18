"""Structural incident trace writer (I25, ARCHITECTURE §17, finding R4-10).

An owner run records the decisions that explain a wrong draft weeks later:
which request the run continued and how, where the owner's instruction came
from, which recipient was selected, which channels of the owner's wishes were
dropped, which action version each proposal result showed the model, the
draft child runs, and the action states at the end. Rows hold ids and fixed
codes only (`agent.models.TRACE_CODES`): a value outside a closed list is
stored as `other`, an id that is not this account's is stored as NULL, so no
message text, name, token or model output can reach the trace.

Bounded: `MAX_TRACE_EVENTS_PER_RUN` rows per run. Start and tool events use
at most `REGULAR_SLOTS`; overflow ends them with one `trace/truncated` row.
The remaining `FINISH_RESERVE` slots are kept for the finish batch (final
action states, late child runs, run status), so the outcome is always
recorded and the run status is always the last row.

Best effort: the three hook entry points (`start_trace`, `IncidentTrace.tool`,
`trace_finished_run`) contain every `Exception`, log its type only and never
change the run (a staged action, a delivered reply). Cancellation propagates.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import ControlTurn
from assistant.actions.models import Action
from assistant.actions.request_models import OwnerRequest
from assistant.agent.models import (
    MAX_TRACE_EVENTS_PER_RUN,
    TRACE_CODES,
    TRACE_REASONS,
    AgentRun,
    IncidentTraceEvent,
)
from assistant.conversations.models import Conversation
from assistant.db.uow import transaction
from assistant.observability.logging import get_logger

if TYPE_CHECKING:
    from assistant.actions.activity import ActionReceipt
    from assistant.agent.tools.registry import ToolContext, ToolOutcome

logger = get_logger(__name__)

#: Tools whose results carry the proposal/revision/read decisions.
TRACED_TOOLS = frozenset(
    {"propose_reply", "propose_message", "propose_owner_message", "revise_draft", "get_draft"}
)
#: Request sources traced per run start; the rest are counted by the request.
MAX_SOURCES_TRACED = 8
#: Actions traced as outcomes at finish.
MAX_OUTCOMES_TRACED = 16
#: Child runs read per hook call.
MAX_CHILD_RUNS_PER_READ = 4
#: Slots kept for the finish batch: outcomes, late child runs, run status.
FINISH_RESERVE = MAX_OUTCOMES_TRACED + MAX_CHILD_RUNS_PER_READ + 1
#: Slots for start and tool events; one more is kept for `trace/truncated`.
REGULAR_SLOTS = MAX_TRACE_EVENTS_PER_RUN - FINISH_RESERVE - 1
#: Legacy writer instruction sources that mean "the owner's wishes were dropped".
_DROPPED_SOURCES = frozenset({"dropped_quoted", "dropped_tainted"})
_REASONS = frozenset(TRACE_REASONS)


def _id(value: object) -> int | None:
    """A positive int id, else None (bools and strings never pass)."""
    return value if type(value) is int and value > 0 else None


def _code(stage: str, value: object) -> str:
    codes = TRACE_CODES[stage]
    return value if isinstance(value, str) and value in codes else "other"


def _reason(value: object) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in _REASONS else "other"


def _contained(where: str, account_id: object, run_id: object, error: Exception) -> None:
    """One content-free line for a contained trace failure: the type only."""
    logger.warning(
        "incident_trace_failed",
        where=where,
        account_id=_id(account_id),
        run_id=_id(run_id),
        error_type=type(error).__name__,
    )


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One decision before it is stored. Use `event()` to build it."""

    stage: str
    code: str
    reason: str | None = None
    turn_id: int | None = None
    request_id: int | None = None
    request_version: int | None = None
    child_run_id: int | None = None
    action_id: int | None = None
    action_version: int | None = None
    conversation_id: int | None = None


def event(
    stage: str,
    code: object,
    *,
    reason: object = None,
    turn_id: object = None,
    request_id: object = None,
    request_version: object = None,
    child_run_id: object = None,
    action_id: object = None,
    action_version: object = None,
    conversation_id: object = None,
) -> TraceEvent:
    """Normalize a decision: closed code/reason lists, positive int ids only."""
    if stage not in TRACE_CODES:
        raise ValueError(f"unknown trace stage {stage!r}")
    return TraceEvent(
        stage=stage,
        code=_code(stage, code),
        reason=_reason(reason),
        turn_id=_id(turn_id),
        request_id=_id(request_id),
        request_version=_id(request_version),
        child_run_id=_id(child_run_id),
        action_id=_id(action_id),
        action_version=_id(action_version),
        conversation_id=_id(conversation_id),
    )


# ------------------------------------------------------------------ storage
async def _scoped(
    session: AsyncSession, model: Any, account_id: int, ids: Iterable[int | None]
) -> set[int]:
    wanted = {value for value in ids if value is not None}
    if not wanted:
        return set()
    rows = await session.scalars(
        select(model.id).where(model.account_id == account_id, model.id.in_(wanted))
    )
    return set(rows)


def _bounded(batch: list[TraceEvent], used: int, *, final: bool) -> list[TraceEvent]:
    """Fit a batch into the run's slots (see the module docstring)."""
    if final:
        capacity = MAX_TRACE_EVENTS_PER_RUN - used
        if capacity <= 0:
            return []
        if len(batch) > capacity:
            # The run status (last) always survives.
            return [*batch[: capacity - 1], batch[-1]]
        return batch
    regular = max(REGULAR_SLOTS - used, 0)
    if len(batch) <= regular:
        return batch
    if used > REGULAR_SLOTS:  # the truncation marker is already there
        return []
    return [*batch[:regular], event("trace", "truncated")]


async def _store(
    session: AsyncSession,
    *,
    account_id: int,
    run_id: int,
    events: Sequence[TraceEvent],
    final: bool,
) -> int:
    # FOR NO KEY UPDATE: serializes trace writers per run without blocking FK
    # inserts that reference the run (their KEY SHARE locks do not conflict).
    run = await session.scalar(
        select(AgentRun.id)
        .where(AgentRun.account_id == account_id, AgentRun.id == run_id)
        .with_for_update(key_share=True)
    )
    if run is None:
        return 0
    used = int(
        await session.scalar(
            select(func.coalesce(func.max(IncidentTraceEvent.seq), 0)).where(
                IncidentTraceEvent.run_id == run_id
            )
        )
        or 0
    )
    batch = _bounded(list(events), used, final=final)
    if not batch:
        return 0
    # Tenant scoping of every reference: another account's id is never stored.
    turns = await _scoped(session, ControlTurn, account_id, (e.turn_id for e in batch))
    requests = await _scoped(session, OwnerRequest, account_id, (e.request_id for e in batch))
    runs = await _scoped(session, AgentRun, account_id, (e.child_run_id for e in batch))
    actions = await _scoped(session, Action, account_id, (e.action_id for e in batch))
    chats = await _scoped(session, Conversation, account_id, (e.conversation_id for e in batch))
    for offset, item in enumerate(batch, start=1):
        request_id = item.request_id if item.request_id in requests else None
        action_id = item.action_id if item.action_id in actions else None
        session.add(
            IncidentTraceEvent(
                account_id=account_id,
                run_id=run_id,
                seq=used + offset,
                stage=item.stage,
                code=item.code,
                reason=item.reason,
                turn_id=item.turn_id if item.turn_id in turns else None,
                request_id=request_id,
                request_version=item.request_version if request_id is not None else None,
                child_run_id=item.child_run_id if item.child_run_id in runs else None,
                action_id=action_id,
                action_version=item.action_version if action_id is not None else None,
                conversation_id=item.conversation_id if item.conversation_id in chats else None,
            )
        )
    return len(batch)


async def write_events(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: int,
    run_id: int,
    events: Sequence[TraceEvent],
    final: bool = False,
) -> int:
    """Append events to a run's trace in one transaction; DB errors are logged."""
    if not events:
        return 0
    try:
        async with transaction(session_factory) as uow:
            return await _store(
                uow.session, account_id=account_id, run_id=run_id, events=events, final=final
            )
    except SQLAlchemyError as error:
        _contained("write", account_id, run_id, error)
        return 0


async def _child_runs(
    session: AsyncSession, *, account_id: int, run_id: int, known: set[int]
) -> list[TraceEvent]:
    """Draft runs this owner run started (`draft:run:<id>:...`) not yet traced.

    Bounded: a range on (account_id, started_at) from the parent's start, at
    most `MAX_CHILD_RUNS_PER_READ` rows."""
    parent_start = (
        select(AgentRun.started_at)
        .where(AgentRun.account_id == account_id, AgentRun.id == run_id)
        .scalar_subquery()
    )
    query = select(AgentRun.id, AgentRun.status).where(
        AgentRun.account_id == account_id,
        AgentRun.started_at >= parent_start,
        AgentRun.trigger_ref.like(f"draft:run:{int(run_id)}:%"),
    )
    if known:
        query = query.where(AgentRun.id.not_in(known))
    rows = await session.execute(query.order_by(AgentRun.id).limit(MAX_CHILD_RUNS_PER_READ))
    found: list[TraceEvent] = []
    for child_id, status in rows:
        known.add(child_id)
        found.append(event("child_run", status, child_run_id=child_id))
    return found


# ------------------------------------------------------------------ recorder
@dataclass
class IncidentTrace:
    """The per-run recorder the executor calls after each traced tool."""

    session_factory: async_sessionmaker[AsyncSession]
    account_id: int
    run_id: int
    children: set[int] = field(default_factory=set)

    async def tool(self, ctx: ToolContext, tool: str, outcome: ToolOutcome) -> None:
        """Executor hook. Never raises (except cancellation)."""
        try:
            if tool in TRACED_TOOLS:
                await self._record(ctx, outcome)
        except Exception as error:
            _contained("tool", self.account_id, self.run_id, error)

    async def _record(self, ctx: ToolContext, outcome: ToolOutcome) -> None:
        events = tool_events(ctx, outcome)
        try:
            async with self.session_factory() as session:
                action_id = next((e.action_id for e in events if e.stage == "observed"), None)
                if action_id is not None and not any(
                    e.stage == "observed" and e.action_version for e in events
                ):
                    version = await session.scalar(
                        select(Action.version).where(
                            Action.account_id == self.account_id, Action.id == action_id
                        )
                    )
                    events = [
                        replace(e, action_version=_id(version)) if e.stage == "observed" else e
                        for e in events
                    ]
                events.extend(
                    await _child_runs(
                        session,
                        account_id=self.account_id,
                        run_id=self.run_id,
                        known=self.children,
                    )
                )
        except SQLAlchemyError as error:
            _contained("tool_read", self.account_id, self.run_id, error)
        await write_events(
            self.session_factory, account_id=self.account_id, run_id=self.run_id, events=events
        )


def tool_events(ctx: ToolContext, outcome: ToolOutcome) -> list[TraceEvent]:
    """The decisions one traced tool result shows. Pure; reads fixed keys only."""
    data: Mapping[str, Any] = outcome.data
    detail: Mapping[str, Any] = outcome.log_detail
    events: list[TraceEvent] = []
    if outcome.taints and not ctx.tainted:
        events.append(event("taint", "tainted_by_tool"))
    source = detail.get("instruction_source")
    if isinstance(source, str) and source in _DROPPED_SOURCES:
        events.append(event("dropped", source, turn_id=ctx.turn_id))
    elif isinstance(source, str):
        events.append(event("instruction", source, turn_id=ctx.turn_id))
    if data.get("guidance_ignored"):
        events.append(event("dropped", "guidance_ignored", turn_id=ctx.turn_id))
    raw_receipt = data.get("action")
    receipt: Mapping[str, Any] = raw_receipt if isinstance(raw_receipt, Mapping) else {}
    action_id = receipt.get("action_id", data.get("action_id"))
    request_id = data.get("request_id", receipt.get("request_id"))
    request_version = data.get("request_version")
    events.append(
        event(
            "proposal",
            data.get("proposal_status") or outcome.status,
            reason=data.get("reason"),
            request_id=request_id,
            request_version=request_version,
            action_id=action_id,
            action_version=receipt.get("version"),
        )
    )
    conversation_id = receipt.get("conversation_id", data.get("conversation_id"))
    if _id(conversation_id) is not None and _id(action_id) is not None:
        events.append(
            event(
                "recipient",
                "proposal_target",
                conversation_id=conversation_id,
                action_id=action_id,
                request_id=request_id,
            )
        )
    if _id(action_id) is not None:
        events.append(
            event(
                "observed",
                receipt.get("presentation", "unknown"),
                action_id=action_id,
                action_version=receipt.get("version"),
                conversation_id=conversation_id,
            )
        )
    return events


def start_events(ctx: ToolContext, *, opened_by_turn: bool) -> list[TraceEvent]:
    """What the run started from: request selection, sources, recipient, taint."""
    events: list[TraceEvent] = []
    continuation = ctx.continuation
    view = continuation.request if continuation is not None else None
    if continuation is None:
        events.append(event("request", "none", turn_id=ctx.turn_id))
    else:
        selection = continuation.selection
        if selection.status == "selected":
            code: object = "opened_by_turn" if opened_by_turn else selection.basis
        else:
            code = selection.status
        events.append(
            event(
                "request",
                code,
                reason=selection.code,
                turn_id=ctx.turn_id,
                request_id=selection.request_id,
                request_version=selection.version,
                action_id=selection.action_id,
                action_version=selection.action_version,
            )
        )
    if view is not None:
        for source in view.sources[:MAX_SOURCES_TRACED]:
            events.append(
                event(
                    "instruction",
                    source.purpose,
                    turn_id=source.span.turn_id,
                    request_id=view.request_id,
                    request_version=view.version,
                )
            )
        events.append(
            event(
                "recipient",
                view.selection_basis or "unbound",
                request_id=view.request_id,
                request_version=view.version,
                conversation_id=view.conversation_id,
            )
        )
    turn = ctx.owner_turn
    if turn is not None and turn.forwarded:
        events.append(event("dropped", "forwarded_turn", turn_id=turn.id))
    if not ctx.managed_ok:
        events.append(event("dropped", "managed_access_off", turn_id=ctx.turn_id))
    if ctx.tainted:
        events.append(event("taint", "started_tainted", turn_id=ctx.turn_id))
    return events


async def start_trace(
    session_factory: async_sessionmaker[AsyncSession], ctx: ToolContext
) -> IncidentTrace:
    """Runtime hook: record the starting decisions; always return the recorder."""
    trace = IncidentTrace(session_factory, ctx.account_id, ctx.run_id)
    try:
        opened = False
        continuation = ctx.continuation
        request_id = continuation.selection.request_id if continuation is not None else None
        if request_id is not None and ctx.turn_id is not None:
            async with session_factory() as session:
                trigger = await session.scalar(
                    select(OwnerRequest.trigger_turn_id).where(
                        OwnerRequest.account_id == ctx.account_id,
                        OwnerRequest.id == request_id,
                    )
                )
            opened = trigger == ctx.turn_id
        await write_events(
            session_factory,
            account_id=ctx.account_id,
            run_id=ctx.run_id,
            events=start_events(ctx, opened_by_turn=opened),
        )
    except Exception as error:
        _contained("start", ctx.account_id, ctx.run_id, error)
    return trace


def _outcome_events(receipts: Mapping[int, ActionReceipt]) -> list[TraceEvent]:
    return [
        event(
            "outcome",
            receipt.state,
            reason=receipt.state_reason,
            action_id=receipt.action_id,
            action_version=receipt.version,
            conversation_id=receipt.conversation_id,
            request_id=receipt.request_id,
        )
        for receipt in (receipts[key] for key in sorted(receipts)[:MAX_OUTCOMES_TRACED])
    ]


async def finish_trace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    account_id: int,
    run_id: int,
    status: str,
    receipts: Mapping[int, ActionReceipt] | None = None,
) -> None:
    """Record the observed outcome of every action the run touched, then the run status."""
    events: list[TraceEvent] = []
    try:
        async with session_factory() as session:
            if receipts:
                events.extend(_outcome_events(receipts))
            else:
                rows = await session.execute(
                    select(
                        Action.id,
                        Action.state,
                        Action.state_reason,
                        Action.version,
                        Action.conversation_id,
                        Action.owner_request_id,
                    )
                    .where(Action.account_id == account_id, Action.run_id == run_id)
                    .order_by(Action.id)
                    .limit(MAX_OUTCOMES_TRACED)
                )
                for action_id, state, reason, version, conversation_id, request_id in rows:
                    events.append(
                        event(
                            "outcome",
                            state,
                            reason=reason,
                            action_id=action_id,
                            action_version=version,
                            conversation_id=conversation_id,
                            request_id=request_id,
                        )
                    )
            traced = set(
                await session.scalars(
                    select(IncidentTraceEvent.child_run_id).where(
                        IncidentTraceEvent.account_id == account_id,
                        IncidentTraceEvent.run_id == run_id,
                        IncidentTraceEvent.child_run_id.is_not(None),
                    )
                )
            )
            events.extend(
                await _child_runs(
                    session,
                    account_id=account_id,
                    run_id=run_id,
                    known={value for value in traced if value is not None},
                )
            )
    except SQLAlchemyError as error:
        _contained("finish_read", account_id, run_id, error)
    events.append(event("run", status))
    await write_events(
        session_factory, account_id=account_id, run_id=run_id, events=events, final=True
    )


class FinishedRun(Protocol):
    """What `_finish` knows about the run (the runtime's private run state)."""

    @property
    def account_id(self) -> int: ...
    @property
    def run_id(self) -> int: ...
    @property
    def final_receipts(self) -> Mapping[int, ActionReceipt] | None: ...


async def trace_finished_run(
    session_factory: async_sessionmaker[AsyncSession], run: FinishedRun, status: str
) -> None:
    """Runtime `_finish` hook. Never raises (except cancellation): the reply is
    already delivered and the run row committed."""
    try:
        await finish_trace(
            session_factory,
            account_id=run.account_id,
            run_id=run.run_id,
            status=status,
            receipts=run.final_receipts,
        )
    except Exception as error:
        _contained("finish", getattr(run, "account_id", None), getattr(run, "run_id", None), error)
