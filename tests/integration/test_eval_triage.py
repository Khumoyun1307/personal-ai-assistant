"""P2 E3 §B: the triage target over the product's `Triager` and effects store,
on a real PostgreSQL.

Small synthetic bursts are built here (E3 itself is E3D's); two tests use a few
real E3 bursts. The model is `FakeLLM` only: nothing here calls a real model or
Telegram.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from evals import cli, live
from evals.agent_mode import MAX_ATTEMPTS, RETRY_DELAYS
from evals.budget import BudgetedLLMClient, CallLedger
from evals.live import LiveConfig, LiveSetupError, ModelChoice
from evals.report import RunMeta
from evals.review import ReviewLedger
from evals.runner import run_target
from evals.schema import Burst, load_e3
from evals.targets import RunContext, Status
from evals.triage import TRIAGE_PURPOSE, TriageTarget, makes_a_call
from pydantic import BaseModel, SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.agent import prompt_registry
from assistant.config import Settings
from assistant.db.engine import build_engine, build_session_factory
from assistant.inbox.models import ConversationTriage, Notification, Task
from assistant.llm.port import (
    LLMError,
    LLMErrorKind,
    LLMRequest,
    ParsedResponse,
    StopReason,
    TextItem,
    Usage,
)
from tests.fakes.llm import FakeLLM
from tests.integration.conftest import admin_url
from tests.unit.test_eval_triage import burst

pytestmark = pytest.mark.integration

LUNA = "gpt-6-luna"
GATED = ("alert_recall", "false_alert_rate", "false_closure_rate", "resolution_recall")


# ------------------------------------------------------------------ the model
@dataclass(frozen=True)
class Answer:
    """What the scripted model concludes about one burst."""

    #: "urgent", "decision", "needs_reply" or None.
    alert: str | None = None
    #: Dataset ids of the open tasks it says are resolved.
    close: tuple[str, ...] = ()
    #: `(by, dataset message id)` commitments it reports.
    commitments: tuple[tuple[str, str], ...] = ()
    suspicious: bool = False


def sent(request: LLMRequest) -> str:
    """What the model was sent, as the untrusted wrapper leaves text (NFKC, entities)."""
    body = "\n".join(item.text for item in request.items if isinstance(item, TextItem))
    return unicodedata.normalize("NFKC", html.unescape(request.instructions + "\n" + body))


def shown_ids(request: LLMRequest) -> list[int]:
    return [int(i) for i in re.findall(r"^\[msg:(\d+)\]", sent(request), flags=re.MULTILINE)]


class TriageOracle(FakeLLM):
    """Plays the triage model, burst by burst in run order (bursts the product
    makes no call for are skipped). It cites messages by their position in the
    burst and tasks by their title, as a model reading the context would.

    `down` bursts fail on the provider at every attempt the harness makes;
    `flaky` bursts fail with the given errors first, then answer."""

    def __init__(
        self,
        bursts: Sequence[Burst],
        answers: dict[str, Answer] | None = None,
        *,
        refuse: Sequence[str] = (),
        down: Sequence[str] = (),
        flaky: Mapping[str, Sequence[LLMErrorKind]] | None = None,
        usage: Usage = Usage(400, 100),  # noqa: B008  (immutable)
    ) -> None:
        super().__init__()
        self._queue = [b for b in bursts if makes_a_call(b)]
        self._answers = answers or {}
        self._refuse = set(refuse)
        self._outages = {burst_id: [LLMErrorKind.UNAVAILABLE] * MAX_ATTEMPTS for burst_id in down}
        self._outages |= {burst_id: list(kinds) for burst_id, kinds in (flaky or {}).items()}
        self._usage = usage
        self._position = 0
        #: Failed calls on the current burst.
        self._failed = 0

    def _next(self) -> None:
        self._position += 1
        self._failed = 0

    async def parse[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> ParsedResponse[T]:
        self.requests.append(request)
        current = self._queue[self._position]
        outages = self._outages.get(current.id, [])
        if self._failed < len(outages):
            kind = outages[self._failed]
            self._failed += 1
            if self._failed == MAX_ATTEMPTS:  # the harness gives the burst up
                self._next()
            raise LLMError(kind)
        self._next()
        if current.id in self._refuse:
            return ParsedResponse(StopReason.REFUSAL, LUNA, self._usage)
        answer = self._answers.get(current.id, Answer())
        text = sent(request)
        message_ids = dict(zip([m.id for m in current.messages()], shown_ids(request), strict=True))
        task_ids = {
            task.id: int(found.group(1))
            for task in current.open_tasks
            if (found := re.search(rf"\[task:(\d+)\][^\n]*{re.escape(task.title)}", text))
        }
        value = {
            "language": "en",
            "needs_reply": answer.alert is not None,
            "contains_question_to_owner": answer.alert is not None,
            "requests_decision": answer.alert == "decision",
            "urgency": "urgent" if answer.alert == "urgent" else "normal",
            "deadline": None,
            "commitments": [
                {
                    "by": by,
                    "text": "Send the signed contract copy",
                    "due": None,
                    "source_message_id": message_ids[source],
                    "existing_task_id": None,
                }
                for by, source in answer.commitments
            ],
            "open_questions": [],
            "resolved_task_ids": [task_ids[task] for task in answer.close],
            "escalation_category": "suspicious" if answer.suspicious else "needs_owner",
            "reasons": ["The contact asks for a decision about the contract today"],
            "script": "latin",
        }
        return ParsedResponse(StopReason.COMPLETED, LUNA, self._usage, schema.model_validate(value))


# ------------------------------------------------------------------ helpers
@pytest.fixture
async def factory(migrated_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


def context(
    llm: FakeLLM,
    *,
    ledger: CallLedger | None = None,
    cap: str = "1000",
    reviews: ReviewLedger | None = None,
    limit: int | None = None,
) -> RunContext:
    budgeted = BudgetedLLMClient(llm, cap_usd=Decimal(cap), ledger=ledger or CallLedger())
    return RunContext(
        llm=budgeted,
        model=LUNA,
        effort="low",
        reviews=reviews or ReviewLedger.empty(),
        limit=limit,
    )


class Sleeps:
    """A fake `asyncio.sleep`: records the retry delays, waits for nothing."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def metric(result: Any, key: str) -> Any:
    return next(m for m in result.metrics if m.key == key)


def record(result: Any, burst_id: str) -> dict[str, Any]:
    return next(d for d in result.details if d["id"] == burst_id)


async def count(factory: async_sessionmaker[AsyncSession], model: type[Any]) -> int:
    async with factory() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def leftovers(factory: async_sessionmaker[AsyncSession]) -> tuple[int, ...]:
    return tuple(
        [await count(factory, m) for m in (Account, Task, Notification, ConversationTriage)]
    )


def confirming(bursts: Sequence[Burst]) -> ReviewLedger:
    return ReviewLedger(frozenset(b.id for b in bursts), {}, {})


#: A small set with every metric measurable: two positives, two negatives, one
#: resolved and one unresolved task.
def small_set() -> list[Burst]:
    return [
        burst("b001", alert=True, messages=[("contact", "SYNTH-1 Shall we sign today?")]),
        burst("b002", alert=True, messages=[("contact", "SYNTH-2 Pick a supplier please")]),
        burst("b003", messages=[("contact", "SYNTH-3 Thanks, all good")]),
        burst(
            "b004",
            history=[
                ("contact", "SYNTH-4 I will send the invoice"),
                ("contact", "SYNTH-4 And the receipt too"),
            ],
            open_tasks=[
                ("contact_commitment", "TASK-INVOICE", "b004.h01"),
                ("contact_commitment", "TASK-RECEIPT", "b004.h02"),
            ],
            messages=[("contact", "SYNTH-4 Invoice attached; the receipt comes later")],
            resolved=["b004.t1"],
        ),
    ]


PERFECT = {
    "b001": Answer(alert="urgent"),
    "b002": Answer(alert="decision"),
    "b004": Answer(close=("b004.t1",)),
}


# ------------------------------------------------------------------ the spec's tests
async def test_the_target_runs_the_product_triager_with_the_shipped_prompt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    # Real E3 bursts with history and open tasks: the product's context, prompt
    # and schema, one call each, no tools.
    bursts = [b for b in load_e3() if b.history and b.open_tasks][:3]
    llm = TriageOracle(bursts)
    result = await TriageTarget(bursts, session_factory=factory).run(context(llm))

    label = prompt_registry.active("triage").label
    assert result.prompts == (label,)
    assert (result.items, result.items_planned, result.items_total) == (3, 3, 3)
    assert [d["status"] for d in result.details] == ["triaged"] * 3
    assert [m.key for m in result.metrics][:4] == list(GATED)
    requests = [r for r in llm.requests if r.purpose == TRIAGE_PURPOSE]
    assert len(requests) == 3
    for request, item in zip(requests, bursts, strict=True):
        assert request.tools == ()
        assert request.model == LUNA
        assert request.effort == "low"
        assert request.instructions == prompt_registry.active("triage").text
        assert request.prompt_cache_key == f"triage:{label}"
        text = sent(request)
        # The history is "already triaged"; the burst is exactly what is new.
        new = text.split("New messages to triage:", 1)[1]
        assert len(re.findall(r"^\[msg:\d+\]", new, flags=re.MULTILINE)) == len(item.burst)
        assert len(re.findall(r"\[task:\d+\]", text)) == len(item.open_tasks)
        assert f"preferred language: {item.owner_language}" in text
    # Every verdict was stored by the product's store and read back.
    assert all(d["verdict"] is not None for d in result.details)


async def test_recall_counts_urgent_and_decision_alerts_only(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    bursts = [
        burst("b001", alert=True),
        burst("b002", alert=True),
        burst("b003", alert=True),
        burst("b004", needs_reply=True),
    ]
    answers = {
        "b001": Answer(alert="urgent"),
        "b002": Answer(alert="decision"),
        # A needs-reply alert is not an urgent/decision alert: a miss.
        "b003": Answer(alert="needs_reply"),
        "b004": Answer(alert="needs_reply"),
    }
    result = await TriageTarget(bursts, session_factory=factory).run(
        context(TriageOracle(bursts, answers))
    )

    recall = metric(result, "alert_recall")
    assert (recall.all_items.numerator, recall.all_items.denominator) == (2, 3)
    assert recall.failed_items == ("b003",)
    assert record(result, "b001")["alerts"] == ["triage_urgent"]
    assert record(result, "b002")["alerts"] == ["triage_decision"]
    assert record(result, "b003")["alerts"] == ["triage_needs_reply"]
    assert not record(result, "b003")["alerted"]
    # The negative's needs-reply alert is no false alert either.
    false_alerts = metric(result, "false_alert_rate")
    assert (false_alerts.all_items.numerator, false_alerts.all_items.denominator) == (0, 2)


async def test_false_alert_rate_is_alerted_negatives_over_alerts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    bursts = [burst("b001", alert=True), burst("b002"), burst("b003"), burst("b004")]
    answers = {
        "b001": Answer(alert="urgent"),
        "b002": Answer(alert="urgent"),
        "b003": Answer(alert="decision"),
    }
    result = await TriageTarget(bursts, session_factory=factory).run(
        context(TriageOracle(bursts, answers))
    )

    rate = metric(result, "false_alert_rate")
    assert (rate.goal, rate.threshold) == ("at_most", 0.20)
    assert (rate.all_items.numerator, rate.all_items.denominator) == (2, 3)
    assert set(rate.failed_items) == {"b002", "b003"}
    assert rate.prescreen_status() is Status.FAIL
    negatives = metric(result, "negatives_alerted")
    assert (negatives.all_items.numerator, negatives.all_items.denominator) == (2, 3)
    assert negatives.gate_status() is Status.INFO


async def test_false_closure_and_resolution_recall(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    tasks = [
        ("contact_commitment", "TASK-CONTRACT", "b001.h01"),
        ("contact_commitment", "TASK-SAMPLES", "b001.h02"),
    ]
    bursts = [
        burst(
            "b001",
            history=[
                ("contact", "SYNTH I will send the contract"),
                ("contact", "SYNTH And the samples"),
            ],
            open_tasks=tasks,
            messages=[("contact", "SYNTH Contract attached; samples next week, I promise")],
            resolved=["b001.t1"],
            commitments=[("contact", "b001.m01")],
        ),
        burst(
            "b002",
            history=[("contact", "SYNTH I will send the price list")],
            open_tasks=[("contact_commitment", "TASK-PRICES", "b002.h01")],
            messages=[("contact", "SYNTH Here is the price list")],
            resolved=["b002.t1"],
        ),
    ]
    answers = {
        # Closes the delivered contract and, wrongly, the promised samples; and
        # records the repeated promise as the contact's commitment.
        "b001": Answer(close=("b001.t1", "b001.t2"), commitments=(("contact", "b001.m01"),)),
        # Misses the delivered price list.
        "b002": Answer(),
    }
    result = await TriageTarget(bursts, session_factory=factory).run(
        context(TriageOracle(bursts, answers))
    )

    closures = metric(result, "false_closure_rate")
    assert (closures.all_items.numerator, closures.all_items.denominator) == (1, 1)
    assert closures.failed_items == ("b001.t2",)
    assert closures.prescreen_status() is Status.FAIL
    resolution = metric(result, "resolution_recall")
    assert (resolution.all_items.numerator, resolution.all_items.denominator) == (1, 2)
    assert resolution.failed_items == ("b002.t1",)
    assert record(result, "b001")["tasks_closed"] == ["b001.t1", "b001.t2"]
    # A closed task enqueues `task_closed`, which is not an urgent/decision alert.
    assert "task_closed" in record(result, "b001")["alerts"]
    assert not record(result, "b001")["alerted"]
    # The commitment is read back by side and source.
    (created,) = record(result, "b001")["tasks"]
    assert (created["kind"], created["sources"]) == ("contact_commitment", ["b001.m01"])
    assert metric(result, "commitment_recall").all_items.ratio == 1.0


async def test_gate_needs_owner_confirmed_bursts(factory: async_sessionmaker[AsyncSession]) -> None:
    bursts = small_set()
    target = TriageTarget(bursts, session_factory=factory)

    unconfirmed = await target.run(context(TriageOracle(bursts, PERFECT)))
    for key in GATED:
        assert metric(unconfirmed, key).prescreen_status() is Status.PASS
        assert metric(unconfirmed, key).gate_status() is Status.NOT_EVALUABLE

    # b001 (a positive) unconfirmed: recall cannot gate, and the false-alert rate
    # waits for every burst; the task metrics only need b004's tasks confirmed.
    partly = await target.run(
        context(TriageOracle(bursts, PERFECT), reviews=confirming(bursts[1:]))
    )
    assert metric(partly, "alert_recall").gate_status() is Status.NOT_EVALUABLE
    assert metric(partly, "false_alert_rate").gate_status() is Status.NOT_EVALUABLE
    assert metric(partly, "false_alert_rate").pending == 1
    assert metric(partly, "false_closure_rate").gate_status() is Status.PASS
    assert metric(partly, "resolution_recall").gate_status() is Status.PASS

    confirmed = await target.run(context(TriageOracle(bursts, PERFECT), reviews=confirming(bursts)))
    for key in GATED:
        assert metric(confirmed, key).gate_status() is Status.PASS
    assert confirmed.complete

    # A trial is never a gate, even fully confirmed.
    trial = await target.run(
        context(TriageOracle(bursts, PERFECT), reviews=confirming(bursts), limit=3)
    )
    assert trial.trial
    for key in GATED:
        assert metric(trial, key).gate_status() is not Status.PASS


async def test_each_burst_starts_from_a_clean_account(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    before = await leftovers(factory)
    bursts = [
        burst(
            "b001",
            history=[("owner", "SYNTH-FIRST I will send the deck")],
            open_tasks=[("owner_commitment", "TASK-FIRST-DECK", "b001.h01")],
            messages=[("contact", "SYNTH-FIRST Decide today please")],
            alert=True,
        ),
        burst("b002", messages=[("contact", "SYNTH-SECOND Thanks")]),
    ]
    llm = TriageOracle(
        bursts, {"b001": Answer(alert="urgent", commitments=(("contact", "b001.m01"),))}
    )
    result = await TriageTarget(bursts, session_factory=factory).run(context(llm))

    assert result.items == 2
    first, second = (sent(r) for r in llm.requests)
    assert "SYNTH-FIRST" in first
    assert "SYNTH-SECOND" not in first
    # Nothing of the first burst (messages, its task, its verdict) reaches the second.
    assert "SYNTH-FIRST" not in second
    assert "TASK-FIRST-DECK" not in second
    assert "Open tasks of this chat: none." in second
    assert llm.requests[0].account_id != llm.requests[1].account_id
    assert record(result, "b002")["alerts"] == []
    assert record(result, "b002")["tasks"] == []
    # Every account, task, alert and verdict the run made is gone.
    assert await leftovers(factory) == before


async def test_the_run_records_its_spend_and_respects_the_cap(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    bursts = small_set()
    target = TriageTarget(bursts, session_factory=factory)
    meta = RunMeta(
        run_date=date(2026, 10, 28),
        set_name="triage",
        mode=target.label,
        model=LUNA,
        live=True,
        effort="low",
        dataset_files={},
        cap_usd=Decimal("1000"),
    )

    # Recorded: every call is in the ledger and in the report's cost.
    llm, ledger = TriageOracle(bursts, PERFECT), CallLedger()
    outcome = await run_target(
        target, context(llm, ledger=ledger), meta=meta, ledger=ledger, out_dir=tmp_path
    )
    assert outcome.machine is not None
    report = json.loads(outcome.machine.read_text(encoding="utf-8"))
    assert report["cost"]["calls"] == len(llm.requests) == 4
    assert Decimal(report["cost"]["spent_usd"]) == ledger.spent > 0
    assert report["cost"]["per_model"][LUNA]["cost_per_100_items_usd"] is not None

    # Capped: the call that would cross the cap is never made, the run stops
    # early, no gate can pass, and nothing is left behind.
    before = await leftovers(factory)
    expensive = TriageOracle(bursts, PERFECT, usage=Usage(200, 1500))
    capped = CallLedger()
    stopped = await target.run(
        context(expensive, ledger=capped, cap="0.002", reviews=confirming(bursts))
    )
    assert stopped.stopped
    assert not stopped.complete
    assert stopped.items < stopped.items_planned == 4
    assert any("STOPPED EARLY" in n for n in stopped.notes)
    assert 0 < capped.spent <= Decimal("0.002")
    assert len(expensive.requests) == len(capped.entries)
    for m in stopped.metrics:
        assert m.gate_status() is not Status.PASS
    assert await leftovers(factory) == before


# ------------------------------------------------------------------ failures
async def test_a_refusal_is_a_miss_and_an_outage_is_not_evaluated(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    bursts = [burst("b001", alert=True), burst("b002", alert=True), burst("b003", alert=True)]
    llm = TriageOracle(bursts, {"b003": Answer(alert="urgent")}, refuse=["b001"], down=["b002"])
    sleeps = Sleeps()
    result = await TriageTarget(bursts, session_factory=factory, sleep=sleeps).run(context(llm))

    # The outage is not evaluated only after the bounded retries (the agent
    # mode's attempts and delays); the refusal is the model's answer, not retried.
    assert result.not_evaluated == ("b002",)
    assert result.incomplete
    assert sleeps.delays == list(RETRY_DELAYS)
    assert len(llm.requests) == 1 + MAX_ATTEMPTS + 1
    assert any(
        f"unavailable through {MAX_ATTEMPTS} attempts" in note and "b002" in note
        for note in result.notes
    )
    refused = record(result, "b001")
    assert (refused["status"], refused["error"], refused["verdict"]) == (
        "refused",
        "refusal",
        None,
    )
    assert refused["attempts"] == 1
    recall = metric(result, "alert_recall")
    assert (recall.all_items.numerator, recall.all_items.denominator) == (1, 2)
    assert any("b001 (refused:refusal)" in note for note in result.notes)


async def test_an_outage_is_retried_and_a_burst_that_gets_through_is_evaluated(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A 503 and then a 429 on one burst: it is run again from a fresh account
    after each bounded delay and scored on the attempt that got through, so one
    transient provider error cannot void a full (paid) run."""
    bursts = small_set()
    flaky = {"b002": (LLMErrorKind.UNAVAILABLE, LLMErrorKind.RATE_LIMITED)}
    llm = TriageOracle(bursts, PERFECT, flaky=flaky)
    sleeps = Sleeps()
    before = await leftovers(factory)
    result = await TriageTarget(bursts, session_factory=factory, sleep=sleeps).run(context(llm))

    assert result.not_evaluated == ()
    assert not result.incomplete
    assert sleeps.delays == list(RETRY_DELAYS[:2])
    retried = record(result, "b002")
    assert (retried["attempts"], retried["failed_attempts"]) == (
        3,
        ["llm_unavailable", "llm_rate_limited"],
    )
    assert (retried["status"], retried["alerts"]) == ("triaged", ["triage_decision"])
    assert record(result, "b001")["attempts"] == 1
    assert metric(result, "alert_recall").all_items.ratio == 1.0
    assert any(
        "run again after a provider outage" in note and "b002" in note for note in result.notes
    )
    assert await leftovers(factory) == before  # every attempt's account is gone


async def test_an_owner_only_burst_without_open_tasks_makes_no_call(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    bursts = [
        burst("b001", messages=[("owner", "SYNTH See you tomorrow")]),
        burst("b002", alert=True),
    ]
    target = TriageTarget(bursts, session_factory=factory)
    assert len(target.plan(RunContext(model=LUNA))) == 2  # b002's call and its retry
    llm = TriageOracle(bursts, {"b002": Answer(alert="decision")})
    result = await target.run(context(llm))

    assert len(llm.requests) == 1
    assert record(result, "b001")["status"] == "up_to_date"
    assert record(result, "b001")["triage_calls"] == 0
    assert metric(result, "needs_reply_agreement").all_items.ratio == 1.0


# ------------------------------------------------------------------ the CLI
def _databases(url: str) -> set[str]:
    with psycopg.connect(admin_url(url), autocommit=True) as conn:
        rows = conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'assistant_eval_%'")
        return {row[0] for row in rows.fetchall()}


def _no_settings() -> Settings:
    raise LiveSetupError("the settings are invalid (test)")


def test_run_triage_works_end_to_end_on_a_throwaway_database(
    tmp_path: Path,
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _databases(test_database_url)
    llm = TriageOracle(load_e3()[:2])
    monkeypatch.setenv("EVAL_DATABASE_URL", test_database_url)
    monkeypatch.setattr(live, "_settings", _no_settings)
    monkeypatch.setattr(
        cli,
        "load_live_config",
        lambda route, override: LiveConfig(ModelChoice(LUNA, "low"), SecretStr("sk-x")),
    )
    monkeypatch.setattr(cli, "build_client", lambda config: llm)
    code = cli.main(
        [
            "run", "triage", "--model", LUNA, "--live", "--limit", "2",
            "--out-dir", str(tmp_path), "--date", "2026-10-28",
        ]
    )  # fmt: skip

    assert code == 0
    printed = capsys.readouterr().out
    assert "triage (e3-bursts): 2 items (TRIAL" in printed
    assert "Urgent/decision recall" in printed
    (markdown,) = tmp_path.glob("*.md")
    assert markdown.name == f"2026-10-28-triage-{LUNA}-trial.md"
    text = markdown.read_text(encoding="utf-8")
    assert "TRIAL RUN" in text
    assert "NOT EVALUABLE" in text  # nothing is owner-confirmed
    (machine,) = tmp_path.glob("*.json")
    report = json.loads(machine.read_text(encoding="utf-8"))
    assert (report["set"], report["target"], report["dataset"]) == ("triage", "triage", "e3")
    assert {m["key"] for m in report["metrics"]} >= set(GATED)
    assert all(m["gate_status"] == "not evaluable" for m in report["metrics"] if m["gated"])
    assert report["cost"]["calls"] == len(llm.requests)
    assert _databases(test_database_url) == before  # the throwaway database is gone
