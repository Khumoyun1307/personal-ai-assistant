"""S03.4: the runner's refusals, report recording and CLI, without any model or database.

Real-model runs are manual (AGENTS §8, §9). These tests prove that nothing
calls a model, or even opens the evaluation database, unless every guard has
passed.
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from evals import cli, live, manifest
from evals.budget import CallEntry, CallLedger
from evals.errors import LedgerError
from evals.live import LiveSetupError
from evals.report import RunMeta, render_comparison, render_markdown, to_json, write_report
from evals.review import ReviewLedger
from evals.runner import RefusedError, ensure_frozen, run_target
from evals.targets import Measure, MetricResult, RunContext, SummaryTarget, TargetResult
from pydantic import SecretStr

from assistant.config import Settings
from assistant.llm.port import Usage

ROOT = Path(__file__).resolve().parents[2]
LUNA = "gpt-6-luna"


class DatabaseTouchedError(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any path that reaches the evaluation database fails the test."""

    def boom(*args: Any, **kwargs: Any) -> None:
        raise DatabaseTouchedError("the evaluation database was opened")

    monkeypatch.setattr(cli, "isolated_corpus", boom)


@pytest.fixture
def no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings that cannot load, so `--model` is the only source of a model."""

    def fail() -> Settings:
        raise LiveSetupError("the settings are invalid (test)")

    monkeypatch.setattr(live, "_settings", fail)


# ------------------------------------------------------------ frozen datasets
def test_the_real_datasets_match_the_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["verify"]) == 0
    assert "retrieval/queries.jsonl" in capsys.readouterr().out


def test_a_modified_dataset_refuses_every_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(manifest, "problems", lambda: ["changed: retrieval/queries.jsonl"])
    with pytest.raises(RefusedError, match="frozen"):
        ensure_frozen()
    assert cli.main(["run", "retrieval"]) == cli.EXIT_REFUSED
    assert "never edit a dataset" in capsys.readouterr().err


# ------------------------------------------------------------------- --live
def test_a_model_run_refuses_without_the_live_flag(
    no_settings: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["run", "retrieval", "--mode", "variants", "--model", LUNA])
    assert code == cli.EXIT_REFUSED
    assert "--live" in capsys.readouterr().err  # and the autouse fixture proves no database


def test_both_mode_refuses_without_live_before_running_the_deterministic_half(
    no_settings: None,
) -> None:
    assert cli.main(["run", "retrieval", "--mode", "both", "--model", LUNA]) == cli.EXIT_REFUSED


def test_live_without_an_api_key_refuses_before_any_database_or_call(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    keyless = Settings(  # type: ignore[call-arg]
        _env_file=None, database_url="postgresql+psycopg://u:p@db:5432/a"
    )
    monkeypatch.setattr(live, "_settings", lambda: keyless)
    code = cli.main(["run", "retrieval", "--mode", "variants", "--live"])
    assert code == cli.EXIT_REFUSED
    assert "OPENAI_API_KEY is not set" in capsys.readouterr().err


def test_a_model_of_another_processor_is_refused(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+psycopg://u:p@db:5432/a",
        openai_api_key="sk-synthetic",
    )
    monkeypatch.setattr(live, "_settings", lambda: settings)
    code = cli.main(
        [
            "run",
            "retrieval",
            "--mode",
            "variants",
            "--live",
            "--model",
            "claude-synthetic",
            "--price",
            "claude-synthetic=1:1",
        ]
    )
    assert code == cli.EXIT_REFUSED
    assert "not served by the OpenAI adapter" in capsys.readouterr().err


# --------------------------------------------------------------- the estimate
def test_estimate_only_prints_the_worst_case_and_calls_nothing(
    no_settings: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["run", "retrieval", "--mode", "variants", "--model", LUNA, "--estimate-only"])
    out = capsys.readouterr().out
    assert code == 0
    assert re.search(r"76 calls, worst case \$0\.\d{4}, cap \$1", out)


def test_a_run_whose_worst_case_exceeds_the_cap_does_not_start(
    no_settings: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        [
            "run",
            "retrieval",
            "--mode",
            "variants",
            "--model",
            LUNA,
            "--live",
            "--budget-usd",
            "0.01",
        ]
    )
    assert code == cli.EXIT_REFUSED
    assert "over the $0.01 cap" in capsys.readouterr().err


def test_limit_shrinks_the_estimate(no_settings: None, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(
        [
            "run",
            "retrieval",
            "--mode",
            "variants",
            "--model",
            LUNA,
            "--estimate-only",
            "--limit",
            "5",
        ]
    )
    assert "10 calls" in capsys.readouterr().out


def test_a_model_without_a_price_needs_an_explicit_price(
    no_settings: None, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["run", "retrieval", "--mode", "variants", "--model", "gpt-6-sol", "--estimate-only"]
    assert cli.main(args) == cli.EXIT_REFUSED
    assert "--price gpt-6-sol=" in capsys.readouterr().err
    assert cli.main([*args, "--price", "gpt-6-sol=0.30:1.50"]) == 0


@pytest.mark.parametrize("cap", ["abc", "0", "-1"])
def test_a_bad_budget_is_refused(cap: str, no_settings: None) -> None:
    assert cli.main(["run", "retrieval", "--budget-usd", cap]) == cli.EXIT_REFUSED


@pytest.mark.parametrize("limit", ["0", "-3", "x"])
def test_a_limit_below_one_is_rejected(limit: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        cli.main(["run", "retrieval", "--limit", limit])
    assert stop.value.code == 2
    assert "--limit" in capsys.readouterr().err


def test_a_price_override_for_a_model_the_table_prices_is_refused(
    no_settings: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["run", "retrieval", "--price", f"{LUNA}=0.01:0.01"])
    assert code == cli.EXIT_REFUSED
    assert "already priced" in capsys.readouterr().err


def test_a_free_price_is_refused(no_settings: None, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["run", "retrieval", "--price", "gpt-6-sol=0:0"])
    assert code == cli.EXIT_REFUSED
    assert "--price expects" in capsys.readouterr().err


def test_live_with_no_write_is_refused_so_spend_cannot_go_unrecorded(
    no_settings: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        ["run", "retrieval", "--mode", "variants", "--model", LUNA, "--live", "--no-write"]
    )
    assert code == cli.EXIT_REFUSED
    assert "--no-write" in capsys.readouterr().err


@pytest.fixture
def forbid_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Building a model client (or reading the API key) fails the test."""

    def boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a model client was configured or built")

    monkeypatch.setattr(cli, "build_client", boom)
    monkeypatch.setattr(cli, "load_live_config", boom)


def test_no_client_is_built_without_live(
    forbid_client: None, no_settings: None, capsys: pytest.CaptureFixture[str]
) -> None:
    variants = ["run", "retrieval", "--mode", "variants", "--model", LUNA]
    assert cli.main(variants) == cli.EXIT_REFUSED
    assert cli.main(["run", "retrieval", "--mode", "both", "--model", LUNA]) == cli.EXIT_REFUSED
    assert cli.main([*variants, "--live", "--estimate-only"]) == 0  # estimating builds nothing
    assert cli.main([*variants, "--live", "--no-write"]) == cli.EXIT_REFUSED


def test_a_deterministic_run_builds_no_client(forbid_client: None) -> None:
    with pytest.raises(DatabaseTouchedError):  # it got as far as the database, and no further
        cli.main(["run", "retrieval"])


def test_a_bug_keeps_its_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the tool's own errors become a clean exit, not every RuntimeError."""

    def broken() -> None:
        raise RuntimeError("a bug, not a refusal")

    monkeypatch.setattr(cli, "ensure_frozen", broken)
    with pytest.raises(RuntimeError, match="a bug"):
        cli.main(["run", "retrieval"])


def test_the_tools_own_errors_become_a_clean_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def ledger_problem(*args: Any) -> None:
        raise LedgerError("the review ledger bad.json cannot be read")

    monkeypatch.setattr(cli, "_reviews", ledger_problem)
    assert cli.main(["run", "retrieval"]) == cli.EXIT_REFUSED
    assert "bad.json" in capsys.readouterr().err


def test_a_malformed_ledger_file_is_a_clean_exit_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"confirmed": []}', encoding="utf-8")
    assert cli.main(["run", "retrieval", "--reviews", str(bad)]) == cli.EXIT_REFUSED
    assert "bad.json" in capsys.readouterr().err


def test_the_eval_client_never_retries_invisibly(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK retries would bill attempts the ledger never sees."""
    captured: dict[str, Any] = {}

    class FakeSDK:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(live, "AsyncOpenAI", FakeSDK)
    live.build_client(live.LiveConfig(live.ModelChoice(LUNA, None), SecretStr("sk-synthetic")))
    assert captured["max_retries"] == 0


# ------------------------------------------------------------------- summary
def test_the_summary_target_is_a_placeholder_until_s03_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["run", "summary"]) == cli.EXIT_NOT_AVAILABLE
    assert "S03.2" in capsys.readouterr().err


def test_the_placeholder_only_declares_the_interface() -> None:
    target = SummaryTarget()
    assert (target.name, target.dataset, target.route, target.uses_model) == (
        "summary",
        "e1",
        "summarize",
        True,
    )


# -------------------------------------------------------------------- reports
def result(
    *,
    items: int = 38,
    planned: int = 38,
    total: int = 38,
    confirmed: Measure | None = None,
    gated: bool = True,
) -> TargetResult:
    """`items` ran of `planned`; the set has `total` (planned < total: a trial)."""
    metric = MetricResult(
        key="top5_hit",
        label="Target message in the top 5 results",
        unit="ratio",
        goal="at_least",
        threshold=0.85,
        all_items=Measure(33, items),
        confirmed=confirmed or Measure(0, 0),
        set_size=total,
        gated=gated,
        failed_items=("r07", "r08"),
        note="a note",
    )
    return TargetResult(
        target="retrieval",
        dataset="retrieval",
        items=items,
        items_planned=planned,
        items_total=total,
        metrics=(metric,),
        details=({"id": "r01", "hit_top5": True},),
        breakdowns={"top-5 hit by intent": {"exact": Measure(5, 5)}},
        notes=("n",),
        prompts=("retrieval_variants.v1",),
    )


def meta(model: str | None = None, **kwargs: Any) -> RunMeta:
    return RunMeta(
        run_date=date(2026, 10, 25),
        set_name="retrieval",
        mode="variants" if model else "deterministic",
        model=model,
        live=model is not None,
        effort=None,
        dataset_files={"retrieval/queries.jsonl": "ab" * 32},
        cap_usd=Decimal("1") if model else None,
        **kwargs,
    )


def spent() -> CallLedger:
    ledger = CallLedger()
    ledger.entries.append(
        CallEntry("eval_retrieval_answer", LUNA, Usage(1000, 500), Decimal("0.01"))
    )
    return ledger


def test_reports_are_named_by_date_set_and_model_with_a_suffix_for_reruns(tmp_path: Path) -> None:
    first, machine = write_report(tmp_path, meta(), result(), CallLedger())
    second, _ = write_report(tmp_path, meta(), result(), CallLedger())
    live_md, _ = write_report(tmp_path, meta(LUNA), result(), spent())
    assert first.name == "2026-10-25-retrieval-deterministic.md"
    assert machine.name == "2026-10-25-retrieval-deterministic.json"
    assert second.name == "2026-10-25-retrieval-deterministic-2.md"
    assert live_md.name == "2026-10-25-retrieval-gpt-6-luna.md"


def test_the_json_is_machine_readable_and_carries_gate_status_and_cost(tmp_path: Path) -> None:
    _, machine = write_report(tmp_path, meta(LUNA), result(), spent())
    data = json.loads(machine.read_text(encoding="utf-8"))
    assert data["model"] == LUNA
    assert data["metrics"][0]["prescreen_status"] == "pass"  # 33/38 = 86.8 % >= 85 %
    assert data["metrics"][0]["gate_status"] == "not evaluable"
    assert data["cost"]["spent_usd"] == "0.01"
    row = data["cost"]["per_model"][LUNA]
    assert row["cost_per_100_items_usd"] == str(Decimal("0.01") / 38 * 100)
    assert data["dataset_files"] == {"retrieval/queries.jsonl": "ab" * 32}


def test_the_markdown_states_pre_screen_gate_cost_and_no_decision(tmp_path: Path) -> None:
    text = render_markdown(meta(LUNA), result(), spent())
    assert "33/38 (86.8 %)" in text
    assert "NOT EVALUABLE" in text
    assert "Failed items, Target message in the top 5 results: r07, r08" in text
    assert "Cost per 100 items" in text
    assert "None recorded here" in text  # the model-choice decision is the owner's


def test_a_deterministic_report_says_no_model_and_no_cost() -> None:
    text = render_markdown(meta(), result(), CallLedger())
    assert "No model was called: cost USD 0." in text
    assert "Real model called | no" in text


def test_a_stopped_run_is_flagged_and_its_gate_cannot_pass() -> None:
    full = Measure(10, 10)
    stopped = result(items=10, planned=38, confirmed=full)
    assert stopped.stopped
    text = render_markdown(meta(LUNA), stopped, spent())
    assert "STOPPED RUN" in text
    assert "**NOT EVALUABLE** (10 of 38 confirmed)" in text


def test_a_limited_run_is_a_trial_never_a_gate_and_is_named_so(tmp_path: Path) -> None:
    trial = result(items=5, planned=5, total=38, confirmed=Measure(5, 5))
    assert trial.trial
    assert not trial.stopped
    assert not trial.complete
    text = render_markdown(meta(LUNA), trial, spent())
    assert "TRIAL RUN" in text
    assert "**NOT EVALUABLE** (5 of 38 confirmed)" in text
    md, machine = write_report(tmp_path, meta(LUNA), trial, spent())
    assert md.name == "2026-10-25-retrieval-gpt-6-luna-trial.md"
    assert json.loads(machine.read_text(encoding="utf-8"))["trial"] is True
    full, _ = write_report(tmp_path, meta(LUNA), result(), spent())
    assert full.name == "2026-10-25-retrieval-gpt-6-luna.md"  # the trial never took this name


def test_a_confirmed_pass_is_reported_as_pass_with_the_confirmation_count() -> None:
    text = render_markdown(meta(), result(confirmed=Measure(38, 38)), CallLedger())
    assert "**PASS** (38 of 38 confirmed)" in text


def test_a_deterministic_baseline_shows_the_threshold_as_pre_screen_only() -> None:
    text = render_markdown(meta(), result(confirmed=Measure(38, 38), gated=False), CallLedger())
    assert "(pass)" in text.split("## Results")[1]  # 33 of 38 vs 85 %, as a pre-screen
    assert "**INFO**" in text
    assert "**PASS**" not in text


def test_exit_code_is_non_zero_only_for_a_stopped_run() -> None:
    assert cli.exit_code(result()) == 0
    assert cli.exit_code(result(items=5, planned=5, total=38)) == 0  # a deliberate trial
    assert cli.exit_code(result(items=5, planned=38)) == cli.EXIT_INCOMPLETE != 0


def run_exploding_target(out_dir: Path | None, ledger: CallLedger) -> None:
    class Exploding:
        name = "retrieval"
        dataset = "retrieval"
        route = "owner_request"
        uses_model = True

        def plan(self, context: RunContext) -> list[Any]:
            return []

        async def run(self, context: RunContext) -> TargetResult:
            raise RuntimeError("secret chat text must not reach the file")

    asyncio.run(
        run_target(
            Exploding(),  # type: ignore[arg-type]
            RunContext(),
            meta=meta(LUNA),
            ledger=ledger,
            out_dir=out_dir,
        )
    )


def test_spend_is_written_down_when_a_run_fails_midway(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        run_exploding_target(tmp_path, spent())
    (path,) = tmp_path.glob("*.json")
    assert path.name == "2026-10-25-retrieval-gpt-6-luna-aborted.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["aborted"] is True
    assert data["error"] == "RuntimeError"  # the type only, never the message
    assert data["cost"]["spent_usd"] == "0.01"
    assert "secret" not in path.read_text(encoding="utf-8")


def test_a_run_that_failed_before_spending_writes_nothing(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        run_exploding_target(tmp_path, CallLedger())
    assert not list(tmp_path.iterdir())


def test_compare_shows_not_available_when_no_cost_per_100_items_exists() -> None:
    report = json.loads(
        json.dumps(to_json(meta(LUNA), result(items=0, planned=0), CallLedger()), default=str)
    )
    report["cost"]["per_model"] = {LUNA: {"cost_per_100_items_usd": None}}
    row = render_comparison([report]).splitlines()[-1]
    assert row.rstrip().endswith("| n/a |")


def test_the_review_ledger_source_is_recorded() -> None:
    ledger = ReviewLedger(frozenset({"r01"}), {}, {}, "retrieval.json")
    assert to_json(meta(reviews=ledger), result(), CallLedger())["reviews"] == {
        "source": "retrieval.json",
        "confirmed_items": 1,
    }


def test_compare_shows_quality_and_cost_per_100_items_per_run(tmp_path: Path) -> None:
    _, det = write_report(tmp_path, meta(), result(), CallLedger())
    _, luna = write_report(tmp_path, meta(LUNA), result(), spent())
    table = render_comparison([json.loads(p.read_text(encoding="utf-8")) for p in (det, luna)])
    assert "deterministic" in table
    assert "USD 0" in table
    assert f"| {LUNA} |" in table
    assert "$0.0263" in table  # 0.01 / 38 * 100


async def test_run_target_writes_nothing_without_an_out_dir() -> None:
    class Fixed:
        name = "retrieval"
        dataset = "retrieval"
        route = "owner_request"
        uses_model = False

        def plan(self, context: RunContext) -> list[Any]:
            return []

        async def run(self, context: RunContext) -> TargetResult:
            return result()

    outcome = await run_target(
        Fixed(),  # type: ignore[arg-type]
        RunContext(),
        meta=meta(),
        ledger=CallLedger(),
        out_dir=None,
    )
    assert outcome.markdown is None
    assert outcome.machine is None


# ------------------------------------------------------------------- hygiene
def test_no_model_id_is_hard_coded_in_the_evaluation_code() -> None:
    """Model ids come from configuration (AGENTS §8)."""
    offenders = [
        path.name
        for path in (ROOT / "evals").glob("*.py")
        if re.search(r"\bgpt-[\w.-]+", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_no_provider_file_batch_or_fine_tuning_api_is_used() -> None:
    """AGENTS §8: message content never goes to file, batch or fine-tuning APIs."""
    sources = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "evals").glob("*.py"))
    assert not re.search(r"\.(batches|files|fine_tuning|evals)\.(create|upload)", sources)


def test_the_only_sdk_client_is_built_in_live_py() -> None:
    builders = [
        p.name
        for p in (ROOT / "evals").glob("*.py")
        if "AsyncOpenAI(" in p.read_text(encoding="utf-8")
    ]
    assert builders == ["live.py"]
