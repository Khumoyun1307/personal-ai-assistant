"""The retrieval target's **agent** mode (S03.7 E2): the pipeline exactly as the
product runs it.

`variants` measures an eval-side prompt plus the search service (E-f). This
mode runs, for each of the 38 owner questions, the real `OwnerRequestRuntime`
with the shipped `owner_request` prompt (the prompt registry's active
version), the product's `default_registry()` with the production release flags,
a real `Summarizer`, the product's `AgentConfig` budgets and a fixed clock. The
model's reply goes through the product's citation step (`agent/citations.py`),
so what is scored is what the owner would read.

Setup (once, on the loaded corpus account): a control chat, an `ai_control` and
an `ai_managed` consent naming the processor of the run's model, an active
business connection and the owner language (`--owner-language`, default
uz-Latn). Per query: the question is stored as an owner control turn, the
runtime answers it, and then the account's control turns, agent runs, memory
items, summaries, cards, confirmations and audit rows are deleted and its
settings and chat modes restored, so **no query sees another's history**.

Instrumentation only, never a behaviour change: a wrapper around the
`search_messages` handler records what each call returned, and a wrapper
around the model client keeps the model's own final text (to check the values
in the model's words, not in the sources the application added).

Metrics (E-aa):

- **top 5**: a target message among the first 5 results of **any**
  `search_messages` call in the run (no search at all: a miss, `no_search`);
- **correct answer with reference**: the run succeeded, at least one
  *verified* citation (a `[msg:id]` the runtime turned into a source) points to
  a target, no citation was removed as unverified, and every `answer_values`
  string is in the model's own words as whole words — or inflected (E-s
  endings) only where a cited target message has that very form.

Cost (E-ab): the loop's worst case is far above a typical run, so a full run
may start on the **expected** cost calibrated from a `--limit 1` trial
(`--calibrate-from`), always under the hard in-flight cap of
`BudgetedLLMClient`; a run that hits the cap is stopped, incomplete, never a
gate.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from sqlalchemy import delete, func, select, update

from assistant.accounts.models import (
    Account,
    AuditLogEntry,
    BusinessConnection,
    ControlTurn,
    PendingInput,
)
from assistant.accounts.service import (
    CONSENT_AI_CONTROL,
    CONSENT_AI_MANAGED,
    grant_consent,
    record_control_turn,
)
from assistant.agent import prompt_registry
from assistant.agent.citations import MARKER
from assistant.agent.models import AgentRun, ToolConfirmation
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime, RunStatus
from assistant.agent.summarize import SummarizeConfig, Summarizer
from assistant.agent.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRegistry,
    default_registry,
)
from assistant.conversations.models import Conversation, Message
from assistant.core.channel import Button, Delivery
from assistant.core.model_routes import processor_for_model
from assistant.llm.port import LLMClient, LLMRequest, LLMResponse, ParsedResponse, StopReason
from assistant.memory.models import ConversationSummary, MemoryItem
from evals.budget import BudgetExceededError, CostEstimate, Prices, estimate
from evals.corpus import LoadedCorpus
from evals.errors import RefusedError
from evals.metrics import (
    MIN_INFLECTED_STEM,
    OwnerLanguage,
    contains_words,
    is_inflection,
    ratio_metric,
    word_key,
)
from evals.retrieval import (
    ANSWER_THRESHOLD,
    HIT_THRESHOLD,
    TOP_N,
    AnswerVerdict,
    RetrievalMode,
)
from evals.review import ReviewLedger
from evals.schema import RetrievalQuery
from evals.targets import (
    DatasetName,
    Measure,
    MetricResult,
    PlannedCall,
    RunContext,
    TargetResult,
)

ROUTE = "owner_request"
#: The production release flags: the `FEATURE_FLAGS` line of
#: `docs/milestones/M03-owner-guide.md` (every M03 tool, plus the background
#: summary refresh). A test checks that it covers every registered tool.
PRODUCTION_FLAGS = frozenset(
    {
        "tool.get_status",
        "tool.search_messages",
        "tool.resolve_contact",
        "tool.get_conversation",
        "tool.list_conversations",
        "tool.summarize_period",
        "tool.get_attention_list",
        "summary.refresh",
        "tool.remember",
        "tool.forget",
        "tool.list_memories",
        "tool.set_conversation_mode",
        "tool.update_preferences",
        "tool.delete_conversation_data",
        "tool.delete_contact_data",
        "tool.delete_everything",
    }
)
#: A consent version label that cannot be mistaken for a real policy version.
EVAL_POLICY_VERSION = "eval-synthetic"
_CONTROL_CHAT_ID = 7_000_000_097

# ------------------------------------------------------------------ estimate
#: The loop's own bound is its token budget: no new call starts once the run has
#: used `run_token_budget` tokens, so everything before the last call fits in
#: it; the last call is planned with a turn of tool results on top, at its full
#: output budget, plus its one truncation retry. Summary calls (catch-up and
#: attention tools) are separate runs: an allowance of two per query. A
#: catch-up over many chats can make more, which only the in-flight cap stops.
_TOOL_RESULT_CHARS = 20_000
SUMMARY_CALLS_PER_QUERY = 2
_SUMMARY_INPUT_CHARS = 40_000
#: E-ab: the expected cost is the trial's spend per query times the queries,
#: times this margin (a single trial query is a small sample).
EXPECTED_MARGIN = Decimal("1.5")


def _planned_calls(query: RetrievalQuery) -> list[PlannedCall]:
    agent = AgentConfig(model="")  # the product's budgets; no model needed
    summary = SummarizeConfig(model="")
    last_input = agent.run_token_budget + _TOOL_RESULT_CHARS + len(query.text)
    retry = min(agent.max_output_tokens * 2, agent.max_output_tokens_ceiling)
    calls = [
        PlannedCall(ROUTE, agent.run_token_budget, 0),
        PlannedCall(ROUTE, last_input, agent.max_output_tokens),
        PlannedCall(ROUTE, last_input, retry),
    ]
    calls += [
        PlannedCall("summarize", _SUMMARY_INPUT_CHARS, summary.max_output_tokens)
    ] * SUMMARY_CALLS_PER_QUERY
    return calls


@dataclass(frozen=True, slots=True)
class Calibration:
    """What a recorded `--limit 1` agent trial spent per query (E-ab)."""

    per_query_usd: Decimal
    source: str


def load_calibration(path: Path, *, model: str) -> Calibration:
    """The spend per query of a finished agent-mode trial of `model` with the
    shipped `owner_request` prompt; refused for anything else."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RefusedError(f"cannot calibrate from {path.name}: {error}") from None
    if not isinstance(report, dict):
        raise RefusedError(f"cannot calibrate from {path.name}: not a report")
    prompt = prompt_registry.active(ROUTE).label
    problems: list[str] = []
    if (report.get("target"), report.get("mode")) != ("retrieval", RetrievalMode.AGENT.value):
        problems.append("not a retrieval report of mode `agent`")
    if report.get("model") != model:
        problems.append(f"its model is {report.get('model')!r}, this run's is {model!r}")
    if prompt not in (report.get("prompts") or []):
        problems.append(f"it did not run {prompt}")
    if report.get("stopped"):
        problems.append("it stopped early")
    items = report.get("items")
    if not isinstance(items, int) or items < 1:
        problems.append("no item ran")
    try:
        spent = Decimal(str((report.get("cost") or {}).get("spent_usd")))
    except InvalidOperation:
        spent = Decimal(0)
    if not spent > 0:
        problems.append("it recorded no spend")
    if problems:
        raise RefusedError(f"cannot calibrate from {path.name}: " + "; ".join(problems))
    assert isinstance(items, int)
    return Calibration(spent / items, path.name)


def check_agent_estimate(
    target: AgentModeTarget,
    context: RunContext,
    *,
    model: str,
    cap_usd: Decimal,
    prices: Prices,
    calibration: Calibration | None,
) -> tuple[CostEstimate, Decimal | None]:
    """The start rule (E-ab). Uncalibrated: the worst case must fit the cap, as
    for every run. Calibrated: the expected cost must; the in-flight cap still
    stops every call that could pass it. Returns the worst case and the
    expected cost (None when uncalibrated)."""
    worst = estimate(target.plan(context), model, cap_usd, prices)
    if calibration is None:
        if not worst.fits:
            raise RefusedError(
                f"refusing: the worst case of this agent run is ${worst.upper_bound_usd:.4f} for "
                f"{worst.calls} planned calls, over the ${cap_usd} cap. Run a `--limit 1` trial "
                "and pass its report with --calibrate-from to start on the expected cost under "
                "the in-flight cap (E-ab), use --limit, or raise --budget-usd with the owner's "
                "approval."
            )
        return worst, None
    queries = len(target.selected(context))
    expected = calibration.per_query_usd * queries * EXPECTED_MARGIN
    if expected > cap_usd:
        raise RefusedError(
            f"refusing: the expected cost of this agent run is ${expected:.4f} "
            f"(${calibration.per_query_usd:.4f} per query in {calibration.source}, x {queries} "
            f"queries x {EXPECTED_MARGIN}), over the ${cap_usd} cap."
        )
    return worst, expected


# ------------------------------------------------------------ answer checking
def _inflected_phrases(haystack: str, needle: str) -> list[str]:
    """The phrases of `haystack` that carry `needle` with E-s endings on its
    last word (both are `word_key`s), as `metrics.contains_value` accepts them."""
    words = needle.split()
    if not words:
        return []
    *head, last = words
    if last.isalpha() and len(last) < MIN_INFLECTED_STEM:
        return []
    tokens = haystack.split()
    found: list[str] = []
    for start in range(len(tokens) - len(words) + 1):
        window = tokens[start : start + len(words)]
        candidate = window[-1]
        if (
            window[:-1] == head
            and candidate != last
            and candidate.startswith(last)
            and is_inflection(candidate[len(last) :])
        ):
            found.append(" ".join(window))
    return found


def check_agent_answer(
    *,
    succeeded: bool,
    model_text: str,
    cited: Sequence[str],
    unverified: int,
    query: RetrievalQuery,
    cited_texts: Mapping[str, str],
) -> AnswerVerdict:
    """Is the agent's reply correct, with a verified reference? (E-aa)

    `cited` are the dataset ids of the citations the runtime verified;
    `model_text` is the model's own final text (markers are not words)."""
    if not succeeded:
        return AnswerVerdict(False, ("run_not_succeeded",))
    problems: list[str] = []
    if not cited:
        problems.append("no_citation")
    elif not any(ref in query.target_message_ids for ref in cited):
        problems.append("no_target_cited")
    if unverified:
        problems.append("unverified_citation")
    words = word_key(MARKER.sub(" ", model_text))
    targets = [
        word_key(text) for ref, text in cited_texts.items() if ref in query.target_message_ids
    ]
    for value in query.answer_values:
        needle = word_key(value)
        if contains_words(words, needle):
            continue
        if any(
            contains_words(target, phrase)
            for phrase in _inflected_phrases(words, needle)
            for target in targets
        ):
            continue
        problems.append("missing_value")
    return AnswerVerdict(not problems, tuple(dict.fromkeys(problems)))


# ------------------------------------------------------------ instrumentation
@dataclass(frozen=True, slots=True)
class _SearchCall:
    queries: tuple[str, ...]
    contact: str | None
    widened: bool
    #: Dataset ids of the results, best first.
    results: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        return {
            "queries": list(self.queries),
            "contact": self.contact,
            "widened": self.widened,
            "top5": list(self.results[:TOP_N]),
        }


class _FinalText:
    """The model client, unchanged, keeping the model's last final text of an
    `owner_request` call (instrumentation only)."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.processor = inner.processor
        self.text: str | None = None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        response = await self._inner.complete(request)
        if request.purpose == ROUTE and response.stop_reason is StopReason.COMPLETED:
            self.text = response.text
        return response

    async def parse[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> ParsedResponse[T]:
        return await self._inner.parse(request, schema)


class _Capture:
    """The control chat: keeps what the runtime sends (eval-side `ControlChannel`)."""

    def __init__(self) -> None:
        self.sends: list[str] = []

    async def send(
        self, *, account_id: int, chat_id: int, text: str, buttons: Sequence[Button] = ()
    ) -> Delivery:
        self.sends.append(text)
        return Delivery(ok=True, message_id=len(self.sends))

    async def typing(self, *, account_id: int, chat_id: int) -> None:
        return None


class _FixedClock:
    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        return self._instant


# --------------------------------------------------------------- the target
@dataclass(frozen=True, slots=True)
class _Record:
    query_id: str
    intents: tuple[str, ...]
    hit: bool
    rank: int | None
    searches: tuple[_SearchCall, ...]
    answer_correct: bool
    problems: tuple[str, ...]
    citations: tuple[str, ...]
    unverified: int
    run_status: str
    error: str | None
    #: The reply the owner would read (synthetic data), for review.
    reply: str

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.query_id,
            "intents": list(self.intents),
            "hit_top5": self.hit,
            "rank": self.rank,
            "miss": "no_search" if not self.searches else None,
            "searches": [search.as_json() for search in self.searches],
            "answer_correct": self.answer_correct,
            "answer_problems": list(self.problems),
            "citations": list(self.citations),
            "unverified_citations": self.unverified,
            "run_status": self.run_status,
            "error": self.error,
            "reply": self.reply,
        }


@dataclass(frozen=True, slots=True)
class _Saved:
    """The account's settings and chat state before the first query."""

    timezone: str
    locale_pref: str | None
    settings: dict[str, Any]
    #: Conversation id -> (mode, pending_ai_since).
    conversations: Mapping[int, tuple[str, datetime | None]]


class AgentModeTarget:
    name = "retrieval"
    dataset: DatasetName = "retrieval"
    route = ROUTE
    uses_model = True
    mode = RetrievalMode.AGENT

    def __init__(
        self,
        queries: Sequence[RetrievalQuery],
        *,
        owner_language: OwnerLanguage = "uz-Latn",
        corpus: LoadedCorpus | None = None,
    ) -> None:
        self._queries = list(queries)
        self._owner_language = owner_language
        self._bound = corpus
        self._saved: _Saved | None = None
        self._contacts: dict[int, str] = {}
        self._turns = 0

    def bind(self, corpus: LoadedCorpus) -> AgentModeTarget:
        return AgentModeTarget(self._queries, owner_language=self._owner_language, corpus=corpus)

    @property
    def _corpus(self) -> LoadedCorpus:
        if self._bound is None:
            raise RuntimeError("bind() a loaded corpus before running the agent mode")
        return self._bound

    def selected(self, context: RunContext) -> list[RetrievalQuery]:
        if context.limit is None:
            return self._queries
        if context.limit < 1:
            raise ValueError("limit must be at least 1")
        return self._queries[: context.limit]

    def plan(self, context: RunContext) -> Sequence[PlannedCall]:
        return [call for query in self.selected(context) for call in _planned_calls(query)]

    # ------------------------------------------------------------------ run
    async def run(self, context: RunContext) -> TargetResult:
        if context.llm is None or context.model is None:
            raise ValueError("the agent mode needs a model client and a model id")
        queries = self.selected(context)
        await self._prepare(context.model)
        records: list[_Record] = []
        stopped: str | None = None
        for query in queries:
            try:
                records.append(await self._evaluate(query, context))
            except BudgetExceededError as stop:
                stopped = str(stop)
                break
        return self._result(records, planned=len(queries), ledger=context.reviews, stopped=stopped)

    async def _evaluate(self, query: RetrievalQuery, context: RunContext) -> _Record:
        assert context.llm is not None
        assert context.model is not None
        corpus = self._corpus
        searches: list[_SearchCall] = []
        llm = _FinalText(context.llm)
        channel = _Capture()
        clock = _FixedClock(corpus.now)
        runtime = OwnerRequestRuntime(
            session_factory=corpus.session_factory,
            llm=llm,
            channel=channel,
            registry=self._registry(searches),
            clock=clock,
            config=AgentConfig(
                model=context.model, effort=context.effort, enabled_flags=PRODUCTION_FLAGS
            ),
            summarizer=Summarizer(
                session_factory=corpus.session_factory,
                llm=llm,
                clock=clock,
                config=SummarizeConfig(model=context.model, effort=context.effort),
            ),
        )
        turn_id = await self._ask(query.text)
        try:
            outcome = await runtime.run(turn_id)
            error_class = await self._error_class(outcome.run_id)
            cited = tuple(corpus.refs[i] for i in outcome.cited_message_ids if i in corpus.refs)
            cited_texts = await self._texts(outcome.cited_message_ids)
        finally:
            await self._reset()
        succeeded = outcome.status is RunStatus.SUCCEEDED
        verdict = check_agent_answer(
            succeeded=succeeded,
            model_text=llm.text or "",
            cited=cited,
            unverified=outcome.unverified_citations,
            query=query,
            cited_texts=cited_texts,
        )
        rank: int | None = None
        for search in searches:
            positions = [
                i
                for i, ref in enumerate(search.results[:TOP_N], start=1)
                if ref in query.target_message_ids
            ]
            if positions:
                rank = positions[0]
                break
        return _Record(
            query_id=query.id,
            intents=tuple(query.intents),
            hit=rank is not None,
            rank=rank,
            searches=tuple(searches),
            answer_correct=verdict.correct,
            problems=verdict.problems,
            citations=cited,
            unverified=outcome.unverified_citations,
            run_status=outcome.status.value,
            error=None
            if succeeded
            else f"run:{outcome.status.value}" + (f":{error_class}" if error_class else ""),
            reply=channel.sends[0] if channel.sends else "",
        )

    def _registry(self, searches: list[_SearchCall]) -> ToolRegistry:
        """The product's registry, with `search_messages` results recorded."""
        refs, contacts = self._corpus.refs, self._contacts

        def recording(tool: ToolDefinition) -> ToolDefinition:
            real = tool.handler

            async def handler(ctx: ToolContext, args: Any) -> ToolOutcome:
                outcome = await real(ctx, args)
                results = outcome.data.get("results") or []
                searches.append(
                    _SearchCall(
                        queries=tuple(args.queries),
                        contact=contacts.get(args.contact_id) if args.contact_id else None,
                        widened=bool(outcome.data.get("widened")),
                        results=tuple(
                            refs[r["message_id"]] for r in results if r["message_id"] in refs
                        ),
                    )
                )
                return outcome

            return replace(tool, handler=handler)

        return ToolRegistry(
            recording(tool) if tool.name == "search_messages" else tool
            for tool in default_registry().all()
        )

    # -------------------------------------------------------------- the data
    async def _prepare(self, model: str) -> None:
        """What the product needs to answer the owner (the module docstring)."""
        if self._saved is not None:
            return
        corpus = self._corpus
        processor = processor_for_model(model)
        if processor is None:
            raise RefusedError(f"no processor is known for model '{model}'")
        clock = _FixedClock(corpus.now)
        async with corpus.session_factory() as session, session.begin():
            account = await session.get(Account, corpus.account_id)
            assert account is not None  # the corpus account
            account.control_chat_id = _CONTROL_CHAT_ID
            account.locale_pref = self._owner_language
            account.status = "active"
            for kind in (CONSENT_AI_CONTROL, CONSENT_AI_MANAGED):
                await grant_consent(
                    session,
                    account,
                    kind=kind,
                    policy_version=EVAL_POLICY_VERSION,
                    processors=[processor],
                    clock=clock,
                )
            first = (
                await session.execute(
                    select(func.min(Message.sent_at)).where(Message.account_id == corpus.account_id)
                )
            ).scalar_one()
            existing = (
                await session.execute(
                    select(BusinessConnection.id).where(
                        BusinessConnection.account_id == corpus.account_id
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    BusinessConnection(
                        account_id=corpus.account_id,
                        connection_id=f"eval-agent-{corpus.account_id}",
                        is_enabled=True,
                        rights=["can_reply", "can_read_messages"],
                        capability_tier="can_reply",
                        user_chat_id=_CONTROL_CHAT_ID,
                        status="active",
                        connected_at=first or corpus.now,
                    )
                )
            rows = (
                await session.execute(
                    select(Conversation.id, Conversation.mode, Conversation.pending_ai_since).where(
                        Conversation.account_id == corpus.account_id
                    )
                )
            ).all()
            contacts = (
                await session.execute(
                    select(Message.id, Conversation.contact_id)
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .where(Message.account_id == corpus.account_id)
                )
            ).all()
            self._saved = _Saved(
                timezone=account.timezone,
                locale_pref=account.locale_pref,
                settings=dict(account.settings),
                conversations={int(r[0]): (str(r[1]), r[2]) for r in rows},
            )
        for message_id, contact_id in contacts:
            conversation = corpus.refs[int(message_id)].split(".", 1)[0]
            self._contacts[int(contact_id)] = corpus.contact_names[conversation]

    async def _ask(self, text: str) -> int:
        self._turns += 1
        async with self._corpus.session_factory() as session, session.begin():
            turn = await record_control_turn(
                session,
                account_id=self._corpus.account_id,
                role="owner",
                text=text,
                telegram_message_id=self._turns,
                created_at=self._corpus.now,
            )
            return turn.id

    async def _error_class(self, run_id: int | None) -> str | None:
        if run_id is None:
            return None
        async with self._corpus.session_factory() as session:
            run = await session.get(AgentRun, run_id)
            return None if run is None else run.error_class

    async def _texts(self, message_ids: Sequence[int]) -> dict[str, str]:
        if not message_ids:
            return {}
        corpus = self._corpus
        async with corpus.session_factory() as session:
            rows = (
                await session.execute(
                    select(Message.id, Message.text)
                    .where(Message.account_id == corpus.account_id)
                    .where(Message.id.in_(message_ids))
                )
            ).all()
        return {corpus.refs[int(i)]: text or "" for i, text in rows if int(i) in corpus.refs}

    async def _reset(self) -> None:
        """No history between queries: what a run left is deleted, and settings
        and chat modes a tool may have changed are restored."""
        saved = self._saved
        assert saved is not None
        account_id = self._corpus.account_id
        async with self._corpus.session_factory() as session, session.begin():
            for model in (
                ToolConfirmation,
                PendingInput,
                MemoryItem,
                ConversationSummary,
                ControlTurn,
                AgentRun,
                AuditLogEntry,
            ):
                await session.execute(delete(model).where(model.account_id == account_id))
            account = await session.get(Account, account_id)
            assert account is not None
            account.timezone = saved.timezone
            account.locale_pref = saved.locale_pref
            account.settings = dict(saved.settings)
            for conversation_id, (mode, pending) in saved.conversations.items():
                await session.execute(
                    update(Conversation)
                    .where(Conversation.account_id == account_id)
                    .where(Conversation.id == conversation_id)
                    .values(mode=mode, pending_ai_since=pending)
                )

    # --------------------------------------------------------------- metrics
    def _result(
        self,
        records: Sequence[_Record],
        *,
        planned: int,
        ledger: ReviewLedger,
        stopped: str | None,
    ) -> TargetResult:
        total = len(self._queries)
        metrics: list[MetricResult] = [
            ratio_metric(
                key="top5_hit",
                label="Target message in the top 5 of any search in the agent run",
                outcomes={r.query_id: r.hit for r in records},
                threshold=HIT_THRESHOLD,
                ledger=ledger,
                set_size=total,
            ),
            ratio_metric(
                key="answer_correct",
                label="Correct reply with a verified citation of a target",
                outcomes={r.query_id: r.answer_correct for r in records},
                threshold=ANSWER_THRESHOLD,
                ledger=ledger,
                set_size=total,
            ),
        ]
        notes: list[str] = [
            "Agent mode: the real owner_request runtime with the shipped prompt, the "
            "production release flags and the product's citation step (E-aa)."
        ]
        unsearched = sorted(r.query_id for r in records if not r.searches)
        if unsearched:
            notes.append(
                f"{len(unsearched)} item(s) made no search_messages call (a top-5 miss, "
                "`no_search`): " + ", ".join(unsearched)
            )
        widened = sorted(r.query_id for r in records if any(s.widened for s in r.searches))
        if widened:
            notes.append(
                f"{len(widened)} item(s) had a search widened by the service: " + ", ".join(widened)
            )
        failed = sorted(r.query_id for r in records if r.error)
        if failed:
            notes.append(
                f"{len(failed)} run(s) did not succeed and count as misses: " + ", ".join(failed)
            )
        if stopped:
            notes.append(f"STOPPED EARLY: {stopped}; {len(records)} of {planned} items ran")
        return TargetResult(
            target=self.name,
            dataset=self.dataset,
            items=len(records),
            items_planned=planned,
            items_total=total,
            metrics=tuple(metrics),
            details=tuple(r.as_json() for r in records),
            breakdowns={"top-5 hit by intent": _by_intent(records)},
            notes=tuple(notes),
            prompts=(prompt_registry.active(ROUTE).label,),
        )


def _by_intent(records: Sequence[_Record]) -> Mapping[str, Measure]:
    grouped: dict[str, list[bool]] = {}
    for record in records:
        for intent in record.intents:
            grouped.setdefault(intent, []).append(record.hit)
    return {intent: Measure(sum(flags), len(flags)) for intent, flags in sorted(grouped.items())}
