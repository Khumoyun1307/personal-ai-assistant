"""The owned agent loop for `owner_request` runs (ARCHITECTURE §7.2, ADR-07a).

One owner control-chat turn in, one reply out. The loop is small and explicit
so that it can stay provider-neutral, persist every step, enforce budgets and
scope tools per run:

    for each iteration (bounded):
        typing indicator → LLM call → check stop reason
        refusal / filtered       → safe reply
        truncated                → retry once with a larger budget, else fail
        tool calls               → execute all, append all results, continue
        text                     → reply
    budget or deadline exhausted → partial reply with a note

Messages within a run are append-only: the model's output is passed back
unchanged. Across runs, context is rebuilt from `control_turns`.

Consent is checked here for a friendly answer, and again — as the wall — by
the consent wrapper around the LLM client on every call (§6 invariant 9).
Nothing is held in a database transaction across an LLM or Telegram call.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, ControlTurn
from assistant.accounts.service import (
    CONSENT_AI_CONTROL,
    CONSENT_AI_MANAGED,
    ConsentMissingError,
    active_consent,
)
from assistant.agent import prompt_registry
from assistant.agent.context import OWNER_HISTORY_TURNS, build_owner_request
from assistant.agent.models import AgentRun
from assistant.agent.persistence import (
    AgentRunRepository,
    ControlTurnRepository,
    LlmCallRepository,
    account_of_turn,
    month_start,
)
from assistant.agent.ports import ControlChannel
from assistant.agent.tools.executor import StepRecorder, ToolExecutor
from assistant.agent.tools.registry import (
    CapabilityTier,
    ToolContext,
    ToolRegistry,
    ToolServices,
)
from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.llm.port import (
    InputItem,
    LLMClient,
    LLMError,
    LLMErrorKind,
    LLMRequest,
    LLMResponse,
    Provenance,
    StopReason,
)
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

RUN_TYPE = "owner_request"


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Budgets and model settings for owner runs (ARCHITECTURE §7.2, §7.4)."""

    model: str
    effort: str | None = "low"
    max_output_tokens: int = 2000
    max_output_tokens_ceiling: int = 8000
    max_iterations: int = 8
    run_token_budget: int = 60_000
    run_timeout: timedelta = timedelta(minutes=5)
    call_timeout_seconds: float = 120.0
    monthly_budget_usd: Decimal | None = None
    enabled_flags: frozenset[str] = frozenset()


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUSED = "refused"
    BUDGET_EXCEEDED = "budget_exceeded"
    SKIPPED = "skipped"
    """Nothing to do, or nobody to reply to; no run row is written."""


@dataclass(frozen=True, slots=True)
class RunOutcome:
    status: RunStatus
    run_id: int | None = None
    reply_message_id: int | None = None
    note: str | None = None


# Owner-facing texts. Each says plainly what happened (§6 invariant 11).
TEXT_NEEDS_CONSENT = (
    "I cannot answer with AI until you agree to it. Send /start to see what that means."
)
TEXT_BUDGET_REACHED = (
    "This month's AI budget is used up, so I am not calling the model. "
    "/status and /settings still work."
)
TEXT_PROVIDER_DOWN = (
    "I cannot reach the AI provider right now, so I could not answer. "
    "Please try again in a few minutes. /status still works."
)
TEXT_INTERNAL_ERROR = (
    "Something went wrong on my side and I could not answer. The problem has been logged."
)
TEXT_REFUSED = "I can't help with that request."
TEXT_TOO_LONG = "My answer would have been too long. Could you ask for something narrower?"
TEXT_LIMITS = (
    "I stopped before finishing because this request hit my time or step limit. "
    "Try a narrower question."
)
TEXT_EMPTY = "I don't have an answer to that."


@dataclass
class _RunState:
    account_id: int
    chat_id: int
    run_id: int
    items: list[InputItem]
    instructions: str
    iterations: int = 0
    tokens: int = 0
    #: Whether anything managed reached the model; the reply inherits it.
    used_managed: bool = False


@dataclass(frozen=True, slots=True)
class _Started:
    account_id: int
    chat_id: int
    no_consent: bool = False
    state: _RunState | None = None
    managed_consent: bool = False
    over_budget: bool = False


class OwnerRequestRuntime:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        llm: LLMClient,
        channel: ControlChannel,
        registry: ToolRegistry,
        clock: Clock,
        config: AgentConfig,
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm
        self._channel = channel
        self._registry = registry
        self._clock = clock
        self._config = config

    async def run(self, turn_id: int) -> RunOutcome:
        prompt = prompt_registry.active(RUN_TYPE)
        started = await self._start(turn_id, prompt)
        if isinstance(started, RunOutcome):
            return started
        if started.no_consent:
            # No run row and no model call: there is nothing to account for.
            await self._channel.send(
                account_id=started.account_id, chat_id=started.chat_id, text=TEXT_NEEDS_CONSENT
            )
            return RunOutcome(RunStatus.SKIPPED, note="no_consent")
        state = started.state
        assert state is not None  # set whenever consent is present
        if started.over_budget:
            return await self._finish(
                state, TEXT_BUDGET_REACHED, RunStatus.BUDGET_EXCEEDED, "monthly_budget"
            )

        offered = self._registry.for_run(
            RUN_TYPE,
            tier=CapabilityTier.CONTROL_ONLY,
            enabled_flags=self._config.enabled_flags,
            managed_consent=started.managed_consent,
        )
        executor = ToolExecutor(
            context=ToolContext(
                account_id=state.account_id,
                run_id=state.run_id,
                run_type=RUN_TYPE,
                tainted=False,
                services=ToolServices(
                    session_factory=self._session_factory,
                    clock=self._clock,
                    monthly_budget_usd=self._config.monthly_budget_usd,
                ),
            ),
            offered=offered,
            record_step=self._step_recorder(state.account_id, state.run_id),
            clock=self._clock,
        )
        return await self._loop(state, executor, prompt.label)

    async def _start(self, turn_id: int, prompt: prompt_registry.Prompt) -> _Started | RunOutcome:
        """Load the turn, check consent and budget, create the run row. One transaction."""
        async with transaction(self._session_factory) as uow:
            session = uow.session
            account_id = await account_of_turn(session, turn_id)
            if account_id is None:
                return RunOutcome(RunStatus.SKIPPED, note="turn_not_found")
            turn = await ControlTurnRepository(session, account_id).get(turn_id)
            account = await session.get(Account, account_id)
            if turn is None or account is None or turn.role != "owner":
                return RunOutcome(RunStatus.SKIPPED, note="not_an_owner_turn")
            if account.control_chat_id is None:
                return RunOutcome(RunStatus.SKIPPED, note="no_control_chat")
            chat_id = account.control_chat_id

            control = await active_consent(session, account_id, kind=CONSENT_AI_CONTROL)
            if not control.granted:
                return _Started(account_id, chat_id, no_consent=True)
            managed = await active_consent(session, account_id, kind=CONSENT_AI_MANAGED)
            over_budget = await self._over_budget(session, account_id)
            run = AgentRunRepository(session, account_id).add(
                AgentRun(
                    run_type=RUN_TYPE,
                    trigger_ref=f"turn:{turn_id}",
                    model=self._config.model,
                    effort=self._config.effort,
                    prompt_version=prompt.label,
                    status="running",
                    started_at=self._clock.now(),
                )
            )
            await session.flush()
            history = await ControlTurnRepository(session, account_id).recent_before(
                turn_id, limit=OWNER_HISTORY_TURNS, include_managed=managed.granted
            )
            context = build_owner_request(
                prompt=prompt, account=account, history=history, turn=turn, now=self._clock.now()
            )
            state = _RunState(
                account_id=account_id,
                chat_id=chat_id,
                run_id=run.id,
                items=list(context.items),
                instructions=context.instructions,
                used_managed=any(item.provenance is Provenance.MANAGED for item in context.items),
            )
            return _Started(
                account_id,
                chat_id,
                state=state,
                managed_consent=managed.granted,
                over_budget=over_budget,
            )

    async def _loop(
        self, state: _RunState, executor: ToolExecutor, prompt_label: str
    ) -> RunOutcome:
        deadline = self._clock.now() + self._config.run_timeout
        tools = tuple(tool.spec() for tool in executor.offered.values())

        while state.iterations < self._config.max_iterations:
            remaining = (deadline - self._clock.now()).total_seconds()
            if remaining <= 0 or state.tokens >= self._config.run_token_budget:
                break
            state.iterations += 1
            await self._channel.typing(account_id=state.account_id, chat_id=state.chat_id)
            request = LLMRequest(
                account_id=state.account_id,
                run_id=state.run_id,
                purpose=RUN_TYPE,
                model=self._config.model,
                instructions=state.instructions,
                items=tuple(state.items),
                tools=tools,
                effort=self._config.effort,
                max_output_tokens=self._config.max_output_tokens,
                timeout_seconds=min(self._config.call_timeout_seconds, remaining),
                prompt_cache_key=f"{RUN_TYPE}:{prompt_label}",
            )
            try:
                response = await self._call(request, state)
            except ConsentMissingError:
                return await self._finish(
                    state, TEXT_NEEDS_CONSENT, RunStatus.FAILED, "consent_missing"
                )
            except LLMError as error:
                text = (
                    TEXT_PROVIDER_DOWN
                    if error.kind in {LLMErrorKind.UNAVAILABLE, LLMErrorKind.RATE_LIMITED}
                    else TEXT_INTERNAL_ERROR
                )
                return await self._finish(state, text, RunStatus.FAILED, f"llm_{error.kind.value}")

            match response.stop_reason:
                case StopReason.REFUSAL | StopReason.CONTENT_FILTER:
                    return await self._finish(
                        state, TEXT_REFUSED, RunStatus.REFUSED, response.stop_reason.value
                    )
                case StopReason.MAX_TOKENS:
                    return await self._finish(state, TEXT_TOO_LONG, RunStatus.FAILED, "max_tokens")
                case StopReason.OTHER:
                    return await self._finish(
                        state, TEXT_INTERNAL_ERROR, RunStatus.FAILED, "stop_other"
                    )
                case StopReason.TOOL_CALLS:
                    if response.output_items is not None:
                        state.items.append(response.output_items)
                    # All results of one turn go back together, before the next call.
                    results = await executor.execute_all(response.tool_calls)
                    state.items.extend(results)
                    if any(result.provenance is Provenance.MANAGED for result in results):
                        state.used_managed = True
                case StopReason.COMPLETED:
                    text = (response.text or "").strip() or TEXT_EMPTY
                    return await self._finish(state, text, RunStatus.SUCCEEDED)

        return await self._finish(state, TEXT_LIMITS, RunStatus.FAILED, "run_limits")

    async def _call(self, request: LLMRequest, state: _RunState) -> LLMResponse:
        """One call, retried once with a larger output budget if truncated (AGENTS §8).

        Never continued by prefilling an assistant turn: the retry is the same
        request with more room, or the run fails.
        """
        response = await self._llm.complete(request)
        state.tokens += response.usage.total
        if response.stop_reason is StopReason.MAX_TOKENS:
            larger = min(request.max_output_tokens * 2, self._config.max_output_tokens_ceiling)
            if larger > request.max_output_tokens:
                response = await self._llm.complete(replace(request, max_output_tokens=larger))
                state.tokens += response.usage.total
        return response

    async def _over_budget(self, session: AsyncSession, account_id: int) -> bool:
        limit = self._config.monthly_budget_usd
        if limit is None:
            return False
        spent = await LlmCallRepository(session, account_id).spend_since(
            month_start(self._clock.now())
        )
        return spent >= limit

    def _step_recorder(self, account_id: int, run_id: int) -> StepRecorder:
        async def record(tool_name: str, digest: str | None, status: str, latency_ms: int) -> None:
            async with transaction(self._session_factory) as uow:
                await AgentRunRepository(uow.session, account_id).add_step(
                    run_id,
                    tool_name=tool_name,
                    args_digest=digest,
                    status=status,
                    latency_ms=latency_ms,
                )

        return record

    async def _finish(
        self,
        state: _RunState,
        text: str,
        status: RunStatus,
        error_class: str | None = None,
    ) -> RunOutcome:
        """Reply to the owner, then close the run and record the assistant turn."""
        if error_class is not None and status is not RunStatus.SUCCEEDED:
            logger.warning(
                "agent_run_not_succeeded",
                account_id=state.account_id,
                run_id=state.run_id,
                status=status.value,
                error_class=error_class,
            )
        delivery = await self._channel.send(
            account_id=state.account_id, chat_id=state.chat_id, text=text
        )
        async with transaction(self._session_factory) as uow:
            run = await AgentRunRepository(uow.session, state.account_id).get(state.run_id)
            if run is not None:
                run.status = status.value
                run.iterations = state.iterations
                run.finished_at = self._clock.now()
                run.error_class = error_class or (None if delivery.ok else "reply_not_delivered")
            ControlTurnRepository(uow.session, state.account_id).add(
                ControlTurn(
                    role="assistant",
                    text=text,
                    telegram_message_id=delivery.message_id,
                    # A reply built from managed content is itself managed
                    # content: excluded later if `ai_managed` is revoked.
                    contains_managed=state.used_managed,
                    run_id=state.run_id,
                )
            )
        logger.info(
            "agent_run_finished",
            account_id=state.account_id,
            run_id=state.run_id,
            status=status.value,
            iterations=state.iterations,
            delivered=delivery.ok,
        )
        return RunOutcome(status, state.run_id, delivery.message_id)
