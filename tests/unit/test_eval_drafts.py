"""P2 E2 §B: the draft eval's deterministic checks, ratings and statuses.

Pure functions only: no database, no model. Every situation and draft here is
synthetic (AGENTS §8).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.drafts import (
    DraftChecks,
    DraftRecord,
    JudgeResult,
    Leak,
    acceptability,
    canary_leaks,
    check_draft,
    draft_metrics,
    unsupported_values,
)
from evals.review import ReviewLedger, rating_key
from evals.schema import E2Situation
from evals.targets import Measure, Status

from assistant.agent.draft_schemas import DraftResult
from tests.unit.test_draft_units import output
from tests.unit.test_e2_dataset import _new

SITUATION = E2Situation.model_validate_json(json.dumps(_new("s001")))
#: The chat the drafter is shown for SITUATION, as the target renders it.
CONTEXT = "\n".join(m.text or "" for m in SITUATION.conversation.messages)  # type: ignore[union-attr]


def draft(**changes: object) -> DraftResult:
    return DraftResult.model_validate(output(**changes))


def record(
    situation_id: str = "s001",
    *,
    variant: str = "en",
    result: DraftResult | None = None,
    checks: DraftChecks | None = None,
    status: str = "drafted",
) -> DraftRecord:
    result = result if result is not None else draft()
    situation = E2Situation.model_validate_json(
        json.dumps(_new(situation_id, canary=f"QZ-{situation_id}-1"))
    )
    return DraftRecord(
        situation_id=situation_id,
        variant=variant,  # type: ignore[arg-type]
        source="new",
        status=status,
        error=None,
        calls=1,
        result=result,
        checks=checks or check_draft(situation, result, context=CONTEXT, candidates={}),
        judge=JudgeResult(screened=False, error="not_asked"),
    )


# ------------------------------------------------------------------ leaks
def test_a_canary_from_another_situation_is_a_leak() -> None:
    candidates = {"s002": ("ZX-77341",), "s003": ("Chilonzor 7-mavze",)}
    text = "Sure, order zx-77341 is on its way."  # case folded: still the canary

    leaks = canary_leaks(text, candidates=candidates, own_context=CONTEXT)

    assert leaks == (Leak(source="s002", text="ZX-77341"),)
    # A string the situation's own chat shows is not a leak, wherever else it occurs.
    assert canary_leaks("QZ-48213", candidates={"s002": ("QZ-48213",)}, own_context=CONTEXT) == ()
    # The check reports the leak on the draft's record, by situation id.
    leaked = check_draft(SITUATION, draft(text=text), context=CONTEXT, candidates=candidates)
    assert leaked.leaks == (Leak("s002", "ZX-77341"),)
    metrics, _, _, headline = draft_metrics(
        [record(checks=leaked), record("s002")], ReviewLedger.empty(), set_size=2
    )
    leak_metric = next(m for m in metrics if m.key == "canary_leaks")
    assert leak_metric.all_items == Measure(1, 2)
    assert leak_metric.failed_items == ("s001",)
    assert any("1 canary leak" in line and "s001" in line for line in headline)


# --------------------------------------------------------- values and amounts
def test_numbers_not_in_the_context_are_flagged() -> None:
    # The context (CONTEXT) says the order came at 18:00; nothing else has a number.
    assert unsupported_values("It came at 18:00, thanks!", CONTEXT) == ()
    assert unsupported_values("I can pick it up at 19:30.", CONTEXT) == ("h1930",)
    assert unsupported_values("That will be 250 000 so'm.", CONTEXT) == ("250000",)
    # The same value written another way is the same value.
    assert unsupported_values("Received at 6:00 p.m.", "Kelgan vaqti 18:00") == ()
    checks = check_draft(
        SITUATION, draft(text="See you on the 14th at 19:30"), context=CONTEXT, candidates={}
    )
    assert checks.unsupported_values == ("14", "h1930")


# ------------------------------------------------------------- needs_reply
def test_needs_reply_disagreement_is_reported() -> None:
    silent = draft(needs_reply=False, text="", skip_reason="no_reply_needed")
    checks = check_draft(SITUATION, silent, context=CONTEXT, candidates={})

    assert checks.needs_reply_agrees is False
    assert checks.length_ok is False  # a reply was needed and none was written
    metrics, breakdowns, _, _ = draft_metrics(
        [record(result=silent, checks=checks, status="not_needed"), record("s002")],
        ReviewLedger.empty(),
        set_size=2,
    )
    agreement = next(m for m in metrics if m.key == "needs_reply_agreement")
    assert agreement.all_items == Measure(1, 2)
    assert agreement.failed_items == ("s001",)
    assert breakdowns["needs_reply agreement by variant"]["en"] == Measure(1, 2)


# ---------------------------------------------------------------- ratings
def test_ratings_are_keyed_by_the_draft_they_rated(tmp_path: Path) -> None:
    first = rating_key("s001", "Yes, it came at 18:00.")
    rerun = rating_key("s001", "Yes, it arrived at 18:00.")
    path = tmp_path / "e2.json"
    path.write_text(json.dumps({"ratings": {first: "minor"}}), encoding="utf-8")

    ledger = ReviewLedger.load(path)

    assert first.startswith("s001#")
    assert first != rerun
    assert ledger.rating(first) == "minor"
    assert ledger.rating(rerun) is None  # another draft: the rating does not carry over
    # A ledger written before ratings existed still loads, with none.
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"confirmed_items": ["c01"], "coverage": {}}), encoding="utf-8")
    assert ReviewLedger.load(old).ratings == {}


def test_acceptability_counts_only_rated_drafts(tmp_path: Path) -> None:
    drafts = [
        ("s001", "en", rating_key("s001", "a")),
        ("s002", "en", rating_key("s002", "b")),
        ("s003", "en", rating_key("s003", "c")),
        ("s004", "ru", rating_key("s004", "d")),
        ("s005", "ru", rating_key("s005", "e")),
    ]
    ratings: dict[str, Any] = {
        drafts[0][2]: "as_is",
        drafts[1][2]: "major",
        drafts[3][2]: "minor",
    }
    path = tmp_path / "e2.json"
    path.write_text(json.dumps({"ratings": ratings}), encoding="utf-8")

    result = acceptability(drafts, ReviewLedger.load(path))

    assert result.per_variant == {"en": Measure(1, 2), "ru": Measure(1, 1)}
    assert result.overall == Measure(2, 3)
    assert result.unrated == (drafts[2][2], drafts[4][2])


# ----------------------------------------------------------------- status
def test_the_status_is_always_info_in_p2(tmp_path: Path) -> None:
    perfect = [record("s001"), record("s002", variant="ru")]
    path = tmp_path / "e2.json"
    everything = {
        "confirmed_items": ["s001", "s002"],
        "ratings": {r.rating_key: "as_is" for r in perfect if r.rating_key is not None},
    }
    path.write_text(json.dumps(everything), encoding="utf-8")

    for ledger in (ReviewLedger.empty(), ReviewLedger.load(path)):
        metrics, _, _, _ = draft_metrics(perfect, ledger, set_size=2)
        assert metrics
        for metric in metrics:
            assert metric.prescreen_status() is Status.INFO, metric.key
            assert metric.gate_status() is Status.INFO, metric.key
