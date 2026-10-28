"""P2 E3 §B part 2 (P2-T43): the `evaluate_monitor` target over the product's
`MonitorEvaluator` and its effects, on a real PostgreSQL.

Small synthetic cases are built here (E3 itself is E3D's); two tests use the
real E3 monitor cases. The model is `FakeLLM` only: nothing here calls a real
model or Telegram.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections.abc import AsyncIterator, Mapping, Sequence
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
from evals.monitors import MONITOR_PURPOSE, MonitorTarget, makes_a_call
from evals.report import RunMeta
from evals.review import ReviewLedger
from evals.runner import run_target
from evals.schema import MonitorCase, load_monitor_cases
from evals.targets import RunContext, Status
from pydantic import BaseModel, SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.agent import prompt_registry
from assistant.agent.models import AgentRun
from assistant.automation.models import Automation
from assistant.config import Settings
from assistant.db.engine import build_engine, build_session_factory
from assistant.inbox.models import Notification
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
from tests.unit.test_eval_monitors import case

pytestmark = pytest.mark.integration

LUNA = "gpt-6-luna"
BAR = ("monitor_accuracy", "false_match_rate")


# ------------------------------------------------------------------ the model
def fold(text: str) -> str:
    """Text as the untrusted wrapper leaves it (NFKC, entities)."""
    return unicodedata.normalize("NFKC", html.unescape(text))


def sent(request: LLMRequest) -> str:
    body = "\n".join(item.text for item in request.items if isinstance(item, TextItem))
    return fold(request.instructions + "\n" + body)


def shown_new(request: LLMRequest) -> list[int]:
    """The `[msg:<id>]` ids of the burst the product asks about."""
    burst = next(
        item.text
        for item in request.items
        if isinstance(item, TextItem) and item.text.startswith("New messages to check:")
    )
    return [int(i) for i in re.findall(r"^\[msg:(\d+)\]", burst, flags=re.MULTILINE)]


def listed_monitors(request: LLMRequest) -> list[int]:
    return [int(i) for i in re.findall(r"\[monitor:(\d+)\]", sent(request))]


class MonitorOracle(FakeLLM):
    """Plays the `evaluate_monitor` model, case by case in run order (cases the
    product makes no call for are skipped). It finds each monitor by its
    description and cites the newest new message, as a model reading the
    context would. `matches`: case id -> the dataset monitor ids to match.

    `down` cases fail on the provider at every attempt the harness makes;
    `flaky` cases fail with the given errors first, then answer."""

    def __init__(
        self,
        cases: Sequence[MonitorCase],
        matches: Mapping[str, Sequence[int]] | None = None,
        *,
        refuse: Sequence[str] = (),
        down: Sequence[str] = (),
        flaky: Mapping[str, Sequence[LLMErrorKind]] | None = None,
        usage: Usage = Usage(300, 60),  # noqa: B008  (immutable)
    ) -> None:
        super().__init__()
        self._queue = [c for c in cases if makes_a_call(c)]
        self._matches = matches or {}
        self._refuse = set(refuse)
        self._outages = {case_id: [LLMErrorKind.UNAVAILABLE] * MAX_ATTEMPTS for case_id in down}
        self._outages |= {case_id: list(kinds) for case_id, kinds in (flaky or {}).items()}
        self._usage = usage
        self._position = 0
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
            if self._failed == MAX_ATTEMPTS:  # the harness gives the case up
                self._next()
            raise LLMError(kind)
        self._next()
        if current.id in self._refuse:
            return ParsedResponse(StopReason.REFUSAL, LUNA, self._usage)
        text = sent(request)
        listed = {
            monitor.id: int(found.group(1))
            for monitor in current.monitors
            if (
                found := re.search(
                    rf"\[monitor:(\d+)\][^\n]*{re.escape(fold(monitor.description))}", text
                )
            )
        }
        newest = max(shown_new(request))
        wanted = set(self._matches.get(current.id, ()))
        value = {
            "language": "en",
            "verdicts": [
                {
                    "monitor_id": automation_id,
                    "matched": monitor_id in wanted,
                    "message_ids": [newest] if monitor_id in wanted else [],
                }
                for monitor_id, automation_id in listed.items()
            ],
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


def measured(result: Any, key: str) -> tuple[int, int]:
    found = metric(result, key).all_items
    return found.numerator, found.denominator


def record(result: Any, case_id: str) -> dict[str, Any]:
    return next(d for d in result.details if d["id"] == case_id)


async def leftovers(factory: async_sessionmaker[AsyncSession]) -> tuple[int, ...]:
    async with factory() as session:
        return tuple(
            [
                int((await session.execute(select(func.count()).select_from(m))).scalar_one())
                for m in (Account, Automation, Notification, AgentRun)
            ]
        )


def confirming(cases: Sequence[MonitorCase]) -> ReviewLedger:
    return ReviewLedger(frozenset(c.id for c in cases), {}, {})


# ------------------------------------------------------------------ the spec's test
async def test_one_call_per_case_and_accuracy_per_monitor(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    cases = [
        case(
            "m001",
            monitors=[
                ("SYNTH-MON-A a refund is requested", True),
                ("SYNTH-MON-B an outage", False),
            ],
            messages=[("contact", "SYNTH-1 Please refund my order"), ("contact", "SYNTH-1 Today")],
        ),
        case("m002", monitors=[("SYNTH-MON-C a delivery is late", True)]),
        case("m003", monitors=[("SYNTH-MON-D a contract is cancelled", False)]),
        case("m004", monitors=[("SYNTH-MON-E a legal threat", False)]),
    ]
    # m001: both right; m002: a missed match; m003: a false match; m004: right.
    llm = MonitorOracle(cases, {"m001": [1], "m003": [1]})
    result = await MonitorTarget(cases, session_factory=factory).run(context(llm))

    label = prompt_registry.active("evaluate_monitor").label
    assert result.prompts == (label,)
    assert (result.items, result.items_planned, result.items_total) == (4, 4, 4)
    assert result.complete
    # One call per case, with every monitor of the case in it: the product's
    # contact-triggered run, no tools, the shipped prompt.
    assert len(llm.requests) == 4
    for request, item in zip(llm.requests, cases, strict=True):
        assert request.purpose == MONITOR_PURPOSE
        assert request.tools == ()
        assert (request.model, request.effort) == (LUNA, "low")
        assert request.instructions == prompt_registry.active("evaluate_monitor").text
        assert request.prompt_cache_key == f"evaluate_monitor:{label}"
        assert len(listed_monitors(request)) == len(item.monitors)
        assert len(shown_new(request)) == len(item.burst)
    assert [d["status"] for d in result.details] == ["evaluated"] * 4

    # Accuracy per monitor (`<case>.<monitor id>`): 3 of 5 right.
    accuracy = metric(result, "monitor_accuracy")
    assert measured(result, "monitor_accuracy") == (3, 5)
    assert accuracy.failed_items == ("m002.1", "m003.1")
    assert (accuracy.goal, accuracy.threshold) == ("at_least", 0.90)
    assert accuracy.prescreen_status() is Status.FAIL
    # False matches: of the monitors that fired, those labelled not matching.
    false_matches = metric(result, "false_match_rate")
    assert measured(result, "false_match_rate") == (1, 2)
    assert false_matches.failed_items == ("m003.1",)
    assert (false_matches.goal, false_matches.threshold) == ("at_most", 0.10)
    assert false_matches.prescreen_status() is Status.FAIL
    assert measured(result, "match_recall") == (1, 2)
    assert measured(result, "negatives_matched") == (1, 3)
    # The bar for the model choice (spec: informational), never an owner gate.
    for m in result.metrics:
        assert m.gate_status() is Status.INFO
    # What fired is what the product's effects enqueued, on the cited message.
    assert record(result, "m001")["monitors"] == [
        {"id": 1, "label_matched": True, "fired": True, "cited": ["m001.m02"]},
        {"id": 2, "label_matched": False, "fired": False, "cited": []},
    ]
    assert record(result, "m003")["monitors"][0]["fired"]
    assert result.breakdowns["monitor accuracy by variant"]["en"].numerator == 3


# ------------------------------------------------------------------ more
async def test_a_model_answering_the_labels_meets_the_bar_on_every_e3_case(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every real E3 case loads into the product's context (each monitor is
    listed with its description, each burst message is new), and the labels
    played back as answers meet both thresholds."""
    cases = load_monitor_cases()
    answers = {c.id: c.labels.matched_monitor_ids for c in cases}
    llm = MonitorOracle(cases, answers)
    result = await MonitorTarget(cases, session_factory=factory).run(
        context(llm, reviews=confirming(cases))
    )

    assert result.complete
    assert len(llm.requests) == len(cases)
    assert measured(result, "monitor_accuracy") == (len(cases), len(cases))
    for key in BAR:
        assert metric(result, key).prescreen_status() is Status.PASS
    positives = sum(len(c.labels.matched_monitor_ids) for c in cases)
    assert measured(result, "false_match_rate") == (0, positives)
    assert set(result.breakdowns["monitor accuracy by variant"]) == {
        "en",
        "ru",
        "uz-Cyrl",
        "uz-Latn",
        "uz-ru-mix",
    }


async def test_a_refusal_is_a_miss_and_an_outage_is_not_evaluated(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    cases = [
        case("m001", monitors=[("SYNTH-MON-A a refund", True)]),
        case("m002", monitors=[("SYNTH-MON-B a refund", True)]),
        case("m003", monitors=[("SYNTH-MON-C a refund", True)]),
    ]
    llm = MonitorOracle(cases, {"m003": [1]}, refuse=["m001"], down=["m002"])
    sleeps = Sleeps()
    before = await leftovers(factory)
    result = await MonitorTarget(cases, session_factory=factory, sleep=sleeps).run(context(llm))

    # The outage is not evaluated only after the bounded retries (the agent
    # mode's attempts and delays); the refusal is the model's answer, not retried.
    assert result.not_evaluated == ("m002",)
    assert result.incomplete
    assert sleeps.delays == list(RETRY_DELAYS)
    assert len(llm.requests) == 1 + MAX_ATTEMPTS + 1
    assert any(
        f"unavailable through {MAX_ATTEMPTS} attempts" in note and "m002" in note
        for note in result.notes
    )
    refused = record(result, "m001")
    assert (refused["status"], refused["error"], refused["attempts"]) == ("refused", "refusal", 1)
    assert not refused["monitors"][0]["fired"]
    assert measured(result, "monitor_accuracy") == (1, 2)
    assert metric(result, "monitor_accuracy").failed_items == ("m001.1",)
    assert any("m001 (refused:refusal)" in note for note in result.notes)
    assert await leftovers(factory) == before


async def test_an_outage_is_retried_and_a_case_that_gets_through_is_evaluated(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    cases = [case("m001", monitors=[("SYNTH-MON-A a refund", True)]), case("m002")]
    flaky = {"m001": (LLMErrorKind.UNAVAILABLE, LLMErrorKind.RATE_LIMITED)}
    llm = MonitorOracle(cases, {"m001": [1]}, flaky=flaky)
    sleeps = Sleeps()
    before = await leftovers(factory)
    result = await MonitorTarget(cases, session_factory=factory, sleep=sleeps).run(context(llm))

    assert result.not_evaluated == ()
    assert result.complete
    assert sleeps.delays == list(RETRY_DELAYS[:2])
    retried = record(result, "m001")
    assert (retried["attempts"], retried["failed_attempts"]) == (
        3,
        ["llm_unavailable", "llm_rate_limited"],
    )
    assert retried["monitors"][0]["fired"]
    assert measured(result, "monitor_accuracy") == (2, 2)
    assert any("run again after a provider outage" in n and "m001" in n for n in result.notes)
    assert await leftovers(factory) == before  # every attempt's account is gone


async def test_each_case_starts_from_a_clean_account(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    before = await leftovers(factory)
    cases = [
        case(
            "m001",
            monitors=[("SYNTH-MON-FIRST a refund", True)],
            messages=[("contact", "SYNTH-FIRST refund please")],
        ),
        case(
            "m002",
            monitors=[("SYNTH-MON-SECOND an outage", False)],
            messages=[("contact", "SYNTH-SECOND thanks")],
        ),
    ]
    llm = MonitorOracle(cases, {"m001": [1]})
    result = await MonitorTarget(cases, session_factory=factory).run(context(llm))

    assert result.items == 2
    first, second = (sent(r) for r in llm.requests)
    assert "SYNTH-FIRST" in first
    assert "SYNTH-SECOND" not in first
    # Nothing of the first case (its message, its monitor, its alert) reaches the second.
    assert "SYNTH-FIRST" not in second
    assert "SYNTH-MON-FIRST" not in second
    assert listed_monitors(llm.requests[1]) != listed_monitors(llm.requests[0])
    assert llm.requests[0].account_id != llm.requests[1].account_id
    assert not record(result, "m002")["monitors"][0]["fired"]
    # Every account, monitor, alert and run the harness made is gone.
    assert await leftovers(factory) == before


async def test_a_case_without_a_contact_message_makes_no_call(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    cases = [
        case("m001", messages=[("owner", "SYNTH see you tomorrow")]),
        case("m002", monitors=[("SYNTH-MON-A a refund", True)]),
    ]
    target = MonitorTarget(cases, session_factory=factory)
    assert len(target.plan(RunContext(model=LUNA))) == 2  # m002's call and its retry
    llm = MonitorOracle(cases, {"m002": [1]})
    result = await target.run(context(llm))

    assert len(llm.requests) == 1
    assert (record(result, "m001")["status"], record(result, "m001")["calls"]) == ("up_to_date", 0)
    assert measured(result, "monitor_accuracy") == (2, 2)
    assert any("no model call" in note and "m001" in note for note in result.notes)


async def test_the_run_records_its_spend_and_respects_the_cap(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    cases = [case("m001"), case("m002"), case("m003"), case("m004")]
    target = MonitorTarget(cases, session_factory=factory)
    meta = RunMeta(
        run_date=date(2026, 10, 28),
        set_name="monitors",
        mode=target.label,
        model=LUNA,
        live=True,
        effort="low",
        dataset_files={},
        cap_usd=Decimal("1000"),
    )

    # Recorded: every call is in the ledger and in the report's cost.
    llm, ledger = MonitorOracle(cases), CallLedger()
    outcome = await run_target(
        target, context(llm, ledger=ledger), meta=meta, ledger=ledger, out_dir=tmp_path
    )
    assert outcome.machine is not None
    report = json.loads(outcome.machine.read_text(encoding="utf-8"))
    assert report["cost"]["calls"] == len(llm.requests) == 4
    assert Decimal(report["cost"]["spent_usd"]) == ledger.spent > 0
    assert report["cost"]["per_model"][LUNA]["cost_per_100_items_usd"] is not None

    # Capped: the call that would cross the cap is never made, the run stops
    # early (incomplete) and nothing is left behind.
    before = await leftovers(factory)
    expensive = MonitorOracle(cases, usage=Usage(200, 1000))
    capped = CallLedger()
    stopped = await target.run(context(expensive, ledger=capped, cap="0.0015"))
    assert stopped.stopped
    assert stopped.incomplete
    assert stopped.items < stopped.items_planned == 4
    assert any("STOPPED EARLY" in n for n in stopped.notes)
    assert 0 < capped.spent <= Decimal("0.0015")
    assert len(expensive.requests) == len(capped.entries)
    assert await leftovers(factory) == before


# ------------------------------------------------------------------ the CLI
def _databases(url: str) -> set[str]:
    with psycopg.connect(admin_url(url), autocommit=True) as conn:
        rows = conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'assistant_eval_%'")
        return {row[0] for row in rows.fetchall()}


def _no_settings() -> Settings:
    raise LiveSetupError("the settings are invalid (test)")


def test_run_monitors_works_end_to_end_on_a_throwaway_database(
    tmp_path: Path,
    test_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _databases(test_database_url)
    llm = MonitorOracle(load_monitor_cases()[:2])
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
            "run", "monitors", "--model", LUNA, "--live", "--limit", "2",
            "--out-dir", str(tmp_path), "--date", "2026-10-28",
        ]
    )  # fmt: skip

    assert code == 0
    printed = capsys.readouterr().out
    assert "monitors (e3-monitor-cases): 2 items (TRIAL" in printed
    assert "Monitor accuracy" in printed
    (markdown,) = tmp_path.glob("*.md")
    assert markdown.name == f"2026-10-28-monitors-{LUNA}-trial.md"
    assert "TRIAL RUN" in markdown.read_text(encoding="utf-8")
    (machine,) = tmp_path.glob("*.json")
    report = json.loads(machine.read_text(encoding="utf-8"))
    assert (report["set"], report["target"], report["dataset"]) == ("monitors", "monitors", "e3")
    assert {m["key"] for m in report["metrics"]} >= set(BAR)
    assert report["prompts"] == [prompt_registry.active("evaluate_monitor").label]
    assert report["cost"]["calls"] == len(llm.requests) == 2
    assert _databases(test_database_url) == before  # the throwaway database is gone
