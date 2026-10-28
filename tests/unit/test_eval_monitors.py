"""P2 E3 §B part 2 (P2-T43): the `evaluate_monitor` target's estimate and its
model-choice decision, without a model or database.

The cases and reports are hand-built synthetic data; nothing here calls a model.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest
from evals.monitors import MONITOR_COMPARISON, MONITOR_PURPOSE, MonitorTarget, makes_a_call
from evals.schema import MonitorCase
from evals.targets import RunContext
from evals.triage import model_choice, render_choice

from assistant.agent.monitor_eval import MonitorEvalConfig

LUNA, SOL, ASTRA = "gpt-6-luna", "gpt-6-sol", "gpt-6-astra"


def case(
    case_id: str,
    *,
    monitors: Sequence[tuple[str, bool]] = (("SYNTH-MON a delivery is late", False),),
    messages: Sequence[tuple[str, str]] = (("contact", "SYNTH new message"),),
    variant: str = "en",
) -> MonitorCase:
    """A synthetic E3 monitor case (also used by `tests/integration/test_eval_monitors.py`).
    `monitors` are `(description, labelled matching)` with ids 1, 2, …; `messages`
    are `(direction, text)`, a minute apart."""
    record: dict[str, Any] = {
        "id": case_id,
        "variant": variant,
        "monitors": [
            {"id": number, "kind": "semantic", "description": description}
            for number, (description, _) in enumerate(monitors, start=1)
        ],
        "burst": [
            {
                "id": f"{case_id}.m{n:02d}",
                "direction": direction,
                "sent_at": f"2026-11-03T10:{n:02d}:00Z",
                "text": text,
            }
            for n, (direction, text) in enumerate(messages, start=1)
        ],
        "labels": {
            "matched_monitor_ids": [
                number for number, (_, matching) in enumerate(monitors, start=1) if matching
            ]
        },
    }
    return MonitorCase.model_validate_json(json.dumps(record))


# ------------------------------------------------------------------ the estimate
def test_the_estimate_plans_one_call_and_its_retry_per_case() -> None:
    """One call per case for all its monitors (the product's one call per
    burst), at its worst: the truncation retry at the larger budget."""
    cases = [
        case("m001", monitors=[("SYNTH-MON-A a refund", True), ("SYNTH-MON-B an outage", False)]),
        case("m002"),
        # Only the owner wrote: the product has nothing to check and makes no call.
        case("m003", messages=[("owner", "SYNTH see you tomorrow")]),
    ]
    config = MonitorEvalConfig(model=LUNA)
    calls = MonitorTarget(cases).plan(RunContext(model=LUNA))

    assert [c.purpose for c in calls] == [MONITOR_PURPOSE] * 4
    assert [c.max_output_tokens for c in calls] == [
        config.max_output_tokens,
        config.max_output_tokens_ceiling,
    ] * 2
    # The first case lists two monitors, the second one: more to read.
    assert calls[0].input_chars > calls[2].input_chars > 0
    assert len(MonitorTarget(cases).plan(RunContext(model=LUNA, limit=1))) == 2


def test_only_a_case_with_a_contact_message_makes_a_call() -> None:
    assert makes_a_call(case("m001"))
    assert makes_a_call(case("m001", messages=[("owner", "SYNTH ok"), ("contact", "SYNTH hi")]))
    assert not makes_a_call(case("m001", messages=[("owner", "SYNTH ok")]))


def test_only_names_cases_of_the_set_and_never_the_whole_set() -> None:
    target = MonitorTarget([case("m001"), case("m002"), case("m003")])
    assert [c.id for c in target.named(["m003", "m001"])] == ["m001", "m003"]
    with pytest.raises(ValueError, match="unknown cases: m009"):
        target.named(["m009"])
    for bad in ([], ["m001", "m002", "m003"]):
        with pytest.raises(ValueError, match="at least one case and not the whole set"):
            target.named(bad)
    with pytest.raises(ValueError, match="cannot be combined"):
        target.named(["m001"], limit=1)


# ------------------------------------------------------------------ model choice
def report(
    model: str,
    *,
    accuracy: str = "pass",
    false_matches: str = "pass",
    target: str = "monitors",
    files: dict[str, str] | None = None,
    cost: str | None = "0.0100",
    prompts: Sequence[str] = ("evaluate_monitor.v1",),
    effort: str | None = "low",
) -> dict[str, Any]:
    """A full monitor run's JSON report: its two thresholds are the model-choice
    bar, not an owner-confirmed gate (`gated: false`)."""
    return {
        "target": target,
        "model": model,
        "effort": effort,
        "complete": True,
        "trial": False,
        "dataset_files": files or {"e3/bursts.jsonl": "aa", "e3/monitor_cases.jsonl": "m1"},
        "prompts": list(prompts),
        "metrics": [
            {
                "key": "monitor_accuracy",
                "label": "Monitor accuracy",
                "threshold": 0.9,
                "gated": False,
                "prescreen_status": accuracy,
            },
            {
                "key": "false_match_rate",
                "label": "False-match rate",
                "threshold": 0.1,
                "gated": False,
                "prescreen_status": false_matches,
            },
            {
                "key": "match_recall",
                "label": "Match recall",
                "threshold": None,
                "gated": False,
                "prescreen_status": "info",
            },
        ],
        "cost": {"per_model": {model: {"cost_per_100_items_usd": cost}}},
    }


def choose(reports: Sequence[dict[str, Any]], route_model: str = LUNA) -> Any:
    return model_choice(reports, route_model=route_model, comparison=MONITOR_COMPARISON)


def test_the_monitor_choice_uses_its_bar_and_names_the_evaluate_monitor_route() -> None:
    assert choose([report(LUNA)]).decision.startswith(f"route stays on {LUNA}")
    luna_misses = report(LUNA, false_matches="fail")
    assert "run the next tier" in choose([luna_misses]).decision
    # A bar that could not be measured is not met either.
    assert not choose([report(LUNA, false_matches="not evaluable")]).tiers[0].meets

    proposed = choose([luna_misses, report(SOL, cost="0.0700")])
    assert proposed.decision.startswith(f"propose LLM_ROUTE_EVALUATE_MONITOR_MODEL={SOL}")
    assert proposed.tiers[0].misses == ("False-match rate",)
    text = render_choice(proposed)
    assert "Model choice (evaluate_monitor route" in text
    assert f"- {LUNA}: misses False-match rate; cost per 100 cases $0.0100" in text
    assert f"- {SOL}: meets every threshold; cost per 100 cases $0.0700" in text

    sol_misses = report(SOL, accuracy="fail")
    astra = choose([luna_misses, sol_misses, report(ASTRA)])
    assert astra.decision.startswith(f"propose LLM_ROUTE_EVALUATE_MONITOR_MODEL={ASTRA}")


def test_the_monitor_choice_compares_only_the_monitor_cases() -> None:
    luna = report(LUNA, accuracy="fail")
    # The bursts changed between the runs: the monitor cases did not.
    bursts_changed = {"e3/bursts.jsonl": "bb", "e3/monitor_cases.jsonl": "m1"}
    assert choose([luna, report(SOL, files=bursts_changed)]).tiers[1].model == SOL
    cases_changed = {"e3/bursts.jsonl": "aa", "e3/monitor_cases.jsonl": "m2"}
    with pytest.raises(ValueError, match="E3 dataset versions"):
        choose([luna, report(SOL, files=cases_changed)])
    with pytest.raises(ValueError, match="names no E3 dataset file"):
        choose([report(LUNA, files={"e3/bursts.jsonl": "aa"})])


@pytest.mark.parametrize(
    ("sol", "reason"),
    [
        (report(SOL, prompts=("evaluate_monitor.v2",)), "different prompt versions"),
        (report(SOL, effort="medium"), "different reasoning efforts"),
    ],
)
def test_the_monitor_choice_refuses_runs_that_differ_in_more_than_the_model(
    sol: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        choose([report(LUNA, accuracy="fail"), sol])


def test_a_comparison_refuses_a_run_of_the_other_target() -> None:
    with pytest.raises(ValueError, match="'triage' is not a monitors run"):
        choose([report(LUNA, target="triage")])
    with pytest.raises(ValueError, match="'monitors' is not a triage run"):
        model_choice([report(LUNA)], route_model=LUNA)
    with pytest.raises(ValueError, match=f"the route's current model {SOL}"):
        choose([report(LUNA)], route_model=SOL)
