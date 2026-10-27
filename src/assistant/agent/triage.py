"""The `triage` run type (ARCHITECTURE §6.5 steps 4-5, §7.1; S05.1).

A contact-triggered run: its input is untrusted chat content, so it has **no
tools** (§6 invariant 3a, RG-17) — one structured-output call through
`LLMClient.parse`, with exactly the guarantees of `agent/summarize.py`:
consent (`ai_control` and `ai_managed`, both naming the route's processor)
and the monthly budget are checked before the `agent_runs` row is written
(a clean skip: no row, no call); the stop reason is read before any content;
a truncated answer is retried once with a larger budget, never continued by
prefilling; the output is re-validated by our own model, sanitized
(`triage_schemas.sanitize_triage`) and checked by `validate_triage` against
the messages shown and this chat's open tasks; the run row is closed on
every path, a cancellation included. No transaction is open across the call
(AGENTS §13).

ONE conversation per run (§6 invariant 4): the context
(`agent/triage_context.py`) is that chat's alone. The verdict is stored by a
`TriageStore` (`inbox/triage_store.py`), which re-checks under the §9.4
locks that every message the model read is unchanged, that the chat may
still be triaged and that no newer verdict exists — otherwise nothing is
stored (`DISCARDED` / `RACED`).

`agent_runs.trigger_ref` names the conversation and the newest message shown
(`triage:conv:<id>:through:<message id>`): the NFR-05 query reads latency
from it (S05.2 §3.9).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

import pydantic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.accounts.service import (
    CONSENT_AI_CONTROL,
    CONSENT_AI_MANAGED,
    ConsentMissingError,
    active_consent,
)
from assistant.agent import prompt_registry
from assistant.agent.attribution import CONTACT_SIDE, OWNER_SIDE
from assistant.agent.models import AgentRun
from assistant.agent.persistence import AgentRunRepository, LlmCallRepository, month_start
from assistant.agent.prompt_registry import Prompt
from assistant.agent.triage_context import TriageContext, build_triage
from assistant.agent.triage_schemas import SanitizedTriage, TriageResult, sanitize_triage
from assistant.core.clock import Clock
from assistant.core.model_routes import processor_for_model
from assistant.db.uow import transaction
from assistant.inbox import tasks
from assistant.inbox.triage_store import (
    ApplyResult,
    TaskCandidate,
    TriageState,
    TriageVerdict,
    burst_source,
)
from assistant.llm.port import LLMClient, LLMError, LLMErrorKind, LLMRequest, StopReason
from assistant.memory import items, summaries
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

RUN_TYPE = "triage"

#: Errors a retry would only repeat at a cost: after enough of them in a row,
#: that chat's triage pauses for a while (the summaries' rule, M03 D65).
_REPEATING_ERRORS = frozenset(
    {"refusal", "content_filter", "invalid_output", "max_tokens", "stop_other"}
)


@dataclass(frozen=True, slots=True)
class TriageConfig:
    """Model and budgets of the `triage` route (ARCHITECTURE §7.4)."""

    model: str
    effort: str | None = "low"
    max_output_tokens: int = 1500
    max_output_tokens_ceiling: int = 3000
    call_timeout_seconds: float = 60.0
    monthly_budget_usd: Decimal | None = None
    failure_backoff_runs: int = 3
    failure_backoff_window: timedelta = timedelta(hours=24)


class TriageStatus(StrEnum):
    TRIAGED = "triaged"
    UP_TO_DATE = "up_to_date"
    """Nothing new to triage (an edit or a deletion set the flag, or only
    the owner wrote and no task could be answered)."""
    NO_CONSENT = "no_consent"
    OVER_BUDGET = "over_budget"
    BACKED_OFF = "backed_off"
    REFUSED = "refused"
    FAILED = "failed"
    RACED = "raced"
    """A newer verdict was stored first: it stands."""
    DISCARDED = "discarded"
    """A message the model read changed, or the chat may no longer be
    triaged: nothing stored."""
    SKIPPED = "skipped"
    """Ignore mode or a bot chat: never triaged."""
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class TriageOutcome:
    status: TriageStatus
    account_id: int | None = None
    #: Model calls made (for the budget alerts after a job).
    calls: int = 0
    #: The newest message the run read, when it ran.
    through: int | None = None
    #: DISCARDED only: why.
    discard_reason: ApplyResult | None = None


class TriageStore(Protocol):
    """Where triage reads a conversation's verdict state and stores a new
    verdict with its effects (`inbox/triage_store.py`)."""

    async def state(
        self, session: AsyncSession, *, account_id: int, conversation_id: int
    ) -> TriageState | None: ...

    async def apply(
        self, session: AsyncSession, verdict: TriageVerdict, *, now: datetime
    ) -> ApplyResult: ...

    async def settle(self, session: AsyncSession, *, account_id: int, conversation_id: int) -> None:
        """Nothing to triage: clear the chat's triage flag."""
        ...


# ------------------------------------------------------------------ validation
@dataclass(frozen=True, slots=True)
class ValidationCounts:
    """What `validate_triage` kept and dropped (counts only: they are logged)."""

    commitments_kept: int = 0
    commitments_unshown: int = 0
    commitments_misattributed: int = 0
    questions_kept: int = 0
    questions_unshown: int = 0
    questions_misattributed: int = 0
    foreign_task_ids: int = 0


def _side(direction: str | None) -> str | None:
    if direction in OWNER_SIDE:
        return "owner"
    if direction in CONTACT_SIDE:
        return "contact"
    return None  # owner_automated (an away/greeting message): neutral


def validate_triage(
    verdict: SanitizedTriage,
    *,
    directions: Mapping[int, str],
    open_task_ids: Collection[int],
) -> tuple[SanitizedTriage, ValidationCounts]:
    """`verdict` checked against the messages shown (id → direction, from
    classification) and this conversation's open task ids (S05.1 §3.4).

    - An item citing a message that was not shown is dropped.
    - A commitment of P must cite a message of P's side, a question one of the
      contact's; otherwise it is **dropped** (TL decision: a task on the wrong
      side is worse than none). An automatic message supports neither.
    - Task ids that are not this conversation's open tasks are removed —
      never an error, never another chat's task.
    Flags, urgency and the category are kept as given."""
    open_ids = frozenset(open_task_ids)
    unshown = misattributed = 0
    foreign = 0
    commitments = []
    for item in verdict.commitments:
        direction = directions.get(item.source_message_id)
        if direction is None:
            unshown += 1
            continue
        if _side(direction) != item.by:
            misattributed += 1
            continue
        if item.existing_task_id is not None and item.existing_task_id not in open_ids:
            foreign += 1
            item = replace(item, existing_task_id=None)
        commitments.append(item)
    q_unshown = q_misattributed = 0
    questions = []
    for question in verdict.open_questions:
        direction = directions.get(question.source_message_id)
        if direction is None:
            q_unshown += 1
            continue
        if _side(direction) != "contact":
            q_misattributed += 1
            continue
        questions.append(question)
    resolved = tuple(i for i in verdict.resolved_task_ids if i in open_ids)
    foreign += len(verdict.resolved_task_ids) - len(resolved)
    checked = replace(
        verdict,
        commitments=tuple(commitments),
        open_questions=tuple(questions),
        resolved_task_ids=resolved,
    )
    return checked, ValidationCounts(
        commitments_kept=len(commitments),
        commitments_unshown=unshown,
        commitments_misattributed=misattributed,
        questions_kept=len(questions),
        questions_unshown=q_unshown,
        questions_misattributed=q_misattributed,
        foreign_task_ids=foreign,
    )


def task_candidates(verdict: SanitizedTriage) -> tuple[TaskCandidate, ...]:
    """The tasks a checked verdict asks for: commitments by side, questions
    as open questions or decisions."""
    found = [
        TaskCandidate(
            kind=tasks.OWNER_COMMITMENT if item.by == "owner" else tasks.CONTACT_COMMITMENT,
            title=item.text,
            due_at=item.due_at,
            source_message_id=item.source_message_id,
            existing_task_id=item.existing_task_id,
        )
        for item in verdict.commitments
    ]
    found += [
        TaskCandidate(
            kind=tasks.DECISION if question.is_decision else tasks.OPEN_QUESTION,
            title=question.text,
            due_at=None,
            source_message_id=question.source_message_id,
            existing_task_id=None,
        )
        for question in verdict.open_questions
    ]
    return tuple(found)


# ------------------------------------------------------------------ the run
class _CallFailedError(Exception):
    def __init__(
        self, status: TriageStatus, run_status: str, error_class: str, *, calls: int = 0
    ) -> None:
        super().__init__(error_class)
        self.status = status
        self.run_status = run_status
        self.error_class = error_class
        self.calls = calls


@dataclass(frozen=True, slots=True)
class _Prepared:
    account_id: int
    run_id: int
    context: TriageContext


class Triager:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        llm: LLMClient,
        clock: Clock,
        config: TriageConfig,
        store: TriageStore,
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm
        self._clock = clock
        self._config = config
        self._store = store

    @property
    def config(self) -> TriageConfig:
        return self._config

    async def triage(self, conversation_id: int) -> TriageOutcome:
        """Triage the conversation's messages its verdict does not cover yet."""
        prompt = prompt_registry.active(RUN_TYPE)
        config = self._config
        async with transaction(self._session_factory) as uow:
            session = uow.session
            ref = await summaries.conversation_ref(session, conversation_id)
            if ref is None:
                return TriageOutcome(TriageStatus.NOT_FOUND)
            account_id = ref.account_id
            if ref.mode == "ignore" or ref.is_bot:
                return TriageOutcome(TriageStatus.SKIPPED, account_id)
            if not await self._managed_ok(session, account_id):
                return TriageOutcome(TriageStatus.NO_CONSENT, account_id)
            if await self._over_budget(session, account_id):
                return TriageOutcome(TriageStatus.OVER_BUDGET, account_id)
            if await self._backed_off(session, account_id, conversation_id):
                return TriageOutcome(TriageStatus.BACKED_OFF, account_id)
            account = await session.get(Account, account_id)
            state = await self._store.state(
                session, account_id=account_id, conversation_id=conversation_id
            )
            if account is None or state is None:
                return TriageOutcome(TriageStatus.NOT_FOUND, account_id)
            source = await burst_source(
                session,
                account_id,
                conversation_id,
                covers_through=state.covers_through_message_id,
            )
            contact_wrote = any(m.direction == "contact" for m in source.burst)
            if source.through is None or not (contact_wrote or state.open_tasks):
                # Nothing a verdict could say: no new message, or only the
                # owner wrote and there is no open task they could answer.
                await self._store.settle(
                    session, account_id=account_id, conversation_id=conversation_id
                )
                return TriageOutcome(TriageStatus.UP_TO_DATE, account_id)
            previous = await summaries.rolling_summary(session, account_id, conversation_id)
            overview = None if previous is None else previous.summary.get("overview")
            context = build_triage(
                prompt=prompt,
                account=account,
                contact_name=await summaries.contact_display_name(
                    session, account_id, conversation_id
                ),
                contact_priority=state.contact_priority,
                notes=await items.contact_facing(
                    session, account_id, conversation_id, include_owner_preferences=True
                ),
                summary_overview=overview if isinstance(overview, str) else None,
                open_tasks=state.open_tasks,
                earlier=source.earlier,
                burst=source.burst,
                omitted=source.omitted,
                now=self._clock.now(),
            )
            timezone = account.timezone
            trigger = f"triage:conv:{conversation_id}:through:{source.through}"
            run_id = await self._start_run(session, account_id, trigger, prompt.label)
        prepared = _Prepared(account_id, run_id, context)
        through = source.through

        try:
            output, calls = await self._call(prepared, prompt)
            sanitized = sanitize_triage(output, timezone=timezone)
            checked, counts = validate_triage(
                sanitized,
                directions=source.directions(),
                open_task_ids=[task.task_id for task in state.open_tasks],
            )
            verdict = TriageVerdict(
                account_id=account_id,
                conversation_id=conversation_id,
                run_id=run_id,
                prompt_version=prompt.label,
                model=config.model,
                source=source.hashes(),
                covers_through_message_id=through,
                burst_message_ids=tuple(m.message_id for m in source.burst),
                needs_reply=checked.needs_reply,
                contains_question=checked.contains_question_to_owner,
                requests_decision=checked.requests_decision,
                urgency=checked.urgency,
                deadline_at=checked.deadline_at,
                escalation_category=checked.escalation_category,
                reasons=checked.reasons,
                language=checked.language or None,
                script=checked.script,
                tasks=task_candidates(checked),
                resolved_task_ids=checked.resolved_task_ids,
            )
            async with transaction(self._session_factory) as uow:
                result = await self._store.apply(uow.session, verdict, now=self._clock.now())
                await self._close_run(uow.session, prepared, *_run_close(result))
        except _CallFailedError as failure:
            await self._finish_run(prepared, failure.run_status, failure.error_class)
            return TriageOutcome(failure.status, account_id, calls=failure.calls, through=through)
        except BaseException as error:
            await self._fail_run(prepared, error)
            raise
        logger.info(
            "triage_validated",
            conversation_id=conversation_id,
            run_id=run_id,
            messages=len(source.burst),
            commitments_kept=counts.commitments_kept,
            commitments_unshown=counts.commitments_unshown,
            commitments_misattributed=counts.commitments_misattributed,
            questions_kept=counts.questions_kept,
            questions_unshown=counts.questions_unshown,
            questions_misattributed=counts.questions_misattributed,
            foreign_task_ids=counts.foreign_task_ids,
            items_capped=sanitized.capped,
            items_empty=sanitized.empty,
            result=result.value,
        )
        if result.discarded:
            return TriageOutcome(
                TriageStatus.DISCARDED, account_id, calls, through, discard_reason=result
            )
        if result is ApplyResult.RACED:
            return TriageOutcome(TriageStatus.RACED, account_id, calls, through)
        return TriageOutcome(TriageStatus.TRIAGED, account_id, calls, through)

    # ------------------------------------------------------------ the call
    async def _call(self, prepared: _Prepared, prompt: Prompt) -> tuple[TriageResult, int]:
        """One structured call — no tools, ever (§6 invariant 3a) — retried
        once with a larger output budget if truncated, never continued by
        prefilling (AGENTS §8). Stop reason first, then content."""
        config = self._config
        request = LLMRequest(
            account_id=prepared.account_id,
            run_id=prepared.run_id,
            purpose=RUN_TYPE,
            model=config.model,
            instructions=prepared.context.instructions,
            items=prepared.context.items,
            tools=(),
            effort=config.effort,
            max_output_tokens=config.max_output_tokens,
            timeout_seconds=config.call_timeout_seconds,
            prompt_cache_key=f"{RUN_TYPE}:{prompt.label}",
        )
        calls = 0

        def failed(status: TriageStatus, run_status: str, error_class: str) -> _CallFailedError:
            return _CallFailedError(status, run_status, error_class, calls=calls)

        try:
            response = await self._llm.parse(request, TriageResult)
            calls += 1
            if response.stop_reason is StopReason.MAX_TOKENS:
                larger = min(request.max_output_tokens * 2, config.max_output_tokens_ceiling)
                if larger > request.max_output_tokens:
                    response = await self._llm.parse(
                        replace(request, max_output_tokens=larger), TriageResult
                    )
                    calls += 1
        except ConsentMissingError:
            raise failed(TriageStatus.NO_CONSENT, "failed", "consent_missing") from None
        except LLMError as error:
            kind = "invalid_output" if error.kind is LLMErrorKind.INVALID_OUTPUT else None
            raise failed(TriageStatus.FAILED, "failed", kind or f"llm_{error.kind.value}") from None
        match response.stop_reason:
            case StopReason.COMPLETED:
                if response.parsed is None:
                    raise failed(TriageStatus.FAILED, "failed", "invalid_output")
                try:
                    # Our own model again: the adapter's validation is not the contract.
                    validated = TriageResult.model_validate(response.parsed.model_dump())
                except pydantic.ValidationError:
                    raise failed(TriageStatus.FAILED, "failed", "invalid_output") from None
                return validated, calls
            case StopReason.REFUSAL | StopReason.CONTENT_FILTER:
                raise failed(TriageStatus.REFUSED, "refused", response.stop_reason.value)
            case StopReason.MAX_TOKENS:
                raise failed(TriageStatus.FAILED, "failed", "max_tokens")
            case _:
                raise failed(TriageStatus.FAILED, "failed", "stop_other")

    # ------------------------------------------------------------ checks
    async def _managed_ok(self, session: AsyncSession, account_id: int) -> bool:
        """`ai_control` and `ai_managed` both active and both naming the
        processor that serves this route's model (§14.3, M03 D48)."""
        processor = processor_for_model(self._config.model)
        if processor is None:
            return False
        for kind in (CONSENT_AI_CONTROL, CONSENT_AI_MANAGED):
            state = await active_consent(session, account_id, kind=kind)
            if not state.granted or processor not in state.processors:
                return False
        return True

    async def _over_budget(self, session: AsyncSession, account_id: int) -> bool:
        limit = self._config.monthly_budget_usd
        if limit is None:
            return False
        spent = await LlmCallRepository(session, account_id).spend_since(
            month_start(self._clock.now())
        )
        return spent >= limit

    async def _backed_off(
        self, session: AsyncSession, account_id: int, conversation_id: int
    ) -> bool:
        """The last `failure_backoff_runs` triage runs of this chat within the
        window all failed in a way a retry would repeat."""
        config = self._config
        rows = (
            await session.execute(
                select(AgentRun.error_class)
                .where(AgentRun.account_id == account_id)
                .where(AgentRun.run_type == RUN_TYPE)
                .where(AgentRun.trigger_ref.like(f"triage:conv:{conversation_id}:%"))
                .where(AgentRun.started_at >= self._clock.now() - config.failure_backoff_window)
                .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
                .limit(config.failure_backoff_runs)
            )
        ).all()
        if len(rows) < config.failure_backoff_runs:
            return False
        return all(error in _REPEATING_ERRORS for (error,) in rows)

    # ------------------------------------------------------------ run rows
    async def _start_run(
        self, session: AsyncSession, account_id: int, trigger: str, prompt_label: str
    ) -> int:
        run = AgentRunRepository(session, account_id).add(
            AgentRun(
                run_type=RUN_TYPE,
                trigger_ref=trigger,
                model=self._config.model,
                effort=self._config.effort,
                prompt_version=prompt_label,
                status="running",
                started_at=self._clock.now(),
            )
        )
        await session.flush()
        return run.id

    async def _close_run(
        self, session: AsyncSession, prepared: _Prepared, status: str, error_class: str | None
    ) -> None:
        run = await AgentRunRepository(session, prepared.account_id).get(prepared.run_id)
        if run is not None:
            run.status = status
            run.iterations = 1
            run.error_class = error_class
            run.finished_at = self._clock.now()

    async def _finish_run(self, prepared: _Prepared, status: str, error_class: str) -> None:
        logger.warning(
            "triage_run_not_succeeded",
            account_id=prepared.account_id,
            run_id=prepared.run_id,
            status=status,
            error_class=error_class,
        )
        async with transaction(self._session_factory) as uow:
            await self._close_run(uow.session, prepared, status, error_class)

    async def _fail_run(self, prepared: _Prepared, error: BaseException) -> None:
        """An unexpected error or a cancellation: the run must not stay
        `running`. Best effort; the original error still propagates."""
        try:
            await self._finish_run(prepared, "failed", f"exception:{type(error).__name__}")
        except Exception as close_error:
            logger.error(
                "triage_run_close_failed",
                run_id=prepared.run_id,
                exc_type=type(close_error).__name__,
            )


def _run_close(result: ApplyResult) -> tuple[str, str | None]:
    """How a run that got an answer is closed, by what the store did."""
    # A raced verdict is not stored either: only stored verdicts count as
    # succeeded (NFR-05 measures triage latency over them).
    if result is ApplyResult.STORED:
        return "succeeded", None
    return "failed", f"discarded:{result.value}"
