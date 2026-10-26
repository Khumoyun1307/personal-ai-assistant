"""The `summarize` run type (ARCHITECTURE §7.1, §7.2, §11.1; S03.2).

A contact-triggered run type: its input is untrusted chat content, so it
has **no tools** (§6 invariant 3a, RG-17) — one structured-output call
through `LLMClient.parse`, the stop reason checked before anything is read,
the output re-validated by our own model and then `sanitize`d (an item that
cites no shown message is dropped) and attribution-checked against the cited
messages' authors (`agent/attribution.py`, S03.7: a "promise" citing only the
other side is stored as that side's request). Invalid, truncated or refused
output stores nothing.

Two entry points, each about ONE conversation (§6 invariant 4):

- `refresh(conversation_id)` — the rolling summary, incrementally: the
  previous summary plus the messages after its `covers_through_message_id`
  (in chunks). Run by `memory.refresh_summary` and, for a stale chat, by the
  attention list (one call at most there, D62).
- `summarize_period(...)` — one chat's messages in a catch-up range, cached
  in `conversation_summaries` (kind `period`) while the range's messages,
  the prompt version and the model are unchanged.

**Nothing deleted is stored.** The store re-checks, under the §9.4 locks,
that every message the model read is still retained with the same content
and that the chat may still be summarized (`memory/summaries.py`); if a
deletion, an edit, managed-data deletion or a bot-chat purge committed while
the model ran, the result is discarded (`DISCARDED`) — not stored, not
returned (§6 invariant 9, PRD §20, D63, D66).

Consent is checked here for a clean skip (no run row, no call) — before a
cached period summary is served too — and again, as the wall, by the consent
wrapper on the call itself: a job queued before a revocation stops when it
runs. Every call has an `agent_runs` row (prompt version, model), closed on
every path, including an unexpected error or a cancellation. No transaction
is open across the model call (AGENTS §13).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

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
from assistant.agent.attribution import NO_CHANGES, AttributionCounts, check_attribution
from assistant.agent.context import SummarizeContext, build_summarize
from assistant.agent.models import AgentRun
from assistant.agent.persistence import AgentRunRepository, LlmCallRepository, month_start
from assistant.agent.prompt_registry import Prompt
from assistant.agent.schemas import SummaryOutput, cited_ids, output_model_for, sanitize
from assistant.core.clock import Clock
from assistant.core.model_routes import processor_for_model
from assistant.db.uow import transaction
from assistant.llm.port import LLMClient, LLMError, LLMErrorKind, LLMRequest, StopReason
from assistant.memory import items, summaries
from assistant.memory.summaries import StoreResult
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

RUN_TYPE = "summarize"

#: Errors a retry would only repeat at a cost: after enough of them in a row,
#: background refreshes of that chat pause for a while.
_REPEATING_ERRORS = frozenset(
    {"refusal", "content_filter", "invalid_output", "max_tokens", "stop_other"}
)


@dataclass(frozen=True, slots=True)
class SummarizeConfig:
    """Model and budgets of the `summarize` route (ARCHITECTURE §7.4)."""

    model: str
    effort: str | None = "low"
    max_output_tokens: int = 2000
    max_output_tokens_ceiling: int = 4000
    call_timeout_seconds: float = 90.0
    #: New messages per incremental call, and calls per background refresh.
    messages_per_call: int = 200
    max_calls_per_refresh: int = 5
    #: Messages of one chat a period summary reads (the newest, if more).
    period_message_limit: int = 300
    monthly_budget_usd: Decimal | None = None
    #: This many repeating failures in a row within the window pause the
    #: background refresh of that chat (no paid call that would fail again).
    failure_backoff_runs: int = 3
    failure_backoff_window: timedelta = timedelta(hours=24)


class SummaryStatus(StrEnum):
    REFRESHED = "refreshed"
    UP_TO_DATE = "up_to_date"
    SUMMARIZED = "summarized"
    CACHED = "cached"
    EMPTY = "empty"
    NO_CONSENT = "no_consent"
    OVER_BUDGET = "over_budget"
    BACKED_OFF = "backed_off"
    REFUSED = "refused"
    FAILED = "failed"
    RACED = "raced"
    """Another refresh stored first, or the summary was invalidated meanwhile."""
    DISCARDED = "discarded"
    """A message the model read was deleted or edited, or the chat was
    purged or set to Ignore while it ran: nothing stored or returned."""
    SKIPPED = "skipped"
    """Ignore mode or a bot chat: never summarized."""
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    status: SummaryStatus
    account_id: int | None = None
    #: Model calls made (for the budget alerts after a job).
    calls: int = 0
    #: More unseen messages remain than one refresh processes.
    more_pending: bool = False
    #: DISCARDED only: why (changed, expired, ignored, bot_chat).
    discard_reason: StoreResult | None = None


@dataclass(frozen=True, slots=True)
class PeriodOutcome:
    status: SummaryStatus
    summary: Mapping[str, Any] | None = None
    #: Retained messages in the range, and how many the summary read.
    message_count: int = 0
    summarized_count: int = 0
    calls: int = 0
    #: DISCARDED only: why (changed, expired, ignored, bot_chat).
    discard_reason: StoreResult | None = None
    #: SUMMARIZED only: what the attribution check changed (S03.7; counts).
    attribution: AttributionCounts = NO_CHANGES


class _CallFailedError(Exception):
    def __init__(
        self, status: SummaryStatus, run_status: str, error_class: str, *, calls: int = 0
    ) -> None:
        super().__init__(error_class)
        self.status = status
        self.run_status = run_status
        self.error_class = error_class
        #: Model calls that returned before the failure (for budget alerts).
        self.calls = calls


@dataclass(frozen=True, slots=True)
class _Prepared:
    account_id: int
    run_id: int
    context: SummarizeContext


class Summarizer:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        llm: LLMClient,
        clock: Clock,
        config: SummarizeConfig,
    ) -> None:
        self._session_factory = session_factory
        self._llm = llm
        self._clock = clock
        self._config = config

    @property
    def config(self) -> SummarizeConfig:
        return self._config

    # ------------------------------------------------------------ rolling
    async def refresh(
        self, conversation_id: int, *, max_calls: int | None = None
    ) -> RefreshOutcome:
        """Bring the rolling summary up to the newest retained message, in at
        most `max_calls` incremental calls (default `max_calls_per_refresh`;
        each may be retried once if truncated)."""
        calls = 0
        outcome = RefreshOutcome(SummaryStatus.NOT_FOUND)
        for _ in range(max_calls or self._config.max_calls_per_refresh):
            outcome = await self._refresh_once(conversation_id)
            calls += outcome.calls
            if outcome.status is not SummaryStatus.REFRESHED or not outcome.more_pending:
                break
        return replace(outcome, calls=calls)

    async def _refresh_once(self, conversation_id: int) -> RefreshOutcome:
        prompt = prompt_registry.active(RUN_TYPE)
        config = self._config
        trigger = f"summary:conv:{conversation_id}"
        async with transaction(self._session_factory) as uow:
            session = uow.session
            ref = await summaries.conversation_ref(session, conversation_id)
            if ref is None:
                return RefreshOutcome(SummaryStatus.NOT_FOUND)
            account_id = ref.account_id
            if ref.mode == "ignore" or ref.is_bot:
                return RefreshOutcome(SummaryStatus.SKIPPED, account_id)
            if not await self._managed_ok(session, account_id):
                return RefreshOutcome(SummaryStatus.NO_CONSENT, account_id)
            if await self._over_budget(session, account_id):
                return RefreshOutcome(SummaryStatus.OVER_BUDGET, account_id)
            if await self._backed_off(session, account_id, trigger):
                return RefreshOutcome(SummaryStatus.BACKED_OFF, account_id)
            account = await session.get(Account, account_id)
            if account is None:  # pragma: no cover - the FK guarantees it
                return RefreshOutcome(SummaryStatus.NOT_FOUND)
            previous = await summaries.rolling_summary(session, account_id, conversation_id)
            after = None if previous is None else previous.covers_through_message_id
            marker = await summaries.activity_marker(session, account_id, conversation_id)
            batch = await summaries.messages_after(
                session,
                account_id,
                conversation_id,
                after_id=after,
                limit=config.messages_per_call + 1,
            )
            more = len(batch) > config.messages_per_call
            batch = batch[: config.messages_per_call]
            if not batch:
                # Nothing unseen (an edit or a deletion set the flag).
                await summaries.settle_refresh_flag(
                    session, account_id=account_id, conversation_id=conversation_id, marker=marker
                )
                return RefreshOutcome(SummaryStatus.UP_TO_DATE, account_id)
            context = build_summarize(
                prompt=prompt,
                account=account,
                messages=batch,
                previous=None if previous is None else previous.summary,
                notes=await _notes(session, account_id, conversation_id),
                contact_name=await summaries.contact_display_name(
                    session, account_id, conversation_id
                ),
            )
            carried = (
                cited_ids(dict(previous.summary)) if previous is not None else frozenset[int]()
            )
            # Who wrote what the model may cite (S03.7): the shown messages, and
            # the earlier ones its previous summary cites, from the database.
            authors = await summaries.message_directions(
                session, account_id, conversation_id, carried - context.shown_ids
            )
            authors.update((m.message_id, m.direction) for m in batch)
            run_id = await self._start_run(session, account_id, trigger, prompt.label)
        prepared = _Prepared(account_id, run_id, context)

        try:
            output, calls = await self._call(prepared, prompt)
            sanitized = sanitize(output, context.shown_ids | carried)
            uncited, capped = sanitized.uncited, sanitized.capped
            clean, attribution = check_attribution(sanitized.summary, authors)
            async with transaction(self._session_factory) as uow:
                result = await summaries.store_rolling(
                    uow.session,
                    account_id=account_id,
                    conversation_id=conversation_id,
                    based_on=None if previous is None else previous.version,
                    source={m.message_id: m.content_hash for m in batch},
                    summary=clean,
                    covers_through=max(m.message_id for m in batch),
                    added_messages=len(batch),
                    prompt_version=prompt.label,
                    model=config.model,
                    now=self._clock.now(),
                )
                if result is StoreResult.STORED and not more:
                    await summaries.settle_refresh_flag(
                        uow.session,
                        account_id=account_id,
                        conversation_id=conversation_id,
                        marker=marker,
                    )
                await self._close_run(uow.session, prepared, *_run_close(result))
        except _CallFailedError as failure:
            await self._finish_run(prepared, failure.run_status, failure.error_class)
            return RefreshOutcome(failure.status, account_id, calls=failure.calls)
        except BaseException as error:
            await self._fail_run(prepared, error)
            raise
        logger.info(
            "summary_refreshed",
            conversation_id=conversation_id,
            run_id=run_id,
            messages=len(batch),
            items_uncited=uncited,
            items_capped=capped,
            items_demoted=attribution.demoted,
            items_misattributed=attribution.dropped,
            result=result.value,
            more_pending=more,
        )
        if result.discarded:
            status = SummaryStatus.DISCARDED
        elif result is StoreResult.RACED:
            status = SummaryStatus.RACED
        else:
            status = SummaryStatus.REFRESHED
        return RefreshOutcome(
            status,
            account_id,
            calls=calls,
            discard_reason=result if result.discarded else None,
            more_pending=more and result is StoreResult.STORED,
        )

    # ------------------------------------------------------------- period
    async def summarize_period(
        self,
        *,
        account_id: int,
        conversation_id: int,
        start: datetime,
        end: datetime,
        open_ended: bool,
        parent_run_id: int | None,
    ) -> PeriodOutcome:
        """One chat's summary for `[start, end)`. `open_ended` (until now)
        caches under a NULL end, reused while no message in the range changes.
        The caller has checked that the conversation is this account's."""
        prompt = prompt_registry.active(RUN_TYPE)
        config = self._config
        end_key = None if open_ended else end
        async with transaction(self._session_factory) as uow:
            session = uow.session
            account = await session.get(Account, account_id)
            if account is None:
                return PeriodOutcome(SummaryStatus.NOT_FOUND)
            # Consent before anything, the cache included: a stored summary is
            # managed content too (§14.3).
            if not await self._managed_ok(session, account_id):
                return PeriodOutcome(SummaryStatus.NO_CONSENT)
            # Like a refresh: an Ignore-mode or bot chat is never summarized,
            # whatever the caller saw a moment ago.
            ref = await summaries.conversation_ref(session, conversation_id)
            if ref is None or ref.account_id != account_id:
                return PeriodOutcome(SummaryStatus.NOT_FOUND)
            if ref.mode == "ignore" or ref.is_bot:
                return PeriodOutcome(SummaryStatus.SKIPPED)
            source = await summaries.period_source(
                session,
                account_id,
                conversation_id,
                start=start,
                end=end,
                limit=config.period_message_limit,
            )
            if source.total == 0 or source.digest is None:
                return PeriodOutcome(SummaryStatus.EMPTY)
            cached = await summaries.cached_period(
                session,
                account_id,
                conversation_id,
                start=start,
                end=end_key,
                digest=source.digest,
                prompt_version=prompt.label,
                model=config.model,
            )
            if cached is not None:
                return PeriodOutcome(
                    SummaryStatus.CACHED,
                    cached.summary,
                    source.total,
                    cached.message_count,
                )
            if await self._over_budget(session, account_id):
                return PeriodOutcome(SummaryStatus.OVER_BUDGET, message_count=source.total)
            context = build_summarize(
                prompt=prompt,
                account=account,
                messages=source.messages,
                period=(start, end),
                notes=await _notes(session, account_id, conversation_id),
                contact_name=await summaries.contact_display_name(
                    session, account_id, conversation_id
                ),
            )
            trigger = f"catch_up:run:{parent_run_id}:conv:{conversation_id}"
            run_id = await self._start_run(session, account_id, trigger, prompt.label)
        prepared = _Prepared(account_id, run_id, context)
        authors = {m.message_id: m.direction for m in source.messages}

        try:
            output, calls = await self._call(prepared, prompt)
            sanitized = sanitize(output, context.shown_ids)
            uncited, capped = sanitized.uncited, sanitized.capped
            clean, attribution = check_attribution(sanitized.summary, authors)
            async with transaction(self._session_factory) as uow:
                result = await summaries.store_period(
                    uow.session,
                    account_id=account_id,
                    conversation_id=conversation_id,
                    start=start,
                    source_end=end,
                    end=end_key,
                    digest=source.digest,
                    source={m.message_id: m.content_hash for m in source.messages},
                    summary=clean,
                    covers_through=source.covers_through_message_id,
                    message_count=len(source.messages),
                    prompt_version=prompt.label,
                    model=config.model,
                    now=self._clock.now(),
                )
                await self._close_run(uow.session, prepared, *_run_close(result))
        except _CallFailedError as failure:
            await self._finish_run(prepared, failure.run_status, failure.error_class)
            return PeriodOutcome(failure.status, message_count=source.total, calls=failure.calls)
        except BaseException as error:
            await self._fail_run(prepared, error)
            raise
        logger.info(
            "period_summarized",
            conversation_id=conversation_id,
            run_id=run_id,
            messages=len(source.messages),
            items_uncited=uncited,
            items_capped=capped,
            items_demoted=attribution.demoted,
            items_misattributed=attribution.dropped,
            result=result.value,
        )
        if result is not StoreResult.STORED:
            # Written from messages that changed meanwhile: never shown.
            return PeriodOutcome(
                SummaryStatus.DISCARDED,
                message_count=source.total,
                calls=calls,
                discard_reason=result if result.discarded else StoreResult.CHANGED,
            )
        return PeriodOutcome(
            SummaryStatus.SUMMARIZED,
            clean,
            source.total,
            len(source.messages),
            calls,
            attribution=attribution,
        )

    # ------------------------------------------------------------ the call
    async def _call(self, prepared: _Prepared, prompt: Prompt) -> tuple[SummaryOutput, int]:
        """One structured call — no tools, ever (§6 invariant 3a) — retried
        once with a larger output budget if truncated, never continued by
        prefilling (AGENTS §8). Stop reason first, then content. The schema
        is the one the prompt version was written for (`output_model_for`)."""
        config = self._config
        schema = output_model_for(prompt)
        prompt_label = prompt.label
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
            prompt_cache_key=f"{RUN_TYPE}:{prompt_label}",
        )
        #: Calls that returned (the ones metered and paid for), carried on a
        #: failure too — a call truncated twice is two calls, not one.
        calls = 0

        def failed(status: SummaryStatus, run_status: str, error_class: str) -> _CallFailedError:
            return _CallFailedError(status, run_status, error_class, calls=calls)

        try:
            response = await self._llm.parse(request, schema)
            calls += 1
            if response.stop_reason is StopReason.MAX_TOKENS:
                larger = min(request.max_output_tokens * 2, config.max_output_tokens_ceiling)
                if larger > request.max_output_tokens:
                    response = await self._llm.parse(
                        replace(request, max_output_tokens=larger), schema
                    )
                    calls += 1
        except ConsentMissingError:
            raise failed(SummaryStatus.NO_CONSENT, "failed", "consent_missing") from None
        except LLMError as error:
            kind = "invalid_output" if error.kind is LLMErrorKind.INVALID_OUTPUT else None
            raise failed(
                SummaryStatus.FAILED, "failed", kind or f"llm_{error.kind.value}"
            ) from None
        match response.stop_reason:
            case StopReason.COMPLETED:
                if response.parsed is None:
                    raise failed(SummaryStatus.FAILED, "failed", "invalid_output")
                try:
                    # Our own model again: the adapter's validation is not the contract.
                    validated = schema.model_validate(response.parsed.model_dump())
                except pydantic.ValidationError:
                    raise failed(SummaryStatus.FAILED, "failed", "invalid_output") from None
                return validated, calls
            case StopReason.REFUSAL | StopReason.CONTENT_FILTER:
                raise failed(SummaryStatus.REFUSED, "refused", response.stop_reason.value)
            case StopReason.MAX_TOKENS:
                raise failed(SummaryStatus.FAILED, "failed", "max_tokens")
            case _:
                raise failed(SummaryStatus.FAILED, "failed", "stop_other")

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

    async def _backed_off(self, session: AsyncSession, account_id: int, trigger: str) -> bool:
        config = self._config
        rows = (
            await session.execute(
                select(AgentRun.status, AgentRun.error_class)
                .where(AgentRun.account_id == account_id)
                .where(AgentRun.trigger_ref == trigger)
                .where(AgentRun.started_at >= self._clock.now() - config.failure_backoff_window)
                .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
                .limit(config.failure_backoff_runs)
            )
        ).all()
        if len(rows) < config.failure_backoff_runs:
            return False
        return all(error in _REPEATING_ERRORS for _status, error in rows)

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
            "summarize_run_not_succeeded",
            account_id=prepared.account_id,
            run_id=prepared.run_id,
            status=status,
            error_class=error_class,
        )
        async with transaction(self._session_factory) as uow:
            await self._close_run(uow.session, prepared, status, error_class)

    async def _fail_run(self, prepared: _Prepared, error: BaseException) -> None:
        """An unexpected error or a cancellation: the run must not stay
        `running`. Best effort — a failure here is logged (type only) and the
        original error still propagates."""
        try:
            await self._finish_run(prepared, "failed", f"exception:{type(error).__name__}")
        except Exception as close_error:
            logger.error(
                "summarize_run_close_failed",
                run_id=prepared.run_id,
                exc_type=type(close_error).__name__,
            )


async def _notes(
    session: AsyncSession, account_id: int, conversation_id: int
) -> list[items.MemoryView]:
    """The owner's notes a summary of this chat may see (§7.3, S03.3): this
    contact's and this chat's — no owner-scope item, nothing about anyone
    else (§6 invariant 4, FR-MEM-06)."""
    return await items.contact_facing(
        session, account_id, conversation_id, include_owner_preferences=False
    )


def _run_close(result: StoreResult) -> tuple[str, str | None]:
    """How a run that got an answer is closed, by what the store did."""
    if result is StoreResult.STORED:
        return "succeeded", None
    return "failed", f"discarded:{result.value}"
