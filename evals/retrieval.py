"""The retrieval target (S03.4): the real search service over the synthetic corpus.

Two modes, both over the 38-query owner-style set:

- **deterministic** (no model): the query exactly as typed goes to
  `memory.retrieval.search_messages`; measures *target message in the top 5*
  (threshold 85 %). This is a baseline and a regression check on the
  normaliser and ranking; the S03.4 gate is measured with variants.
- **variants** (a model, through the `LLMClient` port): a model writes query
  variants (Latin, Cyrillic, Russian, English spellings, as the agent prompt
  asks, `owner_request.v2.md` rule 5); the original query plus the variants are
  searched together; a second call writes the final answer from the top 5 and
  the answer is checked deterministically (threshold 80 %).

Answer correctness (`check_answer`) is deliberately strict and mechanical:
the answer must say it found something, reference a **target** message, name
the contact and the day correctly, quote the message verbatim, and carry
every `answer_values` string. No judge decides it. See the report's notes and
M03.md E-decisions for what that does and does not measure.

A failed model call (provider error, refusal, truncation) counts as a **miss**
and is listed; a budget stop ends the run early and the result is marked
incomplete (its gate is never a pass).
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from assistant.agent.untrusted import Source, wrap
from assistant.conversations import read
from assistant.llm.port import (
    LLMError,
    LLMRequest,
    Provenance,
    StopReason,
    TextItem,
)
from assistant.memory.retrieval import MAX_QUERIES, SearchFilters, SearchHit, search_messages
from evals.assets import load_asset
from evals.budget import BudgetExceededError
from evals.corpus import LoadedCorpus
from evals.metrics import contains_value, contains_words, ratio_metric, word_key
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

TOP_N = 5
HIT_THRESHOLD = 0.85
ANSWER_THRESHOLD = 0.80
VARIANTS_PROMPT_ID = "retrieval_variants.v2"
ANSWER_PROMPT_ID = "retrieval_answer.v1"
#: Output budgets include hidden reasoning tokens, so they are generous; the
#: budget guard prices every call at this worst case before making it.
VARIANTS_MAX_OUTPUT_TOKENS = 1500
ANSWER_MAX_OUTPUT_TOKENS = 2500
TIMEOUT_SECONDS = 90.0
MAX_QUERY_CHARS = 200
#: Generous size of one rendered hit (the JSON wrapper, the untrusted tags and
#: a message of a few hundred characters), for the pre-run estimate only.
_HIT_CHARS = 1000
#: A quote shorter than this (in word characters: letters and digits with single
#: spaces) must be the whole message. One short word or a bare amount occurs in
#: many messages and proves nothing; 8 is about one real word plus a number.
MIN_QUOTE_CHARS = 8


class RetrievalMode(StrEnum):
    DETERMINISTIC = "deterministic"
    VARIANTS = "variants"


# ---------------------------------------------------------- structured output
class QueryVariants(BaseModel):
    model_config = ConfigDict(extra="forbid")

    queries: list[str]
    #: The chat partner the owner named, as typed, or None (E-t): resolved like the
    #: product's `resolve_contact` and applied as the search's contact filter.
    contact: str | None


class Reference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    contact: str
    day: str
    quote: str


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    found: bool
    answer: str
    references: list[Reference]


# ------------------------------------------------------------ answer checking
@dataclass(frozen=True, slots=True)
class HitView:
    """A search hit as the answer step sees it, with what the check needs."""

    ref: str
    dataset_id: str
    conversation_id: str
    contact: str
    day: str
    text: str


@dataclass(frozen=True, slots=True)
class AnswerVerdict:
    correct: bool
    #: Short codes, e.g. `wrong_date`, `missing_value`; never content.
    problems: tuple[str, ...]


def _compact(text: str) -> str:
    """Word key of `text`, with HTML entities undone: the untrusted wrapper
    escapes `<`, `>` and `&`, and a model may copy the escaped form."""
    return word_key(html.unescape(text))


def _names_match(model_name: str, dataset_name: str) -> bool:
    """Whole words: "Kamola opa" matches "Kamola"; "Kam" and "Kamolaxon" do not."""
    a, b = _compact(model_name), _compact(dataset_name)
    return contains_words(a, b) or contains_words(b, a)


def check_answer(answer: Answer, query: RetrievalQuery, hits: Sequence[HitView]) -> AnswerVerdict:
    """Is the final answer correct, with a reference? (threshold 80 %)

    Every condition must hold; the first failures are listed. Values are
    compared in the product's ASCII form, so script and apostrophes do not
    matter, but a translated or reformatted value does not count.
    """
    if not answer.found:
        return AnswerVerdict(False, ("not_found",))
    if not answer.references:
        return AnswerVerdict(False, ("no_reference",))
    by_ref = {hit.ref: hit for hit in hits}
    problems: list[str] = []
    cited_targets = 0
    quotes: list[str] = []
    #: Word keys of the quotes found word for word in the message they cite.
    exact_quotes: list[str] = []
    for reference in answer.references:
        hit = by_ref.get(reference.ref)
        if hit is None:
            problems.append("unknown_ref")
            continue
        cited_targets += hit.dataset_id in query.target_message_ids
        if not _names_match(reference.contact, hit.contact):
            problems.append("wrong_contact")
        if reference.day.strip() != hit.day:
            problems.append("wrong_date")
        message, quote = _compact(hit.text), _compact(reference.quote)
        if not contains_words(message, quote):
            problems.append("quote_not_in_message")
        else:
            exact_quotes.append(quote)
            if len(quote) < MIN_QUOTE_CHARS and quote != message:
                problems.append("quote_too_short")
        quotes.append(reference.quote)
    if not cited_targets:
        problems.append("no_target_cited")
    carried = _compact(f"{answer.answer} {' '.join(quotes)}")
    for value in query.answer_values:
        needle = _compact(value)
        # Whole words in the model's text; only an exact quote of the message may
        # carry the value as the message inflected it ("500 mingni" for "500
        # ming", E-u; S03.6 review).
        if not contains_words(carried, needle) and not any(
            contains_value(exact, needle) for exact in exact_quotes
        ):
            problems.append("missing_value")
    return AnswerVerdict(not problems, tuple(dict.fromkeys(problems)))


# --------------------------------------------------------------- the target
@dataclass(frozen=True, slots=True)
class _Record:
    query_id: str
    intents: tuple[str, ...]
    hit: bool
    rank: int | None
    top: tuple[str, ...]
    variants: int
    answer_correct: bool | None
    problems: tuple[str, ...]
    error: str | None
    #: What was searched (the owner's words, then the model's variants) and the
    #: chat partner the model named and the contact it resolved to: model output
    #: over synthetic queries, kept so a miss can be diagnosed (E-t).
    searched: tuple[str, ...] = ()
    contact: str | None = None
    #: The contact filter that produced the hits; None when there was none, or when
    #: the filtered search found nothing and was searched again unfiltered
    #: (`widened`; S03.6 review).
    contact_filter: str | None = None
    widened: bool = False
    #: The final answer the model wrote (synthetic data), for review.
    answer: Answer | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.query_id,
            "intents": list(self.intents),
            "hit_top5": self.hit,
            "rank": self.rank,
            "top5": list(self.top),
            "variants": self.variants,
            "answer_correct": self.answer_correct,
            "answer_problems": list(self.problems),
            "error": self.error,
            "searched": list(self.searched),
            "contact": self.contact,
            "contact_filter": self.contact_filter,
            "widened": self.widened,
            "answer": self.answer.model_dump() if self.answer is not None else None,
        }


class RetrievalTarget:
    name = "retrieval"
    dataset: DatasetName = "retrieval"
    #: Query variants and answers are owner-request work (ARCHITECTURE §7.4).
    route = "owner_request"

    def __init__(
        self,
        queries: Sequence[RetrievalQuery],
        *,
        mode: RetrievalMode,
        corpus: LoadedCorpus | None = None,
    ) -> None:
        self._bound = corpus
        self._queries = list(queries)
        self.mode = mode
        self.uses_model = mode is RetrievalMode.VARIANTS
        self._variants_prompt = load_asset("prompts", VARIANTS_PROMPT_ID)
        self._answer_prompt = load_asset("prompts", ANSWER_PROMPT_ID)

    def bind(self, corpus: LoadedCorpus) -> RetrievalTarget:
        """The same target over a loaded corpus. The runner builds a target
        first (to refuse and estimate), and only then pays for a database."""
        return RetrievalTarget(self._queries, mode=self.mode, corpus=corpus)

    @property
    def _corpus(self) -> LoadedCorpus:
        if self._bound is None:
            raise RuntimeError("bind() a loaded corpus before running the retrieval target")
        return self._bound

    def _selected(self, context: RunContext) -> list[RetrievalQuery]:
        if context.limit is None:
            return self._queries
        if context.limit < 1:
            raise ValueError("limit must be at least 1")
        return self._queries[: context.limit]

    # ------------------------------------------------------------- estimate
    def plan(self, context: RunContext) -> Sequence[PlannedCall]:
        if not self.uses_model:
            return []
        calls: list[PlannedCall] = []
        for query in self._selected(context):
            calls.append(
                PlannedCall(
                    "eval_retrieval_variants",
                    len(self._variants_prompt.text) + len(query.text),
                    VARIANTS_MAX_OUTPUT_TOKENS,
                )
            )
            calls.append(
                PlannedCall(
                    "eval_retrieval_answer",
                    len(self._answer_prompt.text) + len(query.text) + TOP_N * _HIT_CHARS,
                    ANSWER_MAX_OUTPUT_TOKENS,
                )
            )
        return calls

    # ------------------------------------------------------------------ run
    async def run(self, context: RunContext) -> TargetResult:
        if self.uses_model and (context.llm is None or context.model is None):
            raise ValueError("the variants mode needs a model client and a model id")
        queries = self._selected(context)
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
        proposed = QueryVariants(queries=[], contact=None)
        if self.uses_model:
            try:
                proposed = await self._variants(query, context)
            except _ModelFailureError as failure:
                return _Record(
                    query.id, tuple(query.intents), False, None, (), 0, False, (), failure.code
                )
        search_queries = _search_queries(query.text, proposed.queries)
        contact = await self._resolve_contact(proposed.contact)
        hits, widened = await self._search(search_queries, contact)
        top = tuple(self._corpus.refs[hit.message_id] for hit in hits)
        rank = next(
            (i for i, ref in enumerate(top, start=1) if ref in query.target_message_ids), None
        )
        record = _Record(
            query.id,
            tuple(query.intents),
            rank is not None,
            rank,
            top,
            len(search_queries),
            None,
            (),
            None,
            searched=tuple(search_queries) if self.uses_model else (),
            contact=proposed.contact,
            contact_filter=contact.name if contact is not None and not widened else None,
            widened=widened,
        )
        if not self.uses_model:
            return record
        if not hits:
            return _with_answer(record, False, ("no_hits",), None)
        try:
            answer = await self._answer(query, hits, context)
        except _ModelFailureError as failure:
            return _with_answer(record, False, (), failure.code)
        verdict = check_answer(answer, query, self._views(hits))
        return replace(_with_answer(record, verdict.correct, verdict.problems, None), answer=answer)

    async def _resolve_contact(self, name: str | None) -> _ContactFilter | None:
        """The chat partner the model named, resolved the way the product's
        `resolve_contact` tool does (`conversations.read.match_contacts`). As the
        agent is told to, an ambiguous name (two best candidates of the same kind)
        is not used: the agent would ask the owner instead (E-t)."""
        if not name or not name.strip():
            return None
        async with self._corpus.session_factory() as session:
            directory = await read.contact_directory(session, self._corpus.account_id)
        matches = read.match_contacts(directory, name, limit=2)
        if not matches or (len(matches) > 1 and matches[0].kind == matches[1].kind):
            return None
        best = matches[0].entry
        return _ContactFilter(best.contact_id, best.display_name or "")

    async def _search(
        self, queries: Sequence[str], contact: _ContactFilter | None = None
    ) -> tuple[Sequence[SearchHit], bool]:
        """The service's top results; narrowed to the named contact when there is
        one, and searched again without the filter if that finds nothing (the agent
        would widen a search that came back empty). The flag says it was widened."""
        async with self._corpus.session_factory() as session:
            if contact is not None:
                narrowed = await search_messages(
                    session,
                    self._corpus.account_id,
                    queries,
                    now=self._corpus.now,
                    filters=SearchFilters(contact_ids=(contact.contact_id,)),
                    limit=TOP_N,
                )
                if narrowed.hits:
                    return narrowed.hits, False
            result = await search_messages(
                session,
                self._corpus.account_id,
                queries,
                now=self._corpus.now,
                limit=TOP_N,
            )
        return result.hits, contact is not None

    # ------------------------------------------------------------ model calls
    async def _variants(self, query: RetrievalQuery, context: RunContext) -> QueryVariants:
        assert context.llm is not None  # checked in run()
        assert context.model is not None
        request = LLMRequest(
            account_id=self._corpus.account_id,
            purpose="eval_retrieval_variants",
            model=context.model,
            instructions=self._variants_prompt.text,
            items=(TextItem("user", query.text, Provenance.OWNER_CONTROL),),
            max_output_tokens=VARIANTS_MAX_OUTPUT_TOKENS,
            timeout_seconds=TIMEOUT_SECONDS,
            effort=context.effort,
        )
        try:
            response = await context.llm.parse(request, QueryVariants)
        except LLMError as error:
            raise _ModelFailureError(f"variants:{error.kind.value}") from None
        if response.stop_reason is not StopReason.COMPLETED or response.parsed is None:
            raise _ModelFailureError(f"variants:{response.stop_reason.value}")
        return response.parsed

    async def _answer(
        self, query: RetrievalQuery, hits: Sequence[SearchHit], context: RunContext
    ) -> Answer:
        assert context.llm is not None
        assert context.model is not None
        request = LLMRequest(
            account_id=self._corpus.account_id,
            purpose="eval_retrieval_answer",
            model=context.model,
            instructions=self._answer_prompt.text,
            items=(
                TextItem("user", query.text, Provenance.OWNER_CONTROL),
                TextItem("user", self._results_block(hits), Provenance.MANAGED),
            ),
            max_output_tokens=ANSWER_MAX_OUTPUT_TOKENS,
            timeout_seconds=TIMEOUT_SECONDS,
            effort=context.effort,
        )
        try:
            response = await context.llm.parse(request, Answer)
        except LLMError as error:
            raise _ModelFailureError(f"answer:{error.kind.value}") from None
        if response.stop_reason is not StopReason.COMPLETED or response.parsed is None:
            raise _ModelFailureError(f"answer:{response.stop_reason.value}")
        return response.parsed

    # --------------------------------------------------------------- rendering
    def _views(self, hits: Sequence[SearchHit]) -> list[HitView]:
        zone = ZoneInfo(self._corpus.timezone)
        views: list[HitView] = []
        for number, hit in enumerate(hits, start=1):
            dataset_id = self._corpus.refs[hit.message_id]
            conversation_id = dataset_id.split(".", 1)[0]
            views.append(
                HitView(
                    ref=f"h{number}",
                    dataset_id=dataset_id,
                    conversation_id=conversation_id,
                    contact=self._corpus.contact_names[conversation_id],
                    day=_local_day(hit, zone),
                    text=hit.text or "",
                )
            )
        return views

    def _results_block(self, hits: Sequence[SearchHit]) -> str:
        """Like the `search_messages` tool result: contact text and names
        wrapped as untrusted, local time, no database ids."""
        zone = ZoneInfo(self._corpus.timezone)
        results = [
            {
                "ref": view.ref,
                "contact_name": wrap(view.contact, Source.CONTACT_NAME),
                "from": "contact" if hit.direction == "contact" else "owner",
                "sent_at": hit.sent_at.astimezone(zone).strftime("%Y-%m-%d %H:%M"),
                "text": wrap(
                    view.text,
                    Source.CONTACT if hit.direction == "contact" else Source.OWNER_IN_BUSINESS_CHAT,
                ),
            }
            for view, hit in zip(self._views(hits), hits, strict=True)
        ]
        return json.dumps(
            {"results": results, "timezone": self._corpus.timezone},
            ensure_ascii=False,
        )

    # ---------------------------------------------------------------- metrics
    def _result(
        self,
        records: Sequence[_Record],
        *,
        planned: int,
        ledger: ReviewLedger,
        stopped: str | None,
    ) -> TargetResult:
        total = len(self._queries)
        hits = {r.query_id: r.hit for r in records}
        metrics: list[MetricResult] = [
            ratio_metric(
                key="top5_hit",
                label="Target message in the top 5 results",
                outcomes=hits,
                threshold=HIT_THRESHOLD,
                ledger=ledger,
                set_size=total,
                # The 85 % is defined with model-generated variants: without a
                # model it is only the pre-screen comparison, with no gate.
                gated=self.uses_model,
                note=(
                    "Baseline without a model; the S03.4 gate measures retrieval with "
                    "model-generated query variants (mode `variants`), so 85 % is a "
                    "pre-screen comparison here, not a gate."
                    if not self.uses_model
                    else ""
                ),
            )
        ]
        notes: list[str] = []
        prompts: list[str] = []
        if self.uses_model:
            answers = {r.query_id: bool(r.answer_correct) for r in records}
            metrics.append(
                ratio_metric(
                    key="answer_correct",
                    label="Correct final answer with reference",
                    outcomes=answers,
                    threshold=ANSWER_THRESHOLD,
                    ledger=ledger,
                    set_size=total,
                )
            )
            prompts = [VARIANTS_PROMPT_ID, ANSWER_PROMPT_ID]
            widened = sorted(r.query_id for r in records if r.widened)
            if widened:
                notes.append(
                    f"{len(widened)} item(s) searched without their contact filter after the "
                    "filtered search found nothing (`widened`, E-t): " + ", ".join(widened)
                )
            failed = sorted(r.query_id for r in records if r.error)
            if failed:
                notes.append(
                    f"{len(failed)} item(s) failed at the model and count as misses: "
                    + ", ".join(failed)
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
            prompts=tuple(prompts),
        )


@dataclass(frozen=True, slots=True)
class _ContactFilter:
    contact_id: int
    name: str


class _ModelFailureError(Exception):
    """A model call failed in a way that counts the item as a miss."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _with_answer(
    record: _Record, correct: bool, problems: tuple[str, ...], error: str | None
) -> _Record:
    return replace(record, answer_correct=correct, problems=problems, error=error)


def _search_queries(original: str, variants: Sequence[str]) -> list[str]:
    """The owner's words first, then the model's variants (at most the
    service's `MAX_QUERIES`, each at most the tool's 200 characters)."""
    seen = {original.strip()}
    queries = [original.strip()]
    for variant in variants:
        cleaned = variant.strip()[:MAX_QUERY_CHARS]
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            queries.append(cleaned)
    return queries[:MAX_QUERIES]


def _local_day(hit: SearchHit, zone: ZoneInfo) -> str:
    day: date = hit.sent_at.astimezone(zone).date()
    return day.isoformat()


def _by_intent(records: Sequence[_Record]) -> Mapping[str, Measure]:
    grouped: dict[str, list[bool]] = {}
    for record in records:
        for intent in record.intents:
            grouped.setdefault(intent, []).append(record.hit)
    return {intent: Measure(sum(flags), len(flags)) for intent, flags in sorted(grouped.items())}
