"""E3 — the synthetic triage set (IMPLEMENTATION_PLAN §10.1; E3 spec §A/§B).

The schema's load-time integrity rules are tested on small synthetic records
here; the dataset itself (Junior's E3D) is checked against the §A minimums
once it exists — until then those tests skip. Everything is synthetic
(AGENTS §8). `evals/` sits outside `src/`; pytest's rootdir on `sys.path`
makes it importable.
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from typing import Any

import pytest
from evals.schema import (
    E3_MONITORS_PATH,
    E3_PATH,
    VARIANTS,
    Burst,
    MonitorCase,
    load_e3,
    load_monitor_cases,
)
from pydantic import ValidationError

MIN_BURSTS = 150
MIN_ALERTS = 40
MIN_PER_VARIANT = 25
MIN_TASK_BURSTS = 30
MIN_RESOLUTIONS = 30
MIN_NOTHING_TO_DO = 15
MIN_SUSPICIOUS = 10
MIN_OWNER_IN_BURST = 10
MIN_MONITOR_CASES = 30
MIN_MONITOR_PER_VARIANT = 5

needs_e3 = pytest.mark.skipif(not E3_PATH.exists(), reason="E3D not merged yet")


def parse_burst(data: dict[str, Any]) -> Burst:
    """As the loader reads a line: JSON mode (strict models take ISO times
    only from JSON)."""
    return Burst.model_validate_json(json.dumps(data))


def parse_case(data: dict[str, Any]) -> MonitorCase:
    return MonitorCase.model_validate_json(json.dumps(data))


needs_monitor_cases = pytest.mark.skipif(not E3_MONITORS_PATH.exists(), reason="E3D not merged yet")


def record(**overrides: Any) -> dict[str, Any]:
    """A valid burst in the §A layout; tests break one rule at a time."""
    base: dict[str, Any] = {
        "id": "b001",
        "variant": "uz-Latn",
        "now": "2026-11-03T14:05:00Z",
        "owner_language": "uz-Latn",
        "owner_timezone": "Asia/Tashkent",
        "contact": {"display_name": "Dilnoza R."},
        "contact_priority": "normal",
        "history": [
            {
                "id": "b001.h01",
                "direction": "owner",
                "sent_at": "2026-11-03T09:00:00Z",
                "text": "Hisobotni bugun yuboraman.",
            },
            {
                "id": "b001.h02",
                "direction": "contact",
                "sent_at": "2026-11-03T09:05:00Z",
                "text": "Shartnomani ertaga olib kelaman.",
            },
        ],
        "open_tasks": [
            {
                "id": "b001.t1",
                "kind": "owner_commitment",
                "title": "Hisobotni yuborish",
                "source": "b001.h01",
                "due": "2026-11-03T18:00:00Z",
            },
            {
                "id": "b001.t2",
                "kind": "contact_commitment",
                "title": "Shartnomani olib kelish",
                "source": "b001.h02",
                "due": None,
            },
        ],
        "burst": [
            {
                "id": "b001.m01",
                "direction": "contact",
                "sent_at": "2026-11-03T14:01:00Z",
                "text": "Hisobot keldi, rahmat! Narxni soat 17:00 gacha tasdiqlaysizmi?",
            },
            {
                "id": "b001.m02",
                "direction": "contact",
                "sent_at": "2026-11-03T14:02:00Z",
                "text": None,
                "media": "document",
            },
        ],
        "labels": {
            "alert": True,
            "needs_reply": True,
            "contains_question_to_owner": True,
            "requests_decision": True,
            "urgency": "high",
            "escalation_category": "urgent_decision",
            "deadline": "2026-11-03T12:00:00Z",
            "suspicious": False,
            "commitments": [],
            "questions": [{"id": "b001.q1", "source": "b001.m01", "is_decision": True}],
            "resolved_tasks": ["b001.t1"],
            "unresolved_tasks": ["b001.t2"],
        },
        "tags": ["deadline_today"],
    }
    for key, value in overrides.items():
        base[key] = value
    return base


def with_labels(**changes: Any) -> dict[str, Any]:
    data = record()
    data["labels"] = {**data["labels"], **changes}
    return data


def monitor_case(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "m001",
        "variant": "ru",
        "monitors": [{"id": 1, "kind": "semantic", "description": "asks me to decide"}],
        "burst": [
            {
                "id": "m001.m01",
                "direction": "contact",
                "sent_at": "2026-11-03T10:00:00Z",
                "text": "Решите, пожалуйста, по проекту до пятницы.",
            }
        ],
        "labels": {"matched_monitor_ids": [1]},
    }
    base.update(overrides)
    return base


def test_a_valid_record_loads() -> None:
    burst = parse_burst(record())
    assert burst.labels.alert
    assert [m.id for m in burst.messages()] == ["b001.h01", "b001.h02", "b001.m01", "b001.m02"]
    assert parse_case(monitor_case()).labels.matched_monitor_ids == [1]


# ------------------------------------------------------------ integrity rules
def test_the_alert_label_follows_its_rule() -> None:
    """`alert` is true exactly when urgency is urgent, a decision is requested,
    or the category is urgent_decision (the urgent/decision positives)."""
    with pytest.raises(ValidationError, match="alert"):
        parse_burst(with_labels(alert=False))
    with pytest.raises(ValidationError, match="alert"):
        parse_burst(
            with_labels(
                alert=True,
                requests_decision=False,
                urgency="normal",
                escalation_category="needs_owner",
                questions=[{"id": "b001.q1", "source": "b001.m01", "is_decision": False}],
            )
        )
    urgent_only = with_labels(
        alert=True, requests_decision=False, urgency="urgent", escalation_category="needs_owner"
    )
    assert parse_burst(urgent_only).labels.alert


def test_every_open_task_is_in_exactly_one_resolution_list() -> None:
    for resolved, unresolved in (
        (["b001.t1"], []),  # t2 in neither
        (["b001.t1", "b001.t2"], ["b001.t2"]),  # t2 in both
        (["b001.t1", "b001.t9"], ["b001.t2"]),  # an unknown task
    ):
        with pytest.raises(ValidationError, match="resolution"):
            parse_burst(with_labels(resolved_tasks=resolved, unresolved_tasks=unresolved))


def test_open_task_sources_are_from_the_side_the_task_belongs_to() -> None:
    """The product never creates a task citing the other side (S05.1 §3.4), so
    neither may the dataset: a contact commitment cites the contact's promise,
    an owner commitment the owner's, a question or decision the contact."""
    for kind, source in (
        ("contact_commitment", "b001.h01"),  # the owner's message
        ("owner_commitment", "b001.h02"),  # the contact's message
        ("open_question", "b001.h01"),
        ("decision", "b001.h01"),
    ):
        data = record()
        data["open_tasks"][0] = {**data["open_tasks"][0], "kind": kind, "source": source}
        with pytest.raises(ValidationError, match="task"):
            parse_burst(data)


def test_two_open_tasks_of_one_kind_never_share_a_source() -> None:
    """The product keeps one agent task per kind and first source message
    (`ux_tasks_agent_dedupe`, S05.1 §3.5): the E3 target could not seed such a
    pair, so the dataset may not hold one. Different kinds may share a message."""
    data = record()
    data["open_tasks"][1] = {**data["open_tasks"][1], "kind": "owner_commitment"}
    data["open_tasks"][1]["source"] = "b001.h01"
    with pytest.raises(ValidationError, match="first source"):
        parse_burst(data)
    question = record()
    question["open_tasks"][1] = {**question["open_tasks"][1], "kind": "open_question"}
    assert len(parse_burst(question).open_tasks) == 2  # the contact's h02, another kind


def test_commitment_sources_are_from_the_committing_side() -> None:
    owner_promise_cites_contact = with_labels(
        commitments=[{"id": "b001.c1", "by": "owner", "source": "b001.m01", "values": []}]
    )
    with pytest.raises(ValidationError, match="side"):
        parse_burst(owner_promise_cites_contact)
    question_cites_owner = with_labels(
        questions=[{"id": "b001.q1", "source": "b001.h01", "is_decision": True}]
    )
    with pytest.raises(ValidationError, match="contact message"):
        parse_burst(question_cites_owner)
    fine = with_labels(
        commitments=[{"id": "b001.c1", "by": "contact", "source": "b001.h02", "values": []}]
    )
    assert parse_burst(fine).labels.commitments[0].by == "contact"


def test_values_are_literal_substrings() -> None:
    """Folded like E1: case, NFKC and apostrophe variants do not matter."""
    ok = with_labels(
        commitments=[{"id": "b001.c1", "by": "contact", "source": "b001.h02", "values": ["ERTAGA"]}]
    )
    assert parse_burst(ok)
    with pytest.raises(ValidationError, match="value"):
        parse_burst(
            with_labels(
                commitments=[
                    {"id": "b001.c1", "by": "contact", "source": "b001.h02", "values": ["indin"]}
                ]
            )
        )


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda d: d.update(id="burst1"), "id"),
        (lambda d: d["burst"][0].update(id="b002.m01"), "message ids"),
        (lambda d: d["burst"][0].update(id="b001.h03"), "message ids"),
        (lambda d: d["history"][1].update(id="b001.h01"), "duplicate"),
        (lambda d: d["open_tasks"][0].update(id="b001.x1"), "task ids"),
        (lambda d: d["open_tasks"][0].update(source="b001.h09"), "unknown message"),
        (lambda d: d["labels"]["questions"][0].update(id="b001.c1"), "label ids"),
        (lambda d: d["labels"]["questions"][0].update(source="b001.m09"), "unknown message"),
        (lambda d: d["burst"][0].update(sent_at="2026-11-03T08:00:00Z"), "chronological"),
        (lambda d: d.update(now="2026-11-03T14:01:30Z"), "now"),
        (lambda d: d.update(now="2026-11-03T19:05:00+05:00"), "UTC"),
        (lambda d: d.update(owner_timezone="Nowhere/City"), "timezone"),
        (lambda d: d.update(variant="kz"), "variant"),
        (lambda d: d.update(burst=[]), "burst"),
        (lambda d: d["burst"][1].update(media=None), "text or media"),
        (lambda d: d["labels"].update(escalation_category="suspicious"), "suspicious"),
    ],
)
def test_bad_ids_fail_at_load(change: Any, match: str) -> None:
    """Malformed ids, citations, times and labels fail at load, for the
    runner and the tests alike."""
    data = copy.deepcopy(record())
    change(data)
    with pytest.raises(ValidationError, match=match):
        parse_burst(data)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda d: d["labels"].update(matched_monitor_ids=[2]), "unknown monitor"),
        (lambda d: d["monitors"].append(dict(d["monitors"][0])), "duplicate monitor"),
        (lambda d: d["burst"][0].update(id="m002.m01"), "message ids"),
        (lambda d: d.update(id="case1"), "id"),
    ],
)
def test_bad_monitor_cases_fail_at_load(change: Any, match: str) -> None:
    data = copy.deepcopy(monitor_case())
    change(data)
    with pytest.raises(ValidationError, match=match):
        parse_case(data)


# ------------------------------------------------------------ the dataset (E3D)
@needs_e3
def test_e3_loads_and_meets_the_minimum_counts() -> None:
    """E3 spec §A minimums (IMPLEMENTATION_PLAN §10.1: ≥ 150 bursts, ≥ 40
    urgent/decision positives)."""
    bursts = load_e3()
    ids = [b.id for b in bursts]
    assert len(ids) == len(set(ids)), "burst ids are unique"
    assert len(bursts) >= MIN_BURSTS
    assert sum(b.labels.alert for b in bursts) >= MIN_ALERTS
    per_variant = Counter(b.variant for b in bursts)
    assert all(per_variant[v] >= MIN_PER_VARIANT for v in VARIANTS), per_variant
    with_tasks = [b for b in bursts if b.open_tasks]
    resolved = sum(len(b.labels.resolved_tasks) for b in bursts)
    decisions = resolved + sum(len(b.labels.unresolved_tasks) for b in bursts)
    assert len(with_tasks) >= MIN_TASK_BURSTS
    assert decisions >= MIN_RESOLUTIONS
    assert 0.35 <= resolved / decisions <= 0.65, "about half resolved"
    assert sum(b.nothing_to_do() for b in bursts) >= MIN_NOTHING_TO_DO
    assert sum(b.labels.suspicious for b in bursts) >= MIN_SUSPICIOUS
    assert sum(b.owner_wrote_in_burst() for b in bursts) >= MIN_OWNER_IN_BURST


@needs_monitor_cases
def test_e3_monitor_cases_meet_the_minimum_counts() -> None:
    cases = load_monitor_cases()
    assert len({c.id for c in cases}) == len(cases) >= MIN_MONITOR_CASES
    per_variant = Counter(c.variant for c in cases)
    assert all(per_variant[v] >= MIN_MONITOR_PER_VARIANT for v in VARIANTS), per_variant
    matching = sum(bool(c.labels.matched_monitor_ids) for c in cases)
    assert 0.35 <= matching / len(cases) <= 0.65, "about half matching"
