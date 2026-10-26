"""S03.4b: the summary target over the real S03.2 `summarize` path, on a real PostgreSQL.

Each E1 conversation is stored under its own synthetic account (the shared test
database, deleted afterwards) or in a throwaway database; the model is
`FakeLLM` only. Nothing here calls a real model or Telegram.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import evals.summary as summary_module
import psycopg
import pytest
import sqlalchemy as sa
from evals import cli, corpus, live
from evals.budget import BudgetedLLMClient, CallLedger, _request_chars, estimate, worst_case
from evals.live import LiveConfig, LiveSetupError, ModelChoice
from evals.metrics import values_matcher
from evals.review import ReviewLedger, coverage_key
from evals.schema import Conversation, QuestionToOwner, load_e1
from evals.summary import (
    FABRICATION_PURPOSE,
    LOOPS_PURPOSE,
    SUMMARIZE_PURPOSE,
    SummaryTarget,
    isolated_summary,
    product_coverage,
)
from evals.targets import RunContext, Status
from pydantic import BaseModel, SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.agent import prompt_registry
from assistant.config import Settings
from assistant.conversations.models import Message
from assistant.db.engine import build_engine, build_session_factory
from assistant.llm.port import (
    LLMError,
    LLMErrorKind,
    LLMRequest,
    ParsedResponse,
    StopReason,
    TextItem,
    Usage,
)
from assistant.memory.catch_up import GapFact, PeriodCoverage
from tests.fakes.llm import FakeLLM
from tests.integration.conftest import admin_url, create_scratch_database, drop_scratch_database

pytestmark = pytest.mark.integration

E1 = load_e1()
BY_ID = {c.id: c for c in E1}
LUNA = "gpt-6-luna"
#: A gap chat (possible), a gap chat (definite), a pre-connection chat, a chat
#: with a `delayed`-only gap (nothing to report) and a plain one.
SLICE = [BY_ID[i] for i in ("c01", "c13", "c18", "c20", "c26")]
#: Uzbek Latin function words, repeated: the language check needs a clear winner even
#: when a conversation's values are Cyrillic or English.
CAVEAT_KEYS = ("caveat_presence", "spurious_caveats")
UZ_FILLER = "Bu suhbat haqida va men uchun kerak. " * 12


def open_loops_of(conversation: Conversation) -> list[Any]:
    return [
        *(q for q in conversation.labels.questions_to_owner if q.status == "open"),
        *(c for c in conversation.labels.commitments if c.status == "open"),
    ]


def words_of(item: Any) -> str:
    """What a faithful summary says of an open loop: its literal values, else the
    label's own description."""
    description = item.summary if isinstance(item, QuestionToOwner) else item.what
    return " ".join(item.values) or str(description)


def distinctive_loop() -> tuple[Conversation, Any]:
    """An open loop with `values` that no other loop of its chat repeats, so that
    leaving it out of a summary is a clean, checkable miss."""
    for conversation in E1:
        loops = open_loops_of(conversation)
        for loop in loops:
            if not loop.values:
                continue
            rest = UZ_FILLER + " ".join(words_of(other) for other in loops if other is not loop)
            if values_matcher(loop, rest) is not True:
                return conversation, loop
    raise AssertionError("E1 has no distinctive open loop")


class SummaryOracle(FakeLLM):
    """A scripted 'perfect summarizer and judge'. Conversations are summarized in
    the order given (the target runs them in order); it reads the dataset only to
    *play the model*, never the code under test. Options script the failures."""

    def __init__(
        self,
        conversations: list[Conversation],
        *,
        omit: Collection[str] = (),
        truncate_first: bool = False,
        refuse: Collection[str] = (),
        misquote: Collection[str] = (),
        nearquote: Collection[str] = (),
        flags: dict[str, list[dict[str, Any]]] | None = None,
        during_summary: Callable[[LLMRequest], Awaitable[None]] | None = None,
        usage: Usage = Usage(400, 100),  # noqa: B008  (immutable)
        filler: str = UZ_FILLER,
    ) -> None:
        super().__init__()
        self._order = conversations
        self._position = -1
        self._attempts = 0
        self._omit = set(omit)
        self._truncate_first = truncate_first
        self._refuse = set(refuse)
        self._misquote = set(misquote)
        self._nearquote = set(nearquote)
        self._flags = flags or {}
        self._during_summary = during_summary
        self._usage = usage
        self._filler = filler

    @property
    def current(self) -> Conversation:
        return self._order[self._position]

    async def parse[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> ParsedResponse[T]:
        self.requests.append(request)
        if request.purpose == SUMMARIZE_PURPOSE:
            if self._during_summary is not None:
                await self._during_summary(request)  # the chat changes while the model works
            return self._summarize(request, schema)
        if request.purpose == FABRICATION_PURPOSE:
            value: dict[str, Any] = {"flags": self._flags.get(self.current.id, [])}
        else:
            assert request.purpose == LOOPS_PURPOSE
            text = request.items[0].text  # type: ignore[union-attr]
            asked = re.findall(r"^- (c\d\d\.[qc]\d+):", text, re.M)
            summary = text.split("\n\nSummary:\n", 1)[1]
            value = {"items": [self._verdict(i, summary) for i in asked]}
        return ParsedResponse(StopReason.COMPLETED, LUNA, self._usage, schema.model_validate(value))

    def _verdict(self, loop_id: str, summary: str) -> dict[str, Any]:
        """A faithful judge quotes the summary line that carries the loop (S03.7 A2:
        quotes are checked); `misquote` scripts a quote of a line that is not there."""
        if loop_id in self._misquote:
            return {
                "id": loop_id,
                "evidence": "A line this summary does not have",
                "surfaced": True,
            }
        if loop_id in self._omit:
            return {"id": loop_id, "evidence": "", "surfaced": False}
        loop = next(item for item in open_loops_of(self.current) if item.id == loop_id)
        line = next(line for line in summary.splitlines() if words_of(loop) in line)
        if loop_id in self._nearquote:
            # `nearquote` scripts a copy with one Latin letter in Cyrillic (E-af round 3).
            slip = next(i for i, char in enumerate(line) if char in "aoe")
            line = line[:slip] + {"a": "а", "o": "о", "e": "е"}[line[slip]] + line[slip + 1 :]  # noqa: RUF001
        return {"id": loop_id, "evidence": line, "surfaced": True}

    def _summarize[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> ParsedResponse[T]:
        assert request.tools == ()
        if self._attempts == 0:
            self._position += 1
        self._attempts += 1
        conversation = self.current
        if conversation.id in self._refuse:
            self._attempts = 0
            return ParsedResponse(StopReason.REFUSAL, LUNA, self._usage)
        if self._truncate_first and self._attempts == 1:
            return ParsedResponse(StopReason.MAX_TOKENS, LUNA, self._usage)
        self._attempts = 0
        lines = request.items[-1].text  # type: ignore[union-attr]
        # Cite as a faithful summarizer would (S03.7 checks who a cited message is
        # from): a question cites the contact, a promise its maker's side.
        sides = {
            label: [
                int(i)
                for i, side in re.findall(r"^\[(\d+)\] \S+ \S+ (\w+)", lines, re.M)
                if side == label
            ]
            for label in ("CONTACT", "YOU")
        }
        return ParsedResponse(
            StopReason.COMPLETED,
            LUNA,
            self._usage,
            schema.model_validate(self._summary(conversation, sides)),
        )

    def _summary(self, conversation: Conversation, sides: dict[str, list[int]]) -> dict[str, Any]:
        contact = sides["CONTACT"][:1] or sides["YOU"][:1]
        owner = sides["YOU"][:1] or contact
        questions = [
            {"text": words_of(q), "message_ids": contact}
            for q in conversation.labels.questions_to_owner
            if q.status == "open" and q.id not in self._omit
        ]
        commitments = [
            {
                "by": "owner" if c.who == "owner" else "contact",
                "status": "promised",
                "text": words_of(c),
                "due": c.when,
                "message_ids": owner if c.who == "owner" else contact,
            }
            for c in conversation.labels.commitments
            if c.status == "open" and c.id not in self._omit
        ]
        return {
            "overview": self._filler,
            "open_questions_to_owner": questions,
            "commitments": commitments,
            "decisions_requested": [],
            "key_facts": [],
            "needs_reply": True,
            "needs_reply_reason": None,
            "language": "uz",
        }


@pytest.fixture
async def factory(migrated_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


def target(
    factory: async_sessionmaker[AsyncSession],
    conversations: list[Conversation] | None = None,
    **kwargs: Any,
) -> SummaryTarget:
    return SummaryTarget(
        conversations or SLICE, owner_language="uz-Latn", session_factory=factory, **kwargs
    )


def context(llm: FakeLLM, ledger: CallLedger | None = None, **kwargs: Any) -> RunContext:
    budgeted = BudgetedLLMClient(llm, cap_usd=Decimal("1000"), ledger=ledger or CallLedger())
    return RunContext(llm=budgeted, model=LUNA, effort="low", **kwargs)


def metric(result: Any, key: str) -> Any:
    return next(m for m in result.metrics if m.key == key)


def record(result: Any, conversation_id: str) -> dict[str, Any]:
    return next(d for d in result.details if d["id"] == conversation_id)


# ------------------------------------------------------------------- end to end
async def test_the_target_runs_the_real_summarize_path_and_produces_every_metric(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = SummaryOracle(SLICE)
    result = await target(factory).run(context(llm))

    assert [m.key for m in result.metrics] == [
        "open_loop_recall",
        "caveat_presence",
        "spurious_caveats",
        "owner_language",
        "fabricated_facts",
    ]
    assert (result.items, result.items_planned, result.items_total) == (5, 5, 5)
    assert result.prompts == (
        prompt_registry.active("summarize").label,
        # S03.6 review: the judge input changed (times in the owner's timezone,
        # E-w), so the label names the input version; plain "fabrication.v1"
        # was the UTC input of the earlier runs. S03.7: the summary the judge
        # reads names who asked or promised in the product's words (labels.v2).
        # S03.7 A2: both rubrics say "you" in the summary is the owner, and the
        # judge reads when an edited message was edited (render.v3).
        "fabrication.v2+render.v3+labels.v2",
        "open_loops.v4",
    )
    assert all(d["status"] == "summarized" and d["error"] is None for d in result.details)
    # The report keeps the structured summary too: who promised what (attribution
    # cannot be reviewed from the plain text the checks read).
    assert all("Commitments not yet done:" in d["summary_labelled"] for d in result.details)
    # S03.7: what the attribution check changed is counted per conversation.
    for detail in result.details:
        assert detail["attribution_demoted"] >= 0
        assert detail["attribution_dropped"] == 0  # the oracle cites the right sides
    # A perfect summarizer and judge: every open loop surfaced, nothing flagged.
    assert metric(result, "open_loop_recall").all_items.ratio == 1.0
    assert metric(result, "owner_language").all_items.ratio == 1.0
    assert metric(result, "fabricated_facts").all_items.numerator == 0
    # The same prompt, schema and no-tools rule the product uses.
    summarize = [r for r in llm.requests if r.purpose == SUMMARIZE_PURPOSE]
    assert len(summarize) == 5
    for request in summarize:
        assert request.tools == ()
        assert prompt_registry.active("summarize").text in request.instructions
        assert "preferred language: uz-Latn" in request.instructions
        assert request.prompt_cache_key == f"summarize:{prompt_registry.active('summarize').label}"
        assert request.effort == "low"


async def test_caveats_come_from_the_product_renderer_by_gap_id(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await target(factory).run(context(SummaryOracle(SLICE)))

    assert record(result, "c13")["caveat_gap_ids"] == ["c13.g1"]  # possible
    assert record(result, "c18")["caveat_gap_ids"] == ["c18.g1"]  # definite
    assert record(result, "c20")["caveat_pre_connection"] is True
    assert record(result, "c20")["caveat_gap_ids"] == []
    # The delayed-only gap lost nothing: the renderer says nothing, so nothing is spurious.
    assert record(result, "c26")["caveat_gap_ids"] == []
    assert record(result, "c26")["caveats_spurious"] == []
    # No chat carries a coverage-start note unless the dataset expects one.
    for plain in ("c01", "c13", "c18", "c26"):
        assert record(result, plain)["caveat_pre_connection"] is False
    presence, spurious = metric(result, "caveat_presence"), metric(result, "spurious_caveats")
    assert (presence.all_items.numerator, presence.all_items.denominator) == (3, 3)
    assert spurious.all_items.numerator == 0
    assert presence.confirmation() == (0, 3)  # k of N where N is the chats with caveats


async def test_each_summary_call_sees_only_its_own_conversation_and_no_label(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = SummaryOracle(SLICE)
    await target(factory).run(context(llm))

    def normalized(text: str) -> str:
        # What the untrusted wrapper does to text (NFKC, then entity escapes).
        return unicodedata.normalize("NFKC", html.unescape(text))

    summarize = [r for r in llm.requests if r.purpose == SUMMARIZE_PURPOSE]
    seen_ids: list[set[int]] = []
    for conversation, request in zip(SLICE, summarize, strict=True):
        body = " ".join(i.text for i in request.items if isinstance(i, TextItem))
        sent = normalized(request.instructions + " " + body)
        # Its own messages, all of them and only them: the ids shown are the retained rows.
        ids = {int(i) for i in re.findall(r"^\[(\d+)\]", body, re.M)}
        assert len(ids) == sum(not m.deleted for m in conversation.messages)
        assert all(not ids & earlier for earlier in seen_ids)
        seen_ids.append(ids)
        own = [normalized(m.text) for m in conversation.messages if m.text]
        assert all(text in sent for text in own)
        # The answer key and reviewer-only text never reach the summarizer.
        hidden = [
            *(m.original_text for m in conversation.messages),
            *(m.purged_text for m in conversation.messages),
            *(i.summary for i in conversation.labels.questions_to_owner),
            *(t.statement for t in conversation.labels.must_not_state),
        ]
        for text in hidden:
            if text and not any(normalized(text) in mine for mine in own):
                assert normalized(text) not in sent
        # Other chats' messages are not in it (long texts only: short ones occur anywhere).
        foreign = [
            normalized(m.text)
            for c in E1
            if c.id != conversation.id
            for m in c.messages
            if m.text and len(m.text) > 30
        ]
        assert not any(text in sent and not any(text in mine for mine in own) for text in foreign)


async def test_every_conversation_has_its_own_account_and_leaves_nothing_behind(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async def accounts() -> int:
        async with factory() as session:
            return (await session.execute(select(func.count()).select_from(Account))).scalar_one()

    before = await accounts()
    llm = SummaryOracle(SLICE)
    await target(factory).run(context(llm))
    assert await accounts() == before
    owners = [r.account_id for r in llm.requests if r.purpose == SUMMARIZE_PURPOSE]
    assert len(owners) == 5
    assert len(set(owners)) == 5  # one account per conversation, none shared


async def test_a_failure_while_preparing_a_chat_leaves_no_account_behind(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The messages are committed before the consents and the connection are added:
    a failure in between must still remove the account (the cleanup covers the whole
    chat from the moment it is stored)."""

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the consent could not be written")

    async def accounts() -> int:
        async with factory() as session:
            return (await session.execute(select(func.count()).select_from(Account))).scalar_one()

    monkeypatch.setattr(summary_module, "grant_consent", boom)
    before = await accounts()
    with pytest.raises(RuntimeError, match="consent"):
        await target(factory).run(context(SummaryOracle(SLICE)))
    assert await accounts() == before


async def test_the_run_is_reproducible(factory: async_sessionmaker[AsyncSession]) -> None:
    first = await target(factory).run(context(SummaryOracle(SLICE)))
    second = await target(factory).run(context(SummaryOracle(SLICE)))
    assert first.details == second.details


# ------------------------------------------------------------------ the metrics
async def test_a_summary_that_misses_an_open_loop_lowers_recall(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    chat, missed = distinctive_loop()
    perfect = await target(factory, [chat]).run(context(SummaryOracle([chat])))
    flawed = await target(factory, [chat]).run(context(SummaryOracle([chat], omit={missed.id})))

    assert metric(perfect, "open_loop_recall").failed_items == ()
    recall = metric(flawed, "open_loop_recall")
    assert recall.failed_items == (missed.id,)
    assert recall.all_items.ratio is not None
    assert recall.all_items.ratio < 1.0
    assert recall.all_items.numerator == recall.all_items.denominator - 1
    # Not found by string: undecided (E-s), and the judge rules it missed.
    assert record(flawed, chat.id)["open_loops"][missed.id] is None
    assert record(flawed, chat.id)["open_loops_judged"][missed.id] is False


async def test_a_loop_verdict_quoting_a_line_not_in_the_summary_is_reported_as_missed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """S03.7 A2: the loop judge's quote is checked against the summary it judged;
    a quote that does not occur is no evidence: the loop is missed in the
    pre-screen, and the report lists it for the owner's review."""
    chat = chat_with_unchecked_loops()
    unchecked = [loop.id for loop in open_loops_of(chat) if not loop.values]
    result = await target(factory, [chat]).run(
        context(SummaryOracle([chat], misquote={unchecked[0]}))
    )
    detail = record(result, chat.id)
    assert detail["open_loops_judged"][unchecked[0]] is False
    assert detail["open_loops_unquoted"] == [unchecked[0]]
    assert all(detail["open_loops_judged"][loop] for loop in unchecked[1:])
    assert unchecked[0] in metric(result, "open_loop_recall").failed_items
    assert detail["open_loops_near_quotes"] == []


async def test_a_loop_verdict_quoting_a_line_with_a_slip_counts_and_is_listed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """E-af round 3: a quote copied with a one-letter slip (a Latin letter swapped
    for its Cyrillic look-alike) is the summary's line: the loop counts as surfaced
    in the pre-screen, and the record lists it as a near quote for the owner."""
    chat = chat_with_unchecked_loops()
    unchecked = [loop.id for loop in open_loops_of(chat) if not loop.values]
    result = await target(factory, [chat]).run(
        context(SummaryOracle([chat], nearquote={unchecked[0]}))
    )
    detail = record(result, chat.id)
    assert detail["open_loops_judged"][unchecked[0]] is True
    assert detail["open_loops_near_quotes"] == [unchecked[0]]
    assert detail["open_loops_unquoted"] == []
    assert unchecked[0] not in metric(result, "open_loop_recall").failed_items


async def test_a_run_whose_caveat_is_dropped_fails_the_caveat_metric(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The caveat is the renderer's, not the model's (E-p). A scripted renderer that
    loses the gap must fail presence (the seam this metric exists to guard)."""

    async def forgetful(
        session: AsyncSession, account_id: int, chat: int, start: datetime, end: datetime
    ) -> PeriodCoverage:
        return replace(await product_coverage(session, account_id, chat, start, end), gaps=())

    everything = ReviewLedger(frozenset(c.id for c in SLICE), {}, {})
    result = await target(factory, coverage_reader=forgetful).run(
        context(SummaryOracle(SLICE), reviews=everything)
    )

    presence = metric(result, "caveat_presence")
    assert set(presence.failed_items) == {"c13", "c18"}
    assert presence.all_items.numerator == 1  # only the pre-connection chat kept its caveat
    assert presence.prescreen_status() is Status.FAIL  # the regression check catches it
    assert presence.gate_status() is Status.INFO  # and it is never a gate (E-p)
    assert record(result, "c13")["caveats_missing"] == ["c13.v1"]


async def test_the_caveat_metrics_are_a_wiring_check_not_a_gate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The caveats come from the harness's own gap rows and the product renderer, not
    from the model, so they hold even when every summary failed. They must not read as
    "the plan's caveat threshold passed": ungated, with a note saying what they are not."""
    everything = ReviewLedger(frozenset(c.id for c in SLICE), {}, {})
    result = await target(factory).run(
        context(SummaryOracle(SLICE, refuse={c.id for c in SLICE}), reviews=everything)
    )
    assert all(d["error"] for d in result.details)  # every summary failed
    for key in CAVEAT_KEYS:
        caveats = metric(result, key)
        assert caveats.prescreen_status() is Status.PASS  # true by construction
        assert caveats.gate_status() is Status.INFO
        assert not caveats.gated
        assert "final reply keeps every caveat" in caveats.note
        assert "E-p" in caveats.note
    assert metric(result, "open_loop_recall").gate_status() is Status.FAIL


async def test_a_caveat_for_a_gap_that_lost_nothing_is_spurious(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    c26 = BY_ID["c26"]

    async def eager(
        session: AsyncSession, account_id: int, chat: int, start: datetime, end: datetime
    ) -> PeriodCoverage:
        real = await product_coverage(session, account_id, chat, start, end)
        return replace(
            real,
            gaps=(GapFact("outage", "possible", start, end, chat),),  # a gap nobody labelled
        )

    result = await target(factory, [c26], coverage_reader=eager).run(context(SummaryOracle([c26])))
    assert metric(result, "spurious_caveats").all_items.numerator == 1
    assert record(result, "c26")["caveats_spurious"] == ["gap:unknown-gap-1"]


def chat_with_unchecked_loops() -> Conversation:
    """A conversation with open loops that have no `values`: only an owner verdict
    (or the judge, as a pre-screen) can say whether a summary surfaced them."""
    return next(c for c in E1 if any(not loop.values for loop in open_loops_of(c)))


def verdicts_for(result: Any, chat: Conversation, verdict: str = "covered") -> dict[str, str]:
    """The owner's ledger entries for the loops of `chat` that string matching cannot
    decide, keyed the way the report names them (loop id and a digest of the summary)."""
    keys = record(result, chat.id)["open_loop_keys"]
    assert keys, "the report must name the key of every loop awaiting a verdict"
    return dict.fromkeys(keys.values(), verdict)


async def test_a_failed_summary_cannot_be_ruled_covered_by_the_ledger(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A refused summary surfaces nothing. A verdict in the ledger (here under both
    the old loop-id key and the key of the empty text) must not turn that into a pass."""
    chat = chat_with_unchecked_loops()
    unchecked = [loop.id for loop in open_loops_of(chat) if not loop.values]
    empty_text_keys = {coverage_key(loop, ""): "covered" for loop in unchecked}
    ledger = ReviewLedger(
        frozenset({chat.id}), {}, {**dict.fromkeys(unchecked, "covered"), **empty_text_keys}
    )
    result = await target(factory, [chat]).run(
        context(SummaryOracle([chat], refuse={chat.id}), reviews=ledger)
    )

    assert record(result, chat.id)["error"] is not None
    assert not any(record(result, chat.id)["open_loops"].values())  # every loop False
    recall = metric(result, "open_loop_recall")
    assert recall.confirmed.numerator == 0
    assert recall.pending == 0  # a miss, not "unresolved"
    assert recall.gate_status() is Status.FAIL


async def test_an_owner_verdict_belongs_to_the_summary_it_judged(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    chat = chat_with_unchecked_loops()
    unchecked = [loop.id for loop in open_loops_of(chat) if not loop.values]
    first = await target(factory, [chat]).run(context(SummaryOracle([chat])))
    ledger = ReviewLedger(frozenset({chat.id}), {}, verdicts_for(first, chat))  # type: ignore[arg-type]

    same = await target(factory, [chat]).run(context(SummaryOracle([chat]), reviews=ledger))
    recall = metric(same, "open_loop_recall")
    assert recall.pending == 0
    assert recall.gate_status() is Status.PASS

    # Another run, another summary: the old verdicts do not carry over.
    other = await target(factory, [chat]).run(
        context(
            SummaryOracle([chat], filler=UZ_FILLER + "Yana bir gap va men uchun. "), reviews=ledger
        )
    )
    recall = metric(other, "open_loop_recall")
    assert recall.pending == len(unchecked)
    assert recall.gate_status() is Status.NOT_EVALUABLE
    # and a verdict under the bare loop id never counts for the summary target
    bare = ReviewLedger(frozenset({chat.id}), {}, dict.fromkeys(unchecked, "covered"))
    unresolved = await target(factory, [chat]).run(context(SummaryOracle([chat]), reviews=bare))
    assert metric(unresolved, "open_loop_recall").pending == len(unchecked)


async def test_a_judge_flag_is_pending_and_keeps_the_gate_open(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    c01 = BY_ID["c01"]
    flag = {"statement": "Meeting at noon", "reason": "unsupported", "evidence_message_ids": []}
    result = await target(factory, [c01]).run(
        context(
            SummaryOracle([c01], flags={"c01": [flag]}),
            reviews=ReviewLedger(frozenset({"c01"}), {}, {}),
        )
    )
    fabrication = metric(result, "fabricated_facts")
    assert fabrication.all_items.numerator == 1
    assert fabrication.pending == 1
    assert fabrication.gate_status() is Status.NOT_EVALUABLE
    assert record(result, "c01")["flags"][0]["statement"] == "Meeting at noon"


# --------------------------------------------------------------------- the gate
async def test_the_gate_is_not_evaluable_without_owner_confirmations(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    result = await target(factory).run(context(SummaryOracle(SLICE)))
    for m in result.metrics:
        assert m.confirmation()[0] == 0
        assert m.prescreen_status() is Status.PASS  # the pre-screen alone is never a gate
        if m.key in CAVEAT_KEYS:
            assert m.gate_status() is Status.INFO  # a wiring check, not a gate (E-p)
        else:
            assert m.gate_status() is Status.NOT_EVALUABLE, m.key


async def test_a_fully_confirmed_set_has_an_evaluable_gate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    everything = ReviewLedger(frozenset(c.id for c in SLICE), {}, {})
    result = await target(factory).run(context(SummaryOracle(SLICE), reviews=everything))
    for key in ("owner_language", "fabricated_facts"):
        assert metric(result, key).gate_status() is Status.PASS, key
    # Recall also needs the owner's verdict on the loops that have no `values`: without
    # it the gate stays open, with it (for exactly this summary) the gate decides.
    recall = metric(result, "open_loop_recall")
    assert recall.pending > 0
    assert recall.gate_status() is Status.NOT_EVALUABLE

    verdicts: dict[str, str] = {}
    for chat in SLICE:
        if record(result, chat.id)["open_loop_keys"]:
            verdicts.update(verdicts_for(result, chat))
    assert verdicts, "the slice must hold loops that need the owner's verdict"
    ruled = ReviewLedger(frozenset(c.id for c in SLICE), {}, verdicts)  # type: ignore[arg-type]
    final = await target(factory).run(context(SummaryOracle(SLICE), reviews=ruled))
    assert metric(final, "open_loop_recall").pending == 0
    assert metric(final, "open_loop_recall").gate_status() is Status.PASS


async def test_a_limited_run_is_never_a_gate_even_fully_confirmed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    everything = ReviewLedger(frozenset(c.id for c in E1), {}, {})
    result = await SummaryTarget(E1, owner_language="uz-Latn", session_factory=factory).run(
        context(SummaryOracle(E1[:3]), limit=3, reviews=everything)
    )

    assert (result.items, result.items_planned, result.items_total) == (3, 3, 40)
    assert result.trial
    for m in result.metrics:
        assert m.gate_status() is not Status.PASS, m.key
    assert metric(result, "owner_language").confirmation() == (3, 40)


# ----------------------------------------------------------------- failures, cost
async def test_a_truncated_summary_is_retried_once_and_the_estimate_covers_it(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    llm, ledger = SummaryOracle(SLICE, truncate_first=True), CallLedger()
    subject = target(factory)
    ctx = context(llm, ledger)
    result = await subject.run(ctx)

    assert [d["summary_calls"] for d in result.details] == [2] * 5
    assert any("truncated once and retried" in n for n in result.notes)
    planned = Counter((c.purpose, c.max_output_tokens) for c in subject.plan(ctx))
    made = Counter((r.purpose, r.max_output_tokens) for r in llm.requests)
    assert not made - planned  # every call made was planned, retries included
    assert made[(SUMMARIZE_PURPOSE, 4000)] == 5
    # And priced: the run's worst case (what the in-flight guard charges per call) fits.
    bound = estimate(subject.plan(ctx), LUNA, Decimal("1000"), {}).upper_bound_usd
    charged = sum(
        (worst_case(LUNA, _request_chars(r), r.max_output_tokens, {}) for r in llm.requests),
        Decimal(0),
    )
    assert charged <= bound
    assert len(llm.requests) == len(ledger.entries)  # nothing was called unmetered


async def test_a_refused_summary_is_a_miss_everywhere_and_the_run_goes_on(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = SummaryOracle(SLICE, refuse={"c13"})
    result = await target(factory).run(context(llm))

    bad = record(result, "c13")
    assert bad["status"] == "refused"
    assert bad["error"].startswith("summary:refused")
    assert bad["screened"] is False
    assert bad["language_ok"] is False
    assert not any(bad["open_loops"].values())  # missed, or not judgeable
    assert result.items == 5  # the others ran
    assert record(result, "c18")["error"] is None
    assert any("failed at the model" in n and "c13" in n for n in result.notes)
    # Not judged: nothing to judge. The failure leaves the fabrication gate open.
    assert [r.purpose for r in llm.requests].count(FABRICATION_PURPOSE) == 4
    assert metric(result, "fabricated_facts").pending >= 1
    # The caveat is deterministic and is still reported for the chat.
    assert bad["caveat_gap_ids"] == ["c13.g1"]


async def test_a_chat_that_changed_during_its_summary_is_a_failure_not_a_summary(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """S03.2 discards a summary written from messages that changed while the model ran
    (a deletion, an edit, expiry, Ignore, a purge). The target counts that outcome like
    any other that is not `summarized` (E-r): a miss, unscreened, listed with its reason."""
    deleted: list[int] = []

    async def delete_first_message(request: LLMRequest) -> None:
        if deleted:  # only the first chat is disturbed
            return
        async with factory() as session, session.begin():
            first = (
                await session.execute(
                    select(func.min(Message.id)).where(Message.account_id == request.account_id)
                )
            ).scalar_one()
            await session.execute(
                update(Message)
                .where(Message.id == first)
                .values(deleted_at=func.now(), text=None, search_key=None)
            )
            deleted.append(first)

    llm = SummaryOracle(SLICE, during_summary=delete_first_message)
    result = await target(factory).run(context(llm))

    bad = record(result, SLICE[0].id)
    assert bad["status"] == "discarded"
    assert bad["error"] == "summary:discarded:changed"
    assert bad["summary_calls"] == 1  # the call that was paid for
    assert bad["screened"] is False
    assert not any(bad["open_loops"].values())
    assert bad["summary_text"] == ""  # nothing the model wrote is shown or judged
    assert record(result, SLICE[1].id)["error"] is None  # the others ran
    assert [r.purpose for r in llm.requests].count(FABRICATION_PURPOSE) == len(SLICE) - 1
    assert any("failed at the model" in n and SLICE[0].id in n for n in result.notes)


async def test_an_unavailable_model_is_a_miss_not_a_crash(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    class Down(SummaryOracle):
        async def parse[T: BaseModel](
            self, request: LLMRequest, schema: type[T]
        ) -> ParsedResponse[T]:
            raise LLMError(LLMErrorKind.UNAVAILABLE)

    result = await target(factory).run(context(Down(SLICE)))
    assert all(d["error"].startswith("summary:failed:llm_unavailable") for d in result.details)
    assert metric(result, "open_loop_recall").all_items.numerator == 0
    assert metric(result, "owner_language").all_items.numerator == 0


async def test_a_budget_stop_ends_the_run_early_and_the_gate_cannot_pass(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    llm, ledger = SummaryOracle(SLICE, usage=Usage(200, 1500)), CallLedger()
    capped = BudgetedLLMClient(llm, cap_usd=Decimal("0.01"), ledger=ledger)
    everything = ReviewLedger(frozenset(c.id for c in SLICE), {}, {})
    result = await target(factory).run(
        RunContext(llm=capped, model=LUNA, effort="low", reviews=everything)
    )

    assert result.stopped
    assert not result.complete
    assert result.items < result.items_planned == 5
    assert any("STOPPED EARLY" in n for n in result.notes)
    assert 0 < ledger.spent <= Decimal("0.01")
    for m in result.metrics:
        assert m.gate_status() is not Status.PASS
    assert len(llm.requests) == len(ledger.entries)


async def test_the_target_needs_a_model_and_a_bound_database(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ValueError, match="needs a model"):
        await target(factory).run(RunContext())
    with pytest.raises(RuntimeError, match="bind"):
        await SummaryTarget(SLICE, owner_language="uz-Latn").run(context(SummaryOracle(SLICE)))


# ------------------------------------------------------------- scratch database
async def test_the_throwaway_database_is_migrated_isolated_and_dropped(
    test_database_url: str,
) -> None:
    chat = SLICE[:1]
    async with isolated_summary(
        test_database_url, SummaryTarget(chat, owner_language="uz-Latn")
    ) as bound:
        name = bound._database.kw["bind"].url.database
        assert name.startswith("assistant_eval_")
        assert name != sa.engine.make_url(test_database_url).database
        result = await bound.run(context(SummaryOracle(chat)))
        assert result.items == 1

    with psycopg.connect(admin_url(test_database_url), autocommit=True) as conn:
        left = conn.execute(
            "SELECT count(*) FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
    assert left == (0,)


def test_cleanup_lists_and_drops_only_the_leftover_evaluation_databases(
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A killed run cannot drop its throwaway database; `cleanup` finds it by its
    exact name pattern, lists it, and drops it only with --yes. Look-alike names stay."""
    monkeypatch.setenv("EVAL_DATABASE_URL", test_database_url)
    leftover, lookalike = "assistant_eval_deadbeef", "assistant_eval_notes"
    create_scratch_database(test_database_url, leftover)
    create_scratch_database(test_database_url, lookalike)
    try:
        assert cli.main(["cleanup"]) == 0
        listed = capsys.readouterr().out
        assert leftover in listed
        assert lookalike not in listed
        assert "--yes" in listed
        assert leftover in _databases(test_database_url)  # listing drops nothing

        assert cli.main(["cleanup", "--yes"]) == 0
        assert "dropped 1 database" in capsys.readouterr().out
        assert leftover not in _databases(test_database_url)
        assert lookalike in _databases(test_database_url)
        assert cli.main(["cleanup"]) == 0
        assert "no leftover" in capsys.readouterr().out
    finally:
        drop_scratch_database(test_database_url, leftover)
        drop_scratch_database(test_database_url, lookalike)


def test_cleanup_never_touches_a_database_a_run_is_using(
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """S03.6, the hazard: `cleanup --yes` (here, in the test above) dropped a live
    run's throwaway database mid-run. A database with an open connection belongs to a
    run in progress: it is neither listed nor dropped, and the drop is never forced."""
    monkeypatch.setenv("EVAL_DATABASE_URL", test_database_url)
    busy, leftover = "assistant_eval_0badc0de", "assistant_eval_deadbeef"
    create_scratch_database(test_database_url, busy)
    create_scratch_database(test_database_url, leftover)
    run_url = sa.engine.make_url(test_database_url).set(drivername="postgresql", database=busy)
    try:
        with psycopg.connect(run_url.render_as_string(hide_password=False)):  # the "run"
            assert cli.main(["cleanup", "--yes"]) == 0
            out = capsys.readouterr().out
            assert busy not in out
            assert leftover in out
            assert busy in _databases(test_database_url)
            assert leftover not in _databases(test_database_url)
            # Even named explicitly, a database in use is not dropped.
            assert corpus.drop_leftover_databases(test_database_url, [busy]) == []
            assert busy in _databases(test_database_url)
    finally:
        drop_scratch_database(test_database_url, busy)
        drop_scratch_database(test_database_url, leftover)


# ------------------------------------------------------------------ the CLI, live
def _databases(url: str) -> set[str]:
    with psycopg.connect(admin_url(url), autocommit=True) as conn:
        rows = conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'assistant_eval_%'")
        return {row[0] for row in rows.fetchall()}


def _no_settings() -> Settings:
    raise LiveSetupError("the settings are invalid (test)")


def _wire(monkeypatch: pytest.MonkeyPatch, url: str, llm: FakeLLM) -> None:
    monkeypatch.setenv("EVAL_DATABASE_URL", url)
    monkeypatch.setattr(live, "_settings", _no_settings)
    monkeypatch.setattr(
        cli,
        "load_live_config",
        lambda route, override: LiveConfig(ModelChoice(LUNA, "low"), SecretStr("sk-x")),
    )
    monkeypatch.setattr(cli, "build_client", lambda config: llm)


def test_run_summary_works_end_to_end_on_a_throwaway_database(
    tmp_path: Path,
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _databases(test_database_url)
    first_out, second_out = tmp_path / "a", tmp_path / "b"
    reports: list[dict[str, Any]] = []
    for out in (first_out, second_out):
        _wire(monkeypatch, test_database_url, SummaryOracle(E1[:2]))
        code = cli.main(
            [
                "run", "summary", "--model", LUNA, "--live", "--limit", "2",
                "--out-dir", str(out), "--date", "2026-10-25",
            ]
        )  # fmt: skip
        assert code == 0
        (machine,) = out.glob("*.json")
        reports.append(json.loads(machine.read_text(encoding="utf-8")))

    printed = capsys.readouterr().out
    assert "summary (period-uz-Latn): 2 items (TRIAL" in printed
    assert "Open question/commitment recall" in printed
    (markdown,) = first_out.glob("*.md")
    text = markdown.read_text(encoding="utf-8")
    assert "TRIAL RUN" in text
    assert prompt_registry.active("summarize").label in text
    assert markdown.name == f"2026-10-25-summary-{LUNA}-trial.md"
    for report in reports:
        assert report["set"] == "summary"
        assert report["mode"] == "period-uz-Latn"
        assert report["live"] is True
        assert report["complete"] is False
        assert {m["key"] for m in report["metrics"]} >= {"open_loop_recall", "fabricated_facts"}
        assert report["cost"]["calls"] >= 4  # two chats, each summarized and judged
    assert reports[0]["details"] == reports[1]["details"]  # deterministic on FakeLLM
    assert _databases(test_database_url) == before  # the throwaway databases are gone


def test_a_budget_stopped_summary_run_exits_non_zero_and_still_writes_its_report(
    tmp_path: Path,
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expensive = SummaryOracle(E1, usage=Usage(2_000_000, 400_000))  # USD 0.40 per call
    _wire(monkeypatch, test_database_url, expensive)
    code = cli.main(
        [
            "run", "summary", "--model", LUNA, "--live", "--budget-usd", "0.5", "--limit", "3",
            "--out-dir", str(tmp_path), "--date", "2026-10-25",
        ]
    )  # fmt: skip
    assert code == cli.EXIT_INCOMPLETE
    (report,) = tmp_path.glob("*.md")
    assert "STOPPED RUN" in report.read_text(encoding="utf-8")
    assert "STOPPED EARLY" in capsys.readouterr().out
    assert len(expensive.requests) == 2  # the third call was refused before it was made
