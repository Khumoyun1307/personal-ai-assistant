"""P2 E2 §B: the drafts target over the product's `Drafter`, on a real PostgreSQL.

Small synthetic situations built here (E2 itself is authored in E2D); the model
is `FakeLLM` only. Nothing here calls a real model or Telegram.
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from evals import cli, live
from evals.budget import BudgetedLLMClient, CallLedger
from evals.drafts import DRAFT_PURPOSE, JUDGE_PURPOSE, DraftsTarget
from evals.live import LiveConfig, LiveSetupError, ModelChoice
from evals.schema import E2Situation, load_e1
from evals.targets import RunContext, Status
from pydantic import BaseModel, SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account
from assistant.agent import prompt_registry
from assistant.config import Settings
from assistant.db.engine import build_engine, build_session_factory
from assistant.llm.port import LLMRequest, ParsedResponse, StopReason, TextItem, Usage
from tests.fakes.llm import FakeLLM
from tests.unit.test_draft_units import output
from tests.unit.test_e2_dataset import _cut, _new

pytestmark = pytest.mark.integration

LUNA = "gpt-6-luna"
E1 = load_e1()
TONES = {"s001": "formal", "s002": "friendly", "s003": "neutral"}
NAMES = {"s001": "ONE", "s002": "TWO", "s003": "THREE"}


def situations() -> list[E2Situation]:
    """An E1 cut and two new chats, each with its own preferences and notes."""
    records = [_cut("s001"), _new("s002", canary="QZ-48213"), _new("s003", canary="ZX-77341")]
    for record in records:
        name = NAMES[record["id"]]
        record["owner_prefs"]["reply_tone"] = TONES[record["id"]]
        record["owner_prefs"]["style_notes"] = [f"STYLE-NOTE-{name}"]
        record["contact_notes"] = [f"CONTACT-NOTE-{name}"]
    return [E2Situation.model_validate_json(json.dumps(record)) for record in records]


class DraftOracle(FakeLLM):
    """Plays the drafter and the intent judge. Situations are drafted in order;
    `texts` scripts each draft's text (by situation id)."""

    def __init__(self, texts: dict[str, str] | None = None) -> None:
        super().__init__()
        self._texts = texts or {}
        self._position = 0

    async def parse[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> ParsedResponse[T]:
        self.requests.append(request)
        if request.purpose == DRAFT_PURPOSE:
            self._position += 1
            text = self._texts.get(f"s{self._position:03d}", "Yes, it came at 18:00.")
            value: dict[str, Any] = output(text=text)
        else:
            assert request.purpose == JUDGE_PURPOSE
            value = {"intent": "answer_question", "facts": [], "violations": []}
        return ParsedResponse(
            StopReason.COMPLETED, LUNA, Usage(400, 100), schema.model_validate(value)
        )


@pytest.fixture
async def factory(migrated_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_url)
    try:
        yield build_session_factory(engine)
    finally:
        await engine.dispose()


def target(factory: async_sessionmaker[AsyncSession]) -> DraftsTarget:
    return DraftsTarget(situations(), E1, owner_language="uz-Latn", session_factory=factory)


def context(llm: FakeLLM) -> RunContext:
    budgeted = BudgetedLLMClient(llm, cap_usd=Decimal("1000"), ledger=CallLedger())
    return RunContext(llm=budgeted, model=LUNA, effort="low")


def sent(request: LLMRequest) -> str:
    """What the model was sent, as the untrusted wrapper leaves text (NFKC, entities)."""
    body = " ".join(item.text for item in request.items if isinstance(item, TextItem))
    return unicodedata.normalize("NFKC", html.unescape(request.instructions + "\n" + body))


async def accounts(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        return (await session.execute(select(func.count()).select_from(Account))).scalar_one()


# ------------------------------------------------------------------- end to end
async def test_drafts_target_runs_the_product_drafter_end_to_end_with_fake_llm(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    before = await accounts(factory)
    # s003's draft carries s002's canary: a cross-conversation leak.
    llm = DraftOracle({"s003": "Your order QZ-48213 is ready."})

    result = await target(factory).run(context(llm))

    assert (result.items, result.items_planned, result.items_total) == (3, 3, 3)
    assert [d["status"] for d in result.details] == ["drafted"] * 3
    assert result.prompts == (prompt_registry.active("draft_reply").label, "draft_intent.v1")
    for metric in result.metrics:
        assert metric.prescreen_status() is Status.INFO, metric.key
        assert metric.gate_status() is Status.INFO, metric.key
    leaks = next(m for m in result.metrics if m.key == "canary_leaks")
    assert (leaks.all_items.numerator, leaks.failed_items) == (1, ("s003",))
    assert any("canary leak" in line and "s003" in line for line in result.headline)
    s003 = next(d for d in result.details if d["id"] == "s003")
    assert s003["checks"]["leaks"] == [{"source": "s002", "text": "QZ-48213"}]
    assert s003["draft"]["text"] == "Your order QZ-48213 is ready."
    assert s003["rating_key"].startswith("s003#")
    # The product's prompt, schema and no-tools rule; one draft call per situation.
    drafts = [r for r in llm.requests if r.purpose == DRAFT_PURPOSE]
    assert len(drafts) == 3
    label = prompt_registry.active("draft_reply").label
    for request in drafts:
        assert request.tools == ()
        assert prompt_registry.active("draft_reply").text in request.instructions
        assert "preferred language: uz-Latn" in request.instructions
        assert request.prompt_cache_key == f"draft_reply:{label}"
    # The E1 cut: the chat up to and including `cut_after`, nothing after it.
    cut = situations()[0]
    assert cut.source is not None
    chat = next(c for c in E1 if c.id == cut.source.e1_conversation)
    shown = {m.id for m in cut.shown_messages({chat.id: chat})}
    first = sent(drafts[0])
    for message in chat.messages:
        if not message.text or message.deleted or len(message.text) < 25:
            continue
        text = unicodedata.normalize("NFKC", message.text)
        assert (text in first) == (message.id in shown), message.id
    assert await accounts(factory) == before  # the synthetic account is gone


async def test_each_situation_sees_only_its_own_preferences_and_notes(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    llm = DraftOracle()
    await target(factory).run(context(llm))

    drafts = [r for r in llm.requests if r.purpose == DRAFT_PURPOSE]
    assert len(drafts) == 3
    for situation, request in zip(situations(), drafts, strict=True):
        text = sent(request)
        own = NAMES[situation.id]
        assert f"STYLE-NOTE-{own}" in text
        assert f"CONTACT-NOTE-{own}" in text
        assert f"reply tone: {TONES[situation.id]}" in text
        for other, name in NAMES.items():
            if other == situation.id:
                continue
            assert f"STYLE-NOTE-{name}" not in text
            assert f"CONTACT-NOTE-{name}" not in text
        # Another situation's chat (its canary) never reaches this draft.
        for canary in ("QZ-48213", "ZX-77341"):
            assert (canary in text) == (canary == situation.canary)


# ------------------------------------------------------------------ the CLI
def _no_settings() -> Settings:
    raise LiveSetupError("the settings are invalid (test)")


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Synthetic E2, no settings, and any database or model client fails the test."""

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a database or a model client was used")

    monkeypatch.setattr(cli, "load_e2", lambda *args, **kwargs: situations())
    monkeypatch.setattr(live, "_settings", _no_settings)
    monkeypatch.setattr(cli, "isolated_drafts", boom)
    monkeypatch.setattr(cli, "build_client", boom)
    monkeypatch.setattr(cli, "load_live_config", boom)


def test_the_drafts_target_refuses_without_live(
    offline: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["run", "drafts", "--model", LUNA]) == cli.EXIT_REFUSED
    assert "--live" in capsys.readouterr().err
    assert cli.main(["run", "drafts", "--model", LUNA, "--limit", "1"]) == cli.EXIT_REFUSED
    assert cli.main(["run", "drafts", "--model", LUNA, "--only", "s002"]) == cli.EXIT_REFUSED


def test_estimate_only_calls_no_model(offline: None, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["run", "drafts", "--model", LUNA, "--estimate-only"]) == 0
    assert cli.main(["run", "drafts", "--model", LUNA, "--live", "--estimate-only"]) == 0
    assert cli.main(["run", "drafts", "--model", LUNA, "--estimate-only", "--only", "s002"]) == 0
    full, live_flag, only = capsys.readouterr().out.splitlines()
    pattern = r"drafts \(drafts-uz-Latn\): model gpt-6-luna, (\d+) calls, worst case \$(\d\.\d{4})"
    match = re.search(pattern, full)
    assert match
    # Per situation: the draft, its one truncation retry at the larger budget, the judge.
    assert int(match.group(1)) == 3 * 3
    assert 0 < Decimal(match.group(2)) < Decimal("0.1")
    assert full == live_flag
    only_match = re.search(pattern, only)
    assert only_match
    assert int(only_match.group(1)) == 3


def test_a_live_run_writes_its_report_and_the_rating_sheet(
    tmp_path: Path, test_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = DraftOracle()
    monkeypatch.setenv("EVAL_DATABASE_URL", test_database_url)
    monkeypatch.setattr(cli, "load_e2", lambda *args, **kwargs: situations())
    monkeypatch.setattr(live, "_settings", _no_settings)
    monkeypatch.setattr(
        cli,
        "load_live_config",
        lambda route, override: LiveConfig(ModelChoice(LUNA, "low"), SecretStr("sk-x")),
    )
    monkeypatch.setattr(cli, "build_client", lambda config: llm)
    out = tmp_path / "reports"
    code = cli.main(
        ["run", "drafts", "--model", LUNA, "--live", "--out-dir", str(out), "--date", "2026-10-27"]
    )
    assert code == 0
    (machine,) = out.glob("*.json")
    assert machine.name == f"2026-10-27-e2-drafts-{LUNA}.json"
    report = json.loads(machine.read_text(encoding="utf-8"))
    assert report["set"] == "e2-drafts"
    assert report["dataset"] == "e2"
    assert {d["id"] for d in report["details"]} == {"s001", "s002", "s003"}
    markdown = machine.with_suffix(".md").read_text(encoding="utf-8")
    assert "Canary leaks: 0" in markdown

    first = report["details"][0]["rating_key"]
    ledger = tmp_path / "e2.json"
    ledger.write_text(json.dumps({"ratings": {first: "as_is"}}), encoding="utf-8")
    sheet = tmp_path / "sheet.md"
    code = cli.main(
        [
            "review", "drafts", "--from", str(machine), "--export", str(sheet),
            "--reviews", str(ledger),
        ]
    )  # fmt: skip
    assert code == 0
    text = sheet.read_text(encoding="utf-8")
    for detail in report["details"]:
        assert detail["rating_key"] in text
        assert detail["draft"]["text"] in text
    # The owner rates against the situation's own preferences and notes.
    assert "STYLE-NOTE-ONE" in text
    assert "tone formal" in text
    assert "1 of 3 drafts rated" in text
