"""The retrieval target (S03.4): the real search service over the synthetic corpus.

Three modes here, all over the 38-query owner-style set (a fourth, **agent**,
runs the real `owner_request` runtime per query: `evals/agent_mode.py`).
A named chat partner narrows the search, and an empty narrowed search is
widened by the product's own service (`widen_empty_scope`, S03.7 E-z), as the
`search_messages` tool does:

- **deterministic** (no model): the query exactly as typed goes to
  `memory.retrieval.search_messages`; measures *target message in the top 5*
  (threshold 85 %). This is a baseline and a regression check on the
  normaliser and ranking; the S03.4 gate is measured with variants.
- **variants** (a model, through the `LLMClient` port): a model writes query
  variants (Latin, Cyrillic, Russian, English spellings, as the agent prompt
  asks, `owner_request.v2.md` rule 5); the original query plus the variants are
  searched together; a second call writes the final answer from the top 5 and
  the answer is checked deterministically (threshold 80 %).
- **replay** (no model): the variants and the chat partner a recorded
  **variants** run searched (its JSON report), searched again with the current
  search code; top 5 only (S03.6 review: a search change is measured offline,
  reproducibly, without paying for a model).

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
from pathlib import Path
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
from evals.errors import RefusedError
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
    REPLAY = "replay"
    #: The real `owner_request` runtime per query (S03.7 E2): `evals/agent_mode.py`.
    AGENT = "agent"


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
        recorded: Mapping[str, QueryVariants | None] | None = None,
        replayed_from: str | None = None,
    ) -> None:
        if mode is RetrievalMode.AGENT:
            raise RefusedError("agent mode runs the owner_request runtime: evals/agent_mode.py")
        if (mode is RetrievalMode.REPLAY) != (recorded is not None):
            raise RefusedError(
                "a replay needs a recorded variants run (--replay-from), "
                "and only a replay takes one"
            )
        self._bound = corpus
        self._queries = list(queries)
        self.mode = mode
        self.uses_model = mode is RetrievalMode.VARIANTS
        self._recorded = recorded
        self._replayed_from = replayed_from
        self._variants_prompt = load_asset("prompts", VARIANTS_PROMPT_ID)
        self._answer_prompt = load_asset("prompts", ANSWER_PROMPT_ID)

    def bind(self, corpus: LoadedCorpus) -> RetrievalTarget:
        """The same target over a loaded corpus. The runner builds a target
        first (to refuse and estimate), and only then pays for a database."""
        return RetrievalTarget(
            self._queries,
            mode=self.mode,
            corpus=corpus,
            recorded=self._recorded,
            replayed_from=self._replayed_from,
        )

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
        elif self._recorded is not None:
            replayed = self._recorded.get(query.id)
            if replayed is None:  # the recorded run did not search this item
                return _Record(
                    query.id,
                    tuple(query.intents),
                    False,
                    None,
                    (),
                    0,
                    None,
                    (),
                    "replay:not_searched",
                )
            proposed = replayed
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
            searched=tuple(search_queries) if self.mode is not RetrievalMode.DETERMINISTIC else (),
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
        """The service's top results, narrowed to the named contact when there is
        one, exactly as the product's `search_messages` tool searches: the service
        widens a narrowed search that finds nothing (`widen_empty_scope`, S03.7 B1)
        and says so; the harness no longer widens by itself (E-z)."""
        filters = SearchFilters(contact_ids=(contact.contact_id,)) if contact else SearchFilters()
        async with self._corpus.session_factory() as session:
            result = await search_messages(
                session,
                self._corpus.account_id,
                queries,
                now=self._corpus.now,
                filters=filters,
                limit=TOP_N,
                widen_empty_scope=True,
            )
        return result.hits, result.widened

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
                note=_TOP5_NOTES[self.mode],
            )
        ]
        notes: list[str] = []
        if self._recorded is not None:
            notes.append(f"Replayed: {self._replayed_from or 'a recorded variants run'}.")
            skipped = sorted(r.query_id for r in records if r.error)
            if skipped:
                notes.append(
                    f"{len(skipped)} item(s) the recorded run did not search count as misses: "
                    + ", ".join(skipped)
                )
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


#: What the top-5 metric means in each mode.
_TOP5_NOTES = {
    RetrievalMode.DETERMINISTIC: (
        "Baseline without a model; the S03.4 gate measures retrieval with "
        "model-generated query variants (mode `variants`), so 85 % is a "
        "pre-screen comparison here, not a gate."
    ),
    RetrievalMode.VARIANTS: "",
    RetrievalMode.REPLAY: (
        "Replay without a model: the variants and chat partners a recorded `variants` run "
        "searched, searched again with the current search code (no answer step). 85 % is a "
        "pre-screen comparison here, not a gate."
    ),
}


def load_recorded(path: Path) -> dict[str, QueryVariants | None]:
    """What a recorded **variants** run searched, per item: the model's variants (the
    report's `searched` minus the owner's words, which come first) and the chat
    partner it named; None for an item it did not search (a failed model call)."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RefusedError(f"cannot read the recorded run {path.name}: {error}") from None
    if not isinstance(report, dict) or (report.get("target"), report.get("mode")) != (
        "retrieval",
        RetrievalMode.VARIANTS.value,
    ):
        raise RefusedError(f"{path.name} is not a retrieval report of mode `variants`")
    recorded: dict[str, QueryVariants | None] = {}
    for detail in report.get("details", []):
        searched = list(detail.get("searched") or [])
        recorded[str(detail["id"])] = (
            QueryVariants(queries=searched[1:], contact=detail.get("contact")) if searched else None
        )
    return recorded


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
