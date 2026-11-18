"""Content-free incident report (I25, ARCHITECTURE §17, finding R4-10).

`python -m assistant incident-report --account-id A (--run-id R | --action-id X
| --request-id Q)` explains a wrong draft from structural rows only: the owner
run(s), the I25 decision trace (request selection, instruction sources,
recipient selection, dropped channels, observed versions, child runs,
outcomes), the run's tool steps (name and status), the linked actions and
requests, and successful card presentations. It prints ids, enums, counts and
times; it never selects a content column (`actions.text`, `control_turns.text`,
names, payloads), so it cannot show message text, names or secrets.

Whether the draft *meant* what the owner intended still needs the owner's
judgment or a synthetic reproduction; the report shows which structural
decision led there.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.actions.models import Action
from assistant.actions.request_models import OwnerRequest
from assistant.agent.models import AgentRun, AgentRunStep, IncidentTraceEvent

#: Runs one report covers (newest kept), so a busy request stays readable.
MAX_REPORT_RUNS = 10


@dataclass(frozen=True, slots=True)
class RunFact:
    run_id: int
    run_type: str
    status: str
    prompt_version: str | None
    tainted: bool
    sources_invalidated: bool
    iterations: int
    error_class: str | None
    started_at: datetime
    parent_run_id: int | None


@dataclass(frozen=True, slots=True)
class TraceFact:
    run_id: int
    seq: int
    stage: str
    code: str
    reason: str | None
    turn_id: int | None
    request_id: int | None
    request_version: int | None
    child_run_id: int | None
    action_id: int | None
    action_version: int | None
    conversation_id: int | None


@dataclass(frozen=True, slots=True)
class StepFact:
    run_id: int
    seq: int
    tool_name: str
    status: str


@dataclass(frozen=True, slots=True)
class ActionFact:
    action_id: int
    conversation_id: int
    kind: str
    origin: str
    state: str
    state_reason: str | None
    version: int
    run_id: int | None
    draft_run_id: int | None
    owner_request_id: int | None
    trigger_turn_id: int | None
    presented_versions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class RequestFact:
    request_id: int
    version: int
    operation: str
    state: str
    selection_basis: str | None
    conversation_id: int | None
    trigger_turn_id: int
    last_turn_id: int
    action_id: int | None
    action_version: int | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class IncidentSummary:
    """The five facts a wrong-draft investigation starts from."""

    instruction_sources: tuple[str, ...]
    recipient_selection: tuple[str, ...]
    dropped_channels: tuple[str, ...]
    observed_versions: tuple[str, ...]
    outcomes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IncidentReport:
    account_id: int
    runs: tuple[RunFact, ...]
    trace: tuple[TraceFact, ...]
    steps: tuple[StepFact, ...]
    actions: tuple[ActionFact, ...]
    requests: tuple[RequestFact, ...]

    @property
    def summary(self) -> IncidentSummary:
        def pick(stage: str, *, codes: Iterable[str] | None = None) -> list[TraceFact]:
            wanted = None if codes is None else set(codes)
            return [
                t for t in self.trace if t.stage == stage and (wanted is None or t.code in wanted)
            ]

        return IncidentSummary(
            instruction_sources=tuple(f"{t.code} turn={t.turn_id}" for t in pick("instruction")),
            recipient_selection=tuple(
                f"{t.code} conversation={t.conversation_id}" for t in pick("recipient")
            ),
            dropped_channels=tuple(f"{t.code} turn={t.turn_id}" for t in pick("dropped")),
            observed_versions=tuple(
                f"action={t.action_id} v{t.action_version} {t.code}" for t in pick("observed")
            ),
            outcomes=tuple(
                f"action={t.action_id} v{t.action_version} {t.code}"
                + (f" ({t.reason})" if t.reason else "")
                for t in pick("outcome")
            )
            + tuple(f"run={t.run_id} {t.code}" for t in pick("run")),
        )


def _parent(trigger_ref: str | None) -> int | None:
    """`draft:run:<owner run>:...` names the parent run; nothing else is parsed."""
    if trigger_ref is None or not trigger_ref.startswith("draft:run:"):
        return None
    value = trigger_ref.removeprefix("draft:run:").split(":", 1)[0]
    return int(value) if value.isdigit() else None


async def _root_runs(
    session: AsyncSession,
    *,
    account_id: int,
    run_id: int | None,
    action_id: int | None,
    request_id: int | None,
) -> list[int]:
    found: set[int] = set()
    if run_id is not None:
        found.add(run_id)
    if action_id is not None:
        owner_run = await session.scalar(
            select(Action.run_id).where(Action.account_id == account_id, Action.id == action_id)
        )
        if owner_run is not None:
            found.add(owner_run)
        found.update(
            await session.scalars(
                select(IncidentTraceEvent.run_id).where(
                    IncidentTraceEvent.account_id == account_id,
                    IncidentTraceEvent.action_id == action_id,
                )
            )
        )
    if request_id is not None:
        found.update(
            await session.scalars(
                select(IncidentTraceEvent.run_id).where(
                    IncidentTraceEvent.account_id == account_id,
                    IncidentTraceEvent.request_id == request_id,
                )
            )
        )
        found.update(
            value
            for value in await session.scalars(
                select(Action.run_id).where(
                    Action.account_id == account_id, Action.owner_request_id == request_id
                )
            )
            if value is not None
        )
    if not found:
        return []
    # Only this account's runs; a foreign id simply finds nothing.
    rows = await session.scalars(
        select(AgentRun.id)
        .where(AgentRun.account_id == account_id, AgentRun.id.in_(found))
        .order_by(AgentRun.id.desc())
        .limit(MAX_REPORT_RUNS)
    )
    return sorted(rows)


async def build_incident_report(
    session: AsyncSession,
    *,
    account_id: int,
    run_id: int | None = None,
    action_id: int | None = None,
    request_id: int | None = None,
) -> IncidentReport:
    """Collect the structural facts for one run, action or request (account scoped)."""
    roots = await _root_runs(
        session, account_id=account_id, run_id=run_id, action_id=action_id, request_id=request_id
    )
    trace_rows = (
        list(
            await session.scalars(
                select(IncidentTraceEvent)
                .where(
                    IncidentTraceEvent.account_id == account_id,
                    IncidentTraceEvent.run_id.in_(roots),
                )
                .order_by(IncidentTraceEvent.run_id, IncidentTraceEvent.seq)
            )
        )
        if roots
        else []
    )
    children = {row.child_run_id for row in trace_rows if row.child_run_id is not None}
    if roots:
        children.update(
            value
            for value in await session.scalars(
                select(Action.draft_run_id).where(
                    Action.account_id == account_id, Action.run_id.in_(roots)
                )
            )
            if value is not None
        )
    run_ids = set(roots) | children
    runs = (
        list(
            await session.scalars(
                select(AgentRun)
                .where(AgentRun.account_id == account_id, AgentRun.id.in_(run_ids))
                .order_by(AgentRun.id)
            )
        )
        if run_ids
        else []
    )
    steps = (
        list(
            await session.execute(
                select(
                    AgentRunStep.run_id,
                    AgentRunStep.seq,
                    AgentRunStep.tool_name,
                    AgentRunStep.status,
                )
                .join(AgentRun, AgentRun.id == AgentRunStep.run_id)
                .where(AgentRun.account_id == account_id, AgentRunStep.run_id.in_(roots))
                .order_by(AgentRunStep.run_id, AgentRunStep.seq)
            )
        )
        if roots
        else []
    )
    action_ids = {row.action_id for row in trace_rows if row.action_id is not None}
    if action_id is not None:
        action_ids.add(action_id)
    conditions: list[ColumnElement[bool]] = [Action.id.in_(action_ids)] if action_ids else []
    if roots:
        conditions.append(Action.run_id.in_(roots))
    if request_id is not None:
        conditions.append(Action.owner_request_id == request_id)
    actions = (
        list(
            await session.execute(
                select(
                    Action.id,
                    Action.conversation_id,
                    Action.kind,
                    Action.origin,
                    Action.state,
                    Action.state_reason,
                    Action.version,
                    Action.run_id,
                    Action.draft_run_id,
                    Action.owner_request_id,
                    Action.trigger_turn_id,
                )
                .where(Action.account_id == account_id, or_(*conditions))
                .order_by(Action.id)
            )
        )
        if conditions
        else []
    )
    presented: dict[int, set[int]] = {}
    if actions:
        audit = await session.execute(
            select(AuditLogEntry.subject_id, AuditLogEntry.details).where(
                AuditLogEntry.account_id == account_id,
                AuditLogEntry.event == "card_presented",
                AuditLogEntry.subject_id.in_([row[0] for row in actions]),
            )
        )
        for subject, details in audit:
            version = details.get("version") if isinstance(details, dict) else None
            if subject is not None and type(version) is int:
                presented.setdefault(subject, set()).add(version)
    request_ids = {row.request_id for row in trace_rows if row.request_id is not None}
    request_ids.update(row[9] for row in actions if row[9] is not None)
    if request_id is not None:
        request_ids.add(request_id)
    requests = (
        list(
            await session.execute(
                select(
                    OwnerRequest.id,
                    OwnerRequest.version,
                    OwnerRequest.operation,
                    OwnerRequest.state,
                    OwnerRequest.selection_basis,
                    OwnerRequest.conversation_id,
                    OwnerRequest.trigger_turn_id,
                    OwnerRequest.last_turn_id,
                    OwnerRequest.action_id,
                    OwnerRequest.action_version,
                    OwnerRequest.reason,
                )
                .where(OwnerRequest.account_id == account_id, OwnerRequest.id.in_(request_ids))
                .order_by(OwnerRequest.id)
            )
        )
        if request_ids
        else []
    )
    return IncidentReport(
        account_id=account_id,
        runs=tuple(
            RunFact(
                run_id=run.id,
                run_type=run.run_type,
                status=run.status,
                prompt_version=run.prompt_version,
                tainted=run.tainted,
                sources_invalidated=run.sources_invalidated,
                iterations=run.iterations,
                error_class=run.error_class,
                started_at=run.started_at,
                parent_run_id=_parent(run.trigger_ref),
            )
            for run in runs
        ),
        trace=tuple(
            TraceFact(
                run_id=row.run_id,
                seq=row.seq,
                stage=row.stage,
                code=row.code,
                reason=row.reason,
                turn_id=row.turn_id,
                request_id=row.request_id,
                request_version=row.request_version,
                child_run_id=row.child_run_id,
                action_id=row.action_id,
                action_version=row.action_version,
                conversation_id=row.conversation_id,
            )
            for row in trace_rows
        ),
        steps=tuple(StepFact(*row) for row in steps),
        actions=tuple(
            ActionFact(*row, presented_versions=tuple(sorted(presented.get(row[0], ()))))
            for row in actions
        ),
        requests=tuple(RequestFact(*row) for row in requests),
    )


def _ids(**values: object) -> str:
    return " ".join(f"{name}={value}" for name, value in values.items() if value is not None)


def format_incident_report(report: IncidentReport) -> str:
    """Plain lines of ids, enums and times (UTC); no content can appear."""
    if not report.runs and not report.actions and not report.requests:
        return f"incident report: account={report.account_id}: nothing found"
    lines = [f"incident report: account={report.account_id}"]
    summary = report.summary
    lines.append("summary:")
    for label, values in (
        ("instruction source", summary.instruction_sources),
        ("recipient selection", summary.recipient_selection),
        ("dropped channels", summary.dropped_channels),
        ("observed versions", summary.observed_versions),
        ("outcome", summary.outcomes),
    ):
        lines.append(f"  {label}: {'; '.join(values) if values else 'none recorded'}")
    lines.append("runs:")
    for run in report.runs:
        lines.append(
            "  "
            + _ids(
                run=run.run_id,
                type=run.run_type,
                status=run.status,
                prompt=run.prompt_version,
                parent=run.parent_run_id,
                error=run.error_class,
            )
            + f" tainted={run.tainted} invalidated={run.sources_invalidated}"
            + f" iterations={run.iterations} started={run.started_at.isoformat()}"
        )
    lines.append("trace:")
    for item in report.trace:
        lines.append(
            f"  run={item.run_id} #{item.seq} {item.stage}/{item.code} "
            + _ids(
                reason=item.reason,
                turn=item.turn_id,
                request=item.request_id,
                request_v=item.request_version,
                child_run=item.child_run_id,
                action=item.action_id,
                action_v=item.action_version,
                conversation=item.conversation_id,
            )
        )
    lines.append("steps:")
    for step in report.steps:
        lines.append(f"  run={step.run_id} #{step.seq} {step.tool_name} {step.status}")
    lines.append("actions:")
    for action in report.actions:
        lines.append(
            "  "
            + _ids(
                action=action.action_id,
                conversation=action.conversation_id,
                kind=action.kind,
                origin=action.origin,
                state=action.state,
                reason=action.state_reason,
                version=action.version,
                run=action.run_id,
                draft_run=action.draft_run_id,
                request=action.owner_request_id,
                turn=action.trigger_turn_id,
            )
            + f" presented={list(action.presented_versions)}"
        )
    lines.append("requests:")
    for request in report.requests:
        lines.append(
            "  "
            + _ids(
                request=request.request_id,
                version=request.version,
                operation=request.operation,
                state=request.state,
                basis=request.selection_basis,
                conversation=request.conversation_id,
                trigger_turn=request.trigger_turn_id,
                last_turn=request.last_turn_id,
                action=request.action_id,
                action_v=request.action_version,
                reason=request.reason,
            )
        )
    return "\n".join(lines)
