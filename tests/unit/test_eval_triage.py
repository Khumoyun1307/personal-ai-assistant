"""P2 E3 §B: the triage model comparison's decision rule, without a model or database.

The reports are hand-built JSON in the shape `evals/report.py` writes; nothing
here calls a model.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest
from evals.schema import Burst
from evals.triage import makes_a_call, model_choice, render_choice

LUNA, SOL, ASTRA = "gpt-6-luna", "gpt-6-sol", "gpt-6-astra"


def burst(
    burst_id: str,
    *,
    alert: bool = False,
    needs_reply: bool = False,
    suspicious: bool = False,
    history: Sequence[tuple[str, str]] = (),
    messages: Sequence[tuple[str, str]] = (("contact", "SYNTH new message"),),
    open_tasks: Sequence[tuple[str, str, str]] = (),
    resolved: Sequence[str] = (),
    commitments: Sequence[tuple[str, str]] = (),
    variant: str = "en",
) -> Burst:
    """A synthetic E3 burst (also used by `tests/integration/test_eval_triage.py`).
    `history`/`messages` are `(direction, text)`; `open_tasks` are `(kind, title,
    source message id)`; `resolved` names the open tasks the burst resolves (the
    rest are unresolved); `commitments` are `(by, source message id)`. An alert
    is a decision request."""
    task_ids = [f"{burst_id}.t{n}" for n in range(1, len(open_tasks) + 1)]
    record: dict[str, Any] = {
        "id": burst_id,
        "variant": variant,
        "now": "2026-11-03T14:30:00Z",
        "owner_language": "en",
        "owner_timezone": "Asia/Tashkent",
        "contact": {"display_name": "Dana Synthetic"},
        "history": [
            {
                "id": f"{burst_id}.h{n:02d}",
                "direction": direction,
                "sent_at": f"2026-11-03T09:{n:02d}:00Z",
                "text": text,
            }
            for n, (direction, text) in enumerate(history, start=1)
        ],
        "open_tasks": [
            {"id": task_id, "kind": kind, "title": title, "source": source}
            for task_id, (kind, title, source) in zip(task_ids, open_tasks, strict=True)
        ],
        "burst": [
            {
                "id": f"{burst_id}.m{n:02d}",
                "direction": direction,
                "sent_at": f"2026-11-03T14:{n:02d}:00Z",
                "text": text,
            }
            for n, (direction, text) in enumerate(messages, start=1)
        ],
        "labels": {
            "alert": alert,
            "needs_reply": needs_reply or alert,
            "contains_question_to_owner": alert,
            "requests_decision": alert,
            "urgency": "normal",
            "escalation_category": "suspicious" if suspicious else "none",
            "suspicious": suspicious,
            "commitments": [
                {"id": f"{burst_id}.c{n}", "by": by, "source": source}
                for n, (by, source) in enumerate(commitments, start=1)
            ],
            "resolved_tasks": [t for t in task_ids if t in resolved],
            "unresolved_tasks": [t for t in task_ids if t not in resolved],
        },
    }
    return Burst.model_validate_json(json.dumps(record))


def report(
    model: str,
    *,
    recall: str = "pass",
    closures: str = "pass",
    complete: bool = True,
    trial: bool = False,
    files: dict[str, str] | None = None,
    cost: str | None = "0.0600",
    prompts: Sequence[str] = ("triage.v1",),
    effort: str | None = "low",
) -> dict[str, Any]:
    return {
        "target": "triage",
        "model": model,
        "effort": effort,
        "complete": complete,
        "trial": trial,
        "dataset_files": files or {"e3/bursts.jsonl": "aa11"},
        "prompts": list(prompts),
        "metrics": [
            {
                "key": "alert_recall",
                "label": "Urgent/decision recall",
                "threshold": 0.9,
                "gated": True,
                "prescreen_status": recall,
            },
            {
                "key": "false_closure_rate",
                "label": "False-closure rate",
                "threshold": 0.1,
                "gated": True,
                "prescreen_status": closures,
            },
            {
                "key": "needs_reply_agreement",
                "label": "needs_reply agrees with the label",
                "threshold": None,
                "gated": False,
                "prescreen_status": "info",
            },
        ],
        "cost": {"per_model": {model: {"cost_per_100_items_usd": cost}}},
    }


def test_luna_meeting_every_threshold_keeps_the_route() -> None:
    choice = model_choice([report(LUNA)], route_model=LUNA)
    assert choice.decision.startswith(f"route stays on {LUNA}")
    assert choice.tiers[0].meets


def test_a_miss_calls_for_the_next_tier_and_the_first_tier_that_meets_is_proposed() -> None:
    luna_misses = report(LUNA, recall="fail")
    assert "run the next tier" in model_choice([luna_misses], route_model=LUNA).decision
    # A threshold that could not be measured is not met either.
    unmeasured = report(LUNA, closures="not evaluable")
    assert not model_choice([unmeasured], route_model=LUNA).tiers[0].meets

    proposed = model_choice([luna_misses, report(SOL)], route_model=LUNA)
    assert proposed.decision.startswith(f"propose LLM_ROUTE_TRIAGE_MODEL={SOL}")
    assert "owner's approval" in proposed.decision
    assert proposed.tiers[0].misses == ("Urgent/decision recall",)

    sol_misses = report(SOL, recall="fail")
    assert "run the next tier" in model_choice([luna_misses, sol_misses], route_model=LUNA).decision
    astra = model_choice([luna_misses, sol_misses, report(ASTRA)], route_model=LUNA)
    assert astra.decision.startswith(f"propose LLM_ROUTE_TRIAGE_MODEL={ASTRA}")
    every = [luna_misses, sol_misses, report(ASTRA, closures="fail")]
    nobody = model_choice(every, route_model=LUNA)
    assert nobody.decision == "no tier tried meets every threshold: the owner decides"


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        (report(LUNA, trial=True), "not a complete full run"),
        (report(LUNA, complete=False), "not a complete full run"),
        ({**report(LUNA), "target": "summary"}, "not a triage run"),
    ],
)
def test_the_choice_refuses_anything_but_complete_full_triage_runs(
    bad: dict[str, Any], reason: str
) -> None:
    with pytest.raises(ValueError, match=reason):
        model_choice([bad], route_model=LUNA)


def test_the_choice_refuses_runs_on_different_e3_versions_and_ignores_other_datasets() -> None:
    luna = report(LUNA, recall="fail", files={"e3/bursts.jsonl": "aa", "retrieval/q.jsonl": "r1"})
    with pytest.raises(ValueError, match="E3 dataset versions"):
        model_choice([luna, report(SOL, files={"e3/bursts.jsonl": "bb"})], route_model=LUNA)
    sol = report(SOL, files={"e3/bursts.jsonl": "aa", "e3/monitor_cases.jsonl": "m2"})
    with pytest.raises(ValueError, match="E3 dataset versions"):
        model_choice([luna, sol], route_model=LUNA)
    # Another dataset changed between the runs (a new set landed, CP4): E3 did not.
    later = report(SOL, files={"e3/bursts.jsonl": "aa", "retrieval/q.jsonl": "r2", "e4/x": "x"})
    assert model_choice([luna, later], route_model=LUNA).tiers[1].model == SOL
    with pytest.raises(ValueError, match="names no E3 dataset file"):
        model_choice([report(LUNA, files={"retrieval/q.jsonl": "r1"})], route_model=LUNA)


@pytest.mark.parametrize(
    ("sol", "reason"),
    [
        (report(SOL, prompts=("triage.v2",)), "different prompt versions"),
        (report(SOL, effort="medium"), "different reasoning efforts"),
        (report(SOL, effort=None), "different reasoning efforts"),
        ({k: v for k, v in report(SOL).items() if k != "prompts"}, "names no prompt version"),
    ],
)
def test_the_choice_refuses_runs_with_different_prompt_versions_or_effort(
    sol: dict[str, Any], reason: str
) -> None:
    """A prompt bump or an effort change between the tiers would be credited
    to the model: such runs are not comparable."""
    with pytest.raises(ValueError, match=reason):
        model_choice([report(LUNA, recall="fail"), sol], route_model=LUNA)


def test_the_first_report_must_be_the_routes_current_model() -> None:
    """The argument order is not trusted: the decision never says the route
    "stays on" a model the route is not on."""
    with pytest.raises(ValueError, match=f"the route's current model {LUNA}"):
        model_choice([report(SOL), report(LUNA, recall="fail")], route_model=LUNA)
    with pytest.raises(ValueError, match=f"the route's current model {LUNA}"):
        model_choice([report(SOL)], route_model=LUNA)
    with pytest.raises(ValueError, match=f"{LUNA} more than once"):
        model_choice([report(LUNA, recall="fail"), report(LUNA)], route_model=LUNA)
    # A route already moved to Sol: Sol is the tier the decision starts from.
    assert model_choice([report(SOL)], route_model=SOL).decision.startswith(f"route stays on {SOL}")


def test_the_decision_lists_each_tier_with_its_cost_per_100_bursts() -> None:
    tiers = [report(LUNA, recall="fail"), report(SOL, cost="0.4100")]
    text = render_choice(model_choice(tiers, route_model=LUNA))
    assert f"- {LUNA}: misses Urgent/decision recall; cost per 100 bursts $0.0600" in text
    assert f"- {SOL}: meets every threshold; cost per 100 bursts $0.4100" in text
    assert f"Decision: propose LLM_ROUTE_TRIAGE_MODEL={SOL}" in text


def test_the_product_makes_no_call_for_an_owner_only_burst_without_open_tasks() -> None:
    assert makes_a_call(burst("b001"))
    assert not makes_a_call(burst("b001", messages=[("owner", "SYNTH ok")]))
    with_task = burst(
        "b001",
        history=[("contact", "SYNTH I will send it")],
        open_tasks=[("contact_commitment", "TASK", "b001.h01")],
        messages=[("owner", "SYNTH got it")],
    )
    assert makes_a_call(with_task)
