"""S03.4: the retrieval target over the real search service, on a real PostgreSQL.

The synthetic E1 corpus is loaded under a dedicated account (the shared test
database, deleted afterwards) or into a throwaway database; the model is
`FakeLLM` only. Nothing here calls a real model or Telegram.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from typing import Any

import psycopg
import pytest
import sqlalchemy as sa
from evals.budget import BudgetedLLMClient, CallLedger
from evals.corpus import LoadedCorpus, isolated_corpus, load_corpus, unload_corpus
from evals.retrieval import (
    QueryVariants,
    RetrievalMode,
    RetrievalTarget,
)
from evals.review import ReviewLedger
from evals.schema import RetrievalQuery, load_e1, load_queries
from evals.targets import RunContext, Status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.conversations.models import Message
from assistant.db.engine import build_engine, build_session_factory
from assistant.llm.port import (
    LLMError,
    LLMErrorKind,
    LLMRequest,
    ParsedResponse,
    Provenance,
    StopReason,
    TextItem,
    Usage,
)
from tests.fakes.llm import FakeLLM
from tests.integration.conftest import admin_url

pytestmark = pytest.mark.integration

CONVERSATIONS = load_e1()
QUERIES = load_queries()
MESSAGES = {m.id: m for c in CONVERSATIONS for m in c.messages}
#: The deterministic baseline must not regress below this (measured 2026-10-25
#: on E1 v1: 31 of 38, 81.6 %; the S03.4 gate is 85 % with query variants, not
#: this number).
DETERMINISTIC_FLOOR = 0.8


@pytest.fixture
async def factory(migrated_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
async def corpus(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[LoadedCorpus]:
    loaded = await load_corpus(factory, CONVERSATIONS)
    try:
        yield loaded
    finally:
        await unload_corpus(factory, loaded.account_id)


def target(corpus: LoadedCorpus, mode: RetrievalMode) -> RetrievalTarget:
    return RetrievalTarget(QUERIES, mode=mode, corpus=corpus)


# ------------------------------------------------------------------ the corpus
async def test_the_corpus_is_stored_as_the_product_stores_messages(corpus: LoadedCorpus) -> None:
    assert len(corpus.refs) == sum(len(c.messages) for c in CONVERSATIONS)
    by_dataset_id = {v: k for k, v in corpus.refs.items()}
    async with corpus.session_factory() as session:
        deleted = [m for m in MESSAGES.values() if m.deleted]
        assert deleted
        for message in deleted:
            row = await session.get(Message, by_dataset_id[message.id])
            assert row is not None
            assert (row.text, row.search_key, row.media) == (None, None, None)
            assert row.deleted_at is not None
        edited = next(m for m in MESSAGES.values() if m.edited)
        row = await session.get(Message, by_dataset_id[edited.id])
        assert row is not None
        assert row.text == edited.text
        assert row.search_key


async def test_reviewer_only_text_never_reaches_the_database(corpus: LoadedCorpus) -> None:
    hidden = {text for m in MESSAGES.values() for text in (m.original_text, m.purged_text) if text}
    current = {m.text for m in MESSAGES.values() if m.text}
    hidden -= current  # a pre-edit text that is also someone's current text proves nothing
    assert hidden
    async with corpus.session_factory() as session:
        found = (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.account_id == corpus.account_id, Message.text.in_(hidden))
            )
        ).scalar_one()
    assert found == 0


async def test_unloading_removes_every_row_of_the_account(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    loaded = await load_corpus(factory, CONVERSATIONS[:3])
    await unload_corpus(factory, loaded.account_id)
    async with factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.account_id == loaded.account_id)
            )
        ).scalar_one()
    assert count == 0


async def test_two_corpora_in_one_database_do_not_see_each_other(
    factory: async_sessionmaker[AsyncSession], corpus: LoadedCorpus
) -> None:
    other = await load_corpus(factory, CONVERSATIONS[:5])
    try:
        result = await target(other, RetrievalMode.DETERMINISTIC).run(RunContext())
        returned = {ref for record in result.details for ref in record["top5"]}
        assert returned  # something was found...
        assert {r.split(".")[0] for r in returned} <= {
            c.id for c in CONVERSATIONS[:5]
        }  # ...only here
    finally:
        await unload_corpus(factory, other.account_id)


# ----------------------------------------------------------- deterministic mode
async def test_the_deterministic_baseline_runs_without_a_model(corpus: LoadedCorpus) -> None:
    result = await target(corpus, RetrievalMode.DETERMINISTIC).run(RunContext())

    assert (result.items, result.items_planned, result.complete) == (38, 38, True)
    (metric,) = result.metrics
    assert metric.key == "top5_hit"
    assert metric.threshold == 0.85
    assert metric.all_items.denominator == 38
    assert (metric.all_items.ratio or 0.0) >= DETERMINISTIC_FLOOR
    # No item is owner-confirmed, so the gate is not evaluable, whatever the score.
    assert metric.gate_status() is Status.NOT_EVALUABLE
    assert result.prompts == ()
    assert {"id", "hit_top5", "rank", "top5", "variants"} <= set(result.details[0])
    assert "top-5 hit by intent" in result.breakdowns


async def test_the_deterministic_run_is_reproducible(corpus: LoadedCorpus) -> None:
    first = await target(corpus, RetrievalMode.DETERMINISTIC).run(RunContext(limit=12))
    second = await target(corpus, RetrievalMode.DETERMINISTIC).run(RunContext(limit=12))
    assert first.details == second.details


async def test_limit_evaluates_only_the_first_items(corpus: LoadedCorpus) -> None:
    result = await target(corpus, RetrievalMode.DETERMINISTIC).run(RunContext(limit=3))
    assert [d["id"] for d in result.details] == ["r01", "r02", "r03"]
    assert result.items_planned == 3


async def test_the_gate_is_computed_over_owner_confirmed_queries_only(
    corpus: LoadedCorpus,
) -> None:
    ledger = ReviewLedger(frozenset({"r01", "r02"}), {}, {})
    result = await target(corpus, RetrievalMode.DETERMINISTIC).run(RunContext(reviews=ledger))
    (metric,) = result.metrics
    assert metric.confirmed.denominator == 2
    assert metric.all_items.denominator == 38


# ---------------------------------------------------------------- variants mode
def unwrap(value: str) -> str:
    return html.unescape(re.sub(r"^<untrusted[^>]*>|</untrusted>$", "", value))


class OracleLLM(FakeLLM):
    """A scripted 'perfect model': proposes the owner's own words as the only
    variant and answers from whichever result is a target message. It reads
    the dataset only to *play the model*; the code under test never does."""

    def __init__(
        self,
        variants: Callable[[RetrievalQuery], list[str]] | None = None,
        usage: Usage = Usage(400, 100),  # noqa: B008  (immutable)
    ) -> None:
        super().__init__()
        self._by_text = {q.text: q for q in QUERIES}
        self._variants = variants or (lambda q: [q.text])
        self._usage = usage

    async def parse[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> ParsedResponse[T]:
        self.requests.append(request)
        query = self._by_text[request.items[0].text]  # type: ignore[union-attr]
        value: dict[str, Any]
        if schema is QueryVariants:
            value = {"queries": self._variants(query)}
        else:
            value = self._answer(query, request.items[1].text)  # type: ignore[union-attr]
        return ParsedResponse(
            StopReason.COMPLETED, "gpt-6-luna", self._usage, schema.model_validate(value)
        )

    @staticmethod
    def _answer(query: RetrievalQuery, results_json: str) -> dict[str, Any]:
        targets = {MESSAGES[t].text for t in query.target_message_ids}
        for result in json.loads(results_json)["results"]:
            text = unwrap(result["text"])
            if text in targets:
                return {
                    "found": True,
                    "answer": " ".join(query.answer_values),
                    "references": [
                        {
                            "ref": result["ref"],
                            "contact": unwrap(result["contact_name"]),
                            "day": result["sent_at"][:10],
                            "quote": text,
                        }
                    ],
                }
        return {"found": False, "answer": "Not found in the results.", "references": []}


def context(llm: FakeLLM, ledger: CallLedger, **kwargs: Any) -> RunContext:
    budgeted = BudgetedLLMClient(llm, cap_usd=Decimal("1"), ledger=ledger)
    return RunContext(llm=budgeted, model="gpt-6-luna", **kwargs)


async def test_variants_mode_with_a_perfect_model_answers_every_retrieved_query_correctly(
    corpus: LoadedCorpus,
) -> None:
    llm, ledger = OracleLLM(), CallLedger()
    result = await target(corpus, RetrievalMode.VARIANTS).run(context(llm, ledger))

    hit, answered = result.metrics
    assert (hit.key, answered.key) == ("top5_hit", "answer_correct")
    # The answer step is only as good as retrieval: exactly the retrieved queries
    # are answered correctly, which exercises the whole reference check.
    assert answered.all_items.numerator == hit.all_items.numerator
    assert hit.all_items.numerator > 0
    assert result.prompts == ("retrieval_variants.v1", "retrieval_answer.v1")
    # Metered: a variants call per query plus an answer call per query with hits.
    purposes = [e.purpose for e in ledger.entries]
    assert purposes.count("eval_retrieval_variants") == 38
    assert purposes.count("eval_retrieval_answer") == len(llm.requests) - 38


async def test_what_the_model_is_sent_never_includes_labels_or_other_queries(
    corpus: LoadedCorpus,
) -> None:
    llm = OracleLLM()
    await target(corpus, RetrievalMode.VARIANTS).run(context(llm, CallLedger(), limit=10))

    for request in llm.requests:
        assert request.tools == ()  # contact-triggered work gets no tools (invariant 3a)
        texts = [item.text for item in request.items if isinstance(item, TextItem)]
        sent = request.instructions + " ".join(texts)
        for query in QUERIES:
            assert query.expected_answer not in sent
            assert not any(t in sent for t in query.target_message_ids)
        if len(request.items) == 2:
            assert request.items[1].provenance is Provenance.MANAGED
            assert request.items[0].provenance is Provenance.OWNER_CONTROL


async def test_the_answer_step_is_not_called_when_nothing_was_retrieved(
    corpus: LoadedCorpus,
) -> None:
    llm = OracleLLM(variants=lambda q: [])
    nothing = RetrievalQuery(
        id="r99",
        text="zzzzqqqq xxxxwwww",
        lang="en",
        script="latin",
        intents=["exact"],
        target_message_ids=["c09.m05"],
        conversation_ids=["c09"],
        expected_answer="n/a",
    )
    llm._by_text[nothing.text] = nothing
    result = await RetrievalTarget([nothing], mode=RetrievalMode.VARIANTS, corpus=corpus).run(
        context(llm, CallLedger())
    )
    assert len(llm.requests) == 1  # variants only
    assert result.details[0]["answer_problems"] == ["no_hits"]
    assert result.metrics[1].all_items.numerator == 0


async def test_a_failed_model_call_counts_as_a_miss_and_is_listed(corpus: LoadedCorpus) -> None:
    class Failing(OracleLLM):
        async def parse[T: BaseModel](
            self, request: LLMRequest, schema: type[T]
        ) -> ParsedResponse[T]:
            raise LLMError(LLMErrorKind.UNAVAILABLE)

    result = await target(corpus, RetrievalMode.VARIANTS).run(
        context(Failing(), CallLedger(), limit=4)
    )
    hit, answered = result.metrics
    assert hit.all_items.numerator == 0
    assert answered.all_items.numerator == 0
    assert {d["error"] for d in result.details} == {"variants:unavailable"}
    assert any("failed at the model and count as misses" in n for n in result.notes)


async def test_a_truncated_answer_is_a_miss_not_a_crash(corpus: LoadedCorpus) -> None:
    class Truncated(OracleLLM):
        async def parse[T: BaseModel](
            self, request: LLMRequest, schema: type[T]
        ) -> ParsedResponse[T]:
            if schema is QueryVariants:
                return await super().parse(request, schema)
            return ParsedResponse(StopReason.MAX_TOKENS, "gpt-6-luna", Usage(10, 10))

    result = await target(corpus, RetrievalMode.VARIANTS).run(
        context(Truncated(), CallLedger(), limit=6)
    )
    assert result.metrics[1].all_items.numerator == 0
    assert any(d["error"] == "answer:max_tokens" for d in result.details)


async def test_a_budget_stop_ends_the_run_early_and_the_gate_cannot_pass(
    corpus: LoadedCorpus,
) -> None:
    # Each call uses nearly its whole output budget (reasoning models do), so a
    # cap of about four calls stops the run on the fourth or fifth.
    llm, ledger = OracleLLM(usage=Usage(200, 1500)), CallLedger()
    cap = Decimal("0.0035")
    capped = BudgetedLLMClient(llm, cap_usd=cap, ledger=ledger)
    everything_confirmed = ReviewLedger(frozenset(q.id for q in QUERIES), {}, {})

    result = await target(corpus, RetrievalMode.VARIANTS).run(
        RunContext(llm=capped, model="gpt-6-luna", reviews=everything_confirmed)
    )

    assert not result.complete
    assert result.items < result.items_planned == 38
    assert any("STOPPED EARLY" in note for note in result.notes)
    assert 0 < ledger.spent <= cap
    for metric in result.metrics:
        assert metric.gate_status(complete=result.complete) is not Status.PASS
    assert len(llm.requests) == len(ledger.entries)  # nothing was called unmetered


async def test_variants_mode_needs_a_model(corpus: LoadedCorpus) -> None:
    with pytest.raises(ValueError, match="needs a model"):
        await target(corpus, RetrievalMode.VARIANTS).run(RunContext())


# ------------------------------------------------------------- scratch database
async def test_the_throwaway_database_is_migrated_isolated_and_dropped(
    test_database_url: str,
) -> None:
    async with isolated_corpus(test_database_url, CONVERSATIONS[:4]) as loaded:
        engine = loaded.session_factory.kw["bind"]
        name = engine.url.database
        assert name.startswith("assistant_eval_")
        assert name != sa.engine.make_url(test_database_url).database
        result = await RetrievalTarget(
            QUERIES[:2], mode=RetrievalMode.DETERMINISTIC, corpus=loaded
        ).run(RunContext())
        assert result.items == 2

    with psycopg.connect(admin_url(test_database_url), autocommit=True) as conn:
        left = conn.execute(
            "SELECT count(*) FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
    assert left == (0,)
