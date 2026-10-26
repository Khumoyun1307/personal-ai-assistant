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
from pathlib import Path
from typing import Any

import psycopg
import pytest
import sqlalchemy as sa
from evals import cli, live
from evals import retrieval as retrieval_module
from evals.agent_mode import PRODUCTION_FLAGS, AgentModeTarget
from evals.budget import BudgetedLLMClient, CallLedger
from evals.corpus import LoadedCorpus, isolated_corpus, load_corpus, unload_corpus
from evals.errors import EvalsError
from evals.live import LiveConfig, LiveSetupError, ModelChoice
from evals.retrieval import (
    QueryVariants,
    RetrievalMode,
    RetrievalTarget,
    load_recorded,
)
from evals.review import ReviewLedger
from evals.schema import RetrievalQuery, load_e1, load_queries
from evals.targets import RunContext, Status
from pydantic import BaseModel, SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.agent import prompt_registry
from assistant.agent.tools.registry import default_registry
from assistant.config import Settings
from assistant.conversations.models import Message
from assistant.db.engine import build_engine, build_session_factory
from assistant.llm.port import (
    LLMError,
    LLMErrorKind,
    LLMRequest,
    LLMResponse,
    ParsedResponse,
    Provenance,
    ProviderItems,
    StopReason,
    TextItem,
    ToolCall,
    ToolResultItem,
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
    assert metric.threshold == 0.85  # the pre-screen comparison only
    assert metric.all_items.denominator == 38
    assert (metric.all_items.ratio or 0.0) >= DETERMINISTIC_FLOOR
    # The 85 % gate is defined with model-generated variants: with no model the
    # gate reads INFO, whatever was confirmed.
    assert metric.gate_status() is Status.INFO
    everything = ReviewLedger(frozenset(q.id for q in QUERIES), {}, {})
    confirmed = await target(corpus, RetrievalMode.DETERMINISTIC).run(
        RunContext(reviews=everything)
    )
    assert confirmed.metrics[0].gate_status() is Status.INFO
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
    assert result.trial
    assert not result.complete


async def test_a_limited_run_is_never_gate_evaluable_even_fully_confirmed(
    corpus: LoadedCorpus,
) -> None:
    everything = ReviewLedger(frozenset(q.id for q in QUERIES), {}, {})
    context_ = context(OracleLLM(), CallLedger(), limit=3, reviews=everything)
    result = await target(corpus, RetrievalMode.VARIANTS).run(context_)
    assert (result.items, result.trial, result.stopped) == (3, True, False)
    for metric in result.metrics:
        assert metric.confirmation() == (3, 38)
        assert metric.gate_status() is Status.NOT_EVALUABLE


async def test_the_gate_is_computed_over_owner_confirmed_queries_only(
    corpus: LoadedCorpus,
) -> None:
    ledger = ReviewLedger(frozenset({"r01", "r02"}), {}, {})
    result = await target(corpus, RetrievalMode.DETERMINISTIC).run(RunContext(reviews=ledger))
    (metric,) = result.metrics
    assert metric.confirmed.denominator == 2
    assert metric.all_items.denominator == 38
    assert metric.confirmation() == (2, 38)


async def test_a_fully_confirmed_variants_run_has_an_evaluable_gate(corpus: LoadedCorpus) -> None:
    everything = ReviewLedger(frozenset(q.id for q in QUERIES), {}, {})
    result = await target(corpus, RetrievalMode.VARIANTS).run(
        context(OracleLLM(), CallLedger(), reviews=everything)
    )
    for metric in result.metrics:
        assert metric.confirmation() == (38, 38)
        assert metric.gate_status() in (Status.PASS, Status.FAIL)  # never "not evaluable"


async def test_a_distinct_variant_rescues_a_deterministic_miss(corpus: LoadedCorpus) -> None:
    """r03 ("the address Kamola sent") misses as typed; a variant in the words of
    the message finds it, and the final answer is then correct."""
    r03 = next(q for q in QUERIES if q.id == "r03")
    plain = await RetrievalTarget([r03], mode=RetrievalMode.DETERMINISTIC, corpus=corpus).run(
        RunContext()
    )
    assert plain.details[0]["hit_top5"] is False

    llm = OracleLLM(variants=lambda q: ["Qorasuv-3 17-uy"])
    rescued = await RetrievalTarget([r03], mode=RetrievalMode.VARIANTS, corpus=corpus).run(
        context(llm, CallLedger())
    )
    record = rescued.details[0]
    assert record["hit_top5"] is True
    assert record["variants"] == 2  # the owner's words plus the distinct variant
    assert record["answer_correct"] is True


async def test_a_named_chat_partner_narrows_the_search_like_resolve_contact(
    corpus: LoadedCorpus,
) -> None:
    """E-t: the owner names the person ("…with Nilufar"), and the messages of that
    chat rarely contain the name. The product agent resolves the name and filters by
    contact; the harness does the same with the product's own matcher."""
    r04 = next(q for q in QUERIES if q.id == "r04")
    llm = OracleLLM(variants=lambda q: ["kelishdik"], contact=lambda q: "Nilufar")
    result = await RetrievalTarget([r04], mode=RetrievalMode.VARIANTS, corpus=corpus).run(
        context(llm, CallLedger())
    )
    record = result.details[0]
    assert record["contact"] == "Nilufar"
    assert record["contact_filter"] == "Nilufar opa (rahbar)"
    assert record["widened"] is False
    assert record["top5"]
    assert all(ref.startswith("c11.") for ref in record["top5"])
    assert record["hit_top5"] is True
    assert record["searched"] == [r04.text, "kelishdik"]


async def test_an_ambiguous_or_unknown_name_is_not_used_as_a_filter(corpus: LoadedCorpus) -> None:
    """Two contacts are called Rustam (Рустам амаки, Rustam (kuryer)): the agent would
    ask the owner, so the harness searches unfiltered; an unknown name likewise."""
    r21 = next(q for q in QUERIES if q.id == "r21")
    for name in ("Rustam", "Zebiniso"):
        llm = OracleLLM(contact=lambda q, name=name: name)  # type: ignore[misc]
        result = await RetrievalTarget([r21], mode=RetrievalMode.VARIANTS, corpus=corpus).run(
            context(llm, CallLedger())
        )
        assert result.details[0]["contact"] == name
        assert result.details[0]["contact_filter"] is None
        assert result.details[0]["widened"] is False


async def test_a_filtered_search_that_finds_nothing_is_widened(corpus: LoadedCorpus) -> None:
    r01 = next(q for q in QUERIES if q.id == "r01")  # "Qorasuv-3 17-uy", in Kamola's chat
    llm = OracleLLM(contact=lambda q: "Daniel")  # a real contact, but the wrong chat
    result = await RetrievalTarget([r01], mode=RetrievalMode.VARIANTS, corpus=corpus).run(
        context(llm, CallLedger())
    )
    record = result.details[0]
    # S03.6 review: the report says the filter was dropped, and does not show it
    # as the filter that produced the hits.
    assert record["contact"] == "Daniel"
    assert record["widened"] is True
    assert record["contact_filter"] is None
    assert record["hit_top5"] is True  # found again without the filter
    assert any("widened" in note and "r01" in note for note in result.notes)


async def test_variants_and_replay_use_the_products_widening(
    corpus: LoadedCorpus, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S03.7 E1 (E-z): the harness no longer widens by itself. Each item makes
    one call to the product's `search_messages` with `widen_empty_scope=True`,
    and `widened` is the service's own answer."""
    calls: list[dict[str, Any]] = []
    real = retrieval_module.search_messages

    async def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(retrieval_module, "search_messages", spy)
    r01 = next(q for q in QUERIES if q.id == "r01")  # in Kamola's chat
    llm = OracleLLM(contact=lambda q: "Daniel")  # a real contact, but the wrong chat
    variants = await RetrievalTarget([r01], mode=RetrievalMode.VARIANTS, corpus=corpus).run(
        context(llm, CallLedger())
    )
    path = recorded_report(tmp_path, [{"id": "r01", "searched": [r01.text], "contact": "Daniel"}])
    replayed = await RetrievalTarget(
        [r01], mode=RetrievalMode.REPLAY, recorded=load_recorded(path), corpus=corpus
    ).run(RunContext(reviews=ReviewLedger.empty()))

    assert len(calls) == 2  # one search per item: the service widens, not the harness
    for kwargs in calls:
        assert kwargs["widen_empty_scope"] is True
        assert kwargs["filters"].contact_ids  # narrowed to Daniel's chat first
    for result in (variants, replayed):
        assert result.details[0]["widened"] is True
        assert result.details[0]["contact_filter"] is None
        assert result.details[0]["hit_top5"] is True


# ---------------------------------------------------------------- replay mode
def recorded_report(tmp_path: Path, details: list[dict[str, Any]], **meta: Any) -> Path:
    report = {"target": "retrieval", "mode": "variants", "details": details, **meta}
    path = tmp_path / "variants.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


async def test_replay_searches_the_recorded_variants_and_partner_without_a_model(
    corpus: LoadedCorpus, tmp_path: Path
) -> None:
    """S03.6 review: the offline replay is reproducible: what a variants run searched
    and the partner it named, searched again with the current search code."""
    r04 = next(q for q in QUERIES if q.id == "r04")
    path = recorded_report(
        tmp_path,
        [{"id": "r04", "searched": [r04.text, "kelishdik"], "contact": "Nilufar"}],
    )
    target = RetrievalTarget(
        [r04],
        mode=RetrievalMode.REPLAY,
        recorded=load_recorded(path),
        replayed_from=path.name,
        corpus=corpus,
    )
    assert target.uses_model is False
    result = await target.run(RunContext(reviews=ReviewLedger.empty()))
    record = result.details[0]
    assert record["searched"] == [r04.text, "kelishdik"]
    assert record["contact_filter"] == "Nilufar opa (rahbar)"
    assert record["hit_top5"] is True
    assert [m.key for m in result.metrics] == ["top5_hit"]  # no answer without a model
    assert result.metrics[0].gated is False
    assert result.prompts == ()
    assert "Replayed: variants.json." in result.notes


async def test_replay_counts_an_item_the_recorded_run_did_not_search_as_a_miss(
    corpus: LoadedCorpus, tmp_path: Path
) -> None:
    r01 = next(q for q in QUERIES if q.id == "r01")
    path = recorded_report(tmp_path, [{"id": "r01", "searched": [], "contact": None}])
    result = await RetrievalTarget(
        [r01], mode=RetrievalMode.REPLAY, recorded=load_recorded(path), corpus=corpus
    ).run(RunContext(reviews=ReviewLedger.empty()))
    assert result.details[0]["hit_top5"] is False
    assert result.details[0]["error"] == "replay:not_searched"


def test_only_a_variants_report_can_be_replayed(tmp_path: Path) -> None:
    with pytest.raises(EvalsError):
        load_recorded(recorded_report(tmp_path, [], mode="deterministic"))
    with pytest.raises(EvalsError):
        RetrievalTarget(QUERIES[:1], mode=RetrievalMode.REPLAY)


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
        contact: Callable[[RetrievalQuery], str | None] | None = None,
        usage: Usage = Usage(400, 100),  # noqa: B008  (immutable)
    ) -> None:
        super().__init__()
        self._by_text = {q.text: q for q in QUERIES}
        self._variants = variants or (lambda q: [q.text])
        self._contact = contact or (lambda q: None)
        self._usage = usage

    async def parse[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> ParsedResponse[T]:
        self.requests.append(request)
        query = self._by_text[request.items[0].text]  # type: ignore[union-attr]
        value: dict[str, Any]
        if schema is QueryVariants:
            value = {"queries": self._variants(query), "contact": self._contact(query)}
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
    assert result.prompts == ("retrieval_variants.v2", "retrieval_answer.v1")
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
    assert result.stopped
    assert result.items < result.items_planned == 38
    assert any("STOPPED EARLY" in note for note in result.notes)
    assert 0 < ledger.spent <= cap
    for metric in result.metrics:
        assert metric.gate_status() is not Status.PASS
    assert len(llm.requests) == len(ledger.entries)  # nothing was called unmetered


def test_a_budget_stopped_run_exits_non_zero_and_still_writes_its_report(
    tmp_path: Path,
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI end to end on a throwaway database, with a scripted model whose
    calls are expensive enough to hit the cap on the third call."""
    expensive = OracleLLM(usage=Usage(2_000_000, 400_000))  # USD 0.40 per call
    monkeypatch.setenv("EVAL_DATABASE_URL", test_database_url)
    monkeypatch.setattr(live, "_settings", _no_settings)
    monkeypatch.setattr(
        cli,
        "load_live_config",
        lambda route, override: LiveConfig(ModelChoice("gpt-6-luna", None), SecretStr("sk-x")),
    )
    monkeypatch.setattr(cli, "build_client", lambda config: expensive)

    code = cli.main(
        [
            "run", "retrieval", "--mode", "variants", "--model", "gpt-6-luna", "--live",
            "--budget-usd", "0.5", "--out-dir", str(tmp_path), "--date", "2026-10-25",
        ]
    )  # fmt: skip

    assert code == cli.EXIT_INCOMPLETE
    (report,) = tmp_path.glob("*.md")
    assert "STOPPED RUN" in report.read_text(encoding="utf-8")
    assert len(expensive.requests) == 2  # the third call was refused before it was made
    assert "STOPPED EARLY" in capsys.readouterr().out


def _no_settings() -> Settings:
    raise LiveSetupError("the settings are invalid (test)")


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


# ------------------------------------------------------------------ agent mode
QUERIES_BY_ID = {q.id: q for q in QUERIES}


class AgentOracle(FakeLLM):
    """Plays the agent's model in `--mode agent` (S03.7 E2): calls
    `search_messages` with each scripted list of phrases, one call per turn,
    then answers. It finds the target among the results by its text (the
    dataset is read only to *play the model*; the code under test never does)."""

    def __init__(
        self,
        searches: Callable[[RetrievalQuery], list[list[str]]] | None = None,
        reply: Callable[[RetrievalQuery, list[dict[str, Any]]], str] | None = None,
        usage: Usage = Usage(400, 100),  # noqa: B008  (immutable)
    ) -> None:
        super().__init__()
        self._searches = searches or (lambda q: [[q.text]])
        self._reply = reply or cite_a_target
        self._usage = usage

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        assert request.purpose == "owner_request"
        query = asked(request)
        outputs = [
            json.loads(item.output) for item in request.items if isinstance(item, ToolResultItem)
        ]
        planned = self._searches(query)
        if len(outputs) < len(planned):
            arguments = json.dumps(
                {
                    "queries": planned[len(outputs)],
                    "contact_id": None,
                    "conversation_id": None,
                    "since": None,
                    "until": None,
                    "limit": None,
                }
            )
            call = ToolCall(f"call-{len(outputs)}", "search_messages", arguments)
            return LLMResponse(
                StopReason.TOOL_CALLS,
                "gpt-6-luna",
                self._usage,
                tool_calls=(call,),
                output_items=ProviderItems("fake", ({"type": "function_call"},)),
            )
        results = [r for output in outputs for r in output.get("results", [])]
        return LLMResponse(
            StopReason.COMPLETED,
            "gpt-6-luna",
            self._usage,
            text=self._reply(query, results),
            output_items=ProviderItems("fake", ({"type": "message"},)),
        )


def asked(request: LLMRequest) -> RetrievalQuery:
    """The owner's question this run answers (the last owner text item)."""
    texts = [item.text for item in request.items if isinstance(item, TextItem)]
    for text in reversed(texts):
        for query in QUERIES:
            if query.text in text:
                return query
    raise AssertionError("no dataset question in the request")


def is_target(query: RetrievalQuery, result: dict[str, Any]) -> bool:
    targets = {MESSAGES[t].text for t in query.target_message_ids}
    return unwrap(result["text"] or "") in targets


def cite_a_target(query: RetrievalQuery, results: list[dict[str, Any]]) -> str:
    for result in results:
        if is_target(query, result):
            return f"{' '.join(query.answer_values)} [msg:{result['message_id']}]"
    return "I found nothing about that."


def agent_target(corpus: LoadedCorpus, *ids: str) -> AgentModeTarget:
    return AgentModeTarget([QUERIES_BY_ID[i] for i in ids], corpus=corpus)


async def test_agent_mode_runs_the_owner_request_runtime_with_the_shipped_prompt_and_production_flags(  # noqa: E501
    corpus: LoadedCorpus,
) -> None:
    oracle, ledger = AgentOracle(), CallLedger()
    result = await agent_target(corpus, "r01").run(context(oracle, ledger))

    shipped = prompt_registry.active("owner_request")
    first = oracle.requests[0]
    assert first.purpose == "owner_request"
    assert first.instructions.startswith(shipped.text)
    registry_tools = {tool.name for tool in default_registry().all()}
    assert {tool.name for tool in first.tools} == registry_tools
    assert {tool.release_flag for tool in default_registry().all()} <= PRODUCTION_FLAGS
    assert result.prompts == (shipped.label,)
    [record] = result.details
    assert record["hit_top5"] is True
    assert record["answer_correct"] is True, record["answer_problems"]
    assert record["citations"]
    assert set(record["citations"]) <= set(QUERIES_BY_ID["r01"].target_message_ids)
    assert "[msg:" not in record["reply"]
    assert record["unverified_citations"] == 0
    assert [m.key for m in result.metrics] == ["top5_hit", "answer_correct"]
    assert len(ledger.entries) == len(oracle.requests) == 2  # a search turn, then the answer


async def test_agent_mode_top5_counts_any_search_call_in_the_run(corpus: LoadedCorpus) -> None:
    two_searches = AgentOracle(searches=lambda q: [["zzqx synthetic nothing"], [q.text]])
    result = await agent_target(corpus, "r01").run(context(two_searches, CallLedger()))
    [record] = result.details
    assert len(record["searches"]) == 2
    assert record["searches"][0]["top5"] == []
    assert record["hit_top5"] is True

    no_search = AgentOracle(searches=lambda q: [], reply=lambda q, r: "I can't tell.")
    result = await agent_target(corpus, "r01").run(context(no_search, CallLedger()))
    [record] = result.details
    assert record["hit_top5"] is False
    assert record["miss"] == "no_search"
    assert record["answer_correct"] is False


async def test_agent_mode_needs_a_verified_citation_of_a_target(corpus: LoadedCorpus) -> None:
    def cite_another(query: RetrievalQuery, results: list[dict[str, Any]]) -> str:
        other = next(r for r in results if not is_target(query, r))
        return f"{' '.join(query.answer_values)} [msg:{other['message_id']}]"

    def no_marker(query: RetrievalQuery, results: list[dict[str, Any]]) -> str:
        return " ".join(query.answer_values)

    for reply, problem in ((cite_another, "no_target_cited"), (no_marker, "no_citation")):
        oracle = AgentOracle(searches=lambda q: [[q.text], ["salom"]], reply=reply)
        result = await agent_target(corpus, "r01").run(context(oracle, CallLedger()))
        [record] = result.details
        assert record["hit_top5"] is True
        assert record["answer_correct"] is False
        assert problem in record["answer_problems"]


async def test_agent_mode_an_unverified_citation_is_a_problem(corpus: LoadedCorpus) -> None:
    def cite_target_and_more(query: RetrievalQuery, results: list[dict[str, Any]]) -> str:
        return cite_a_target(query, results) + " Also [msg:987654321]."

    oracle = AgentOracle(reply=cite_target_and_more)
    result = await agent_target(corpus, "r01").run(context(oracle, CallLedger()))
    [record] = result.details
    assert record["unverified_citations"] == 1
    assert "unverified_citation" in record["answer_problems"]
    assert record["answer_correct"] is False
    assert "987654321" not in record["reply"]


async def test_agent_mode_starts_each_query_without_history(corpus: LoadedCorpus) -> None:
    oracle = AgentOracle()
    result = await agent_target(corpus, "r01", "r02").run(context(oracle, CallLedger()))

    assert [d["id"] for d in result.details] == ["r01", "r02"]
    first_reply = result.details[0]["reply"]
    second_run = [r for r in oracle.requests if asked(r).id == "r02"]
    assert second_run
    for request in second_run:
        texts = " ".join(item.text for item in request.items if isinstance(item, TextItem))
        assert QUERIES_BY_ID["r01"].text not in texts
        assert first_reply not in texts
    async with corpus.session_factory() as session:
        for table in ("control_turns", "agent_runs", "memory_items", "conversation_summaries"):
            left = (
                await session.execute(
                    sa.text(f"SELECT count(*) FROM app.{table} WHERE account_id = :a"),  # noqa: S608
                    {"a": corpus.account_id},
                )
            ).scalar_one()
            assert left == 0, table


def test_agent_mode_requires_live_and_records_its_spend(
    tmp_path: Path,
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    oracle = AgentOracle()
    monkeypatch.setenv("EVAL_DATABASE_URL", test_database_url)
    monkeypatch.setattr(live, "_settings", _no_settings)
    monkeypatch.setattr(
        cli,
        "load_live_config",
        lambda route, override: LiveConfig(ModelChoice("gpt-6-luna", None), SecretStr("sk-x")),
    )
    monkeypatch.setattr(cli, "build_client", lambda config: oracle)
    base = ["run", "retrieval", "--mode", "agent", "--model", "gpt-6-luna", "--limit", "1"]

    refused = cli.main([*base, "--out-dir", str(tmp_path)])
    assert refused == cli.EXIT_REFUSED
    assert "--live" in capsys.readouterr().err
    assert oracle.requests == []

    code = cli.main([*base, "--live", "--out-dir", str(tmp_path), "--date", "2026-10-26"])

    assert code == 0
    (machine,) = tmp_path.glob("*.json")
    assert machine.name == "2026-10-26-retrieval-agent-gpt-6-luna-trial.json"
    report = json.loads(machine.read_text(encoding="utf-8"))
    assert report["mode"] == "agent"
    assert report["cost"]["calls"] == len(oracle.requests) == 2
    assert Decimal(report["cost"]["spent_usd"]) > 0
    assert report["details"][0]["id"] == QUERIES[0].id
