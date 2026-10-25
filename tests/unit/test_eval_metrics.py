"""S03.4: the deterministic E1 metrics, on hand-built fixtures (no model).

Thresholds are the S03.4 plan's: open-loop recall >= 95 %, a coverage caveat
whenever gaps exist, owner-facing language correct 100 %. Gate results count
only owner-confirmed items (IMPLEMENTATION_PLAN §10.1): with none, a metric is
"not evaluable", never a pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from evals.metrics import (
    SummaryOutput,
    caveat_metrics,
    check_caveats,
    check_language,
    count_metric,
    detect_language,
    language_metric,
    open_loop_coverage,
    open_loops,
    ratio_metric,
    recall_metric,
    values_matcher,
)
from evals.review import ReviewLedger, finding_id
from evals.schema import Conversation
from evals.targets import Measure, MetricResult, Status
from pydantic import ValidationError

MESSAGES = [
    ("contact", "Bekzod, ertaga soat 15:00 da uchrashamizmi?"),
    ("contact", "Shuningdek 250 ming so'm to'lovni yuborasizmi?"),
    ("owner", "Ha, uchrashamiz. Hisobotni juma kuni yuboraman"),
    ("contact", "Manzil: Qorasuv-3, 17-uy"),
    ("contact", "Rahmat"),
    ("owner", "Xo'p"),
]


def conversation(
    cid: str = "c90",
    *,
    gaps: list[dict[str, Any]] | None = None,
    caveats: list[dict[str, Any]] | None = None,
    coverage_started_at: str | None = None,
) -> Conversation:
    """A six-message conversation with four open loops (q1, q2, c1, c2) and
    one answered question (q3). q1, q2 and c1 carry `values`; c2 has none, so
    it cannot be matched by string."""
    messages = [
        {
            "id": f"{cid}.m{number:02d}",
            "direction": direction,
            "sent_at": f"2026-10-20T10:0{number}:00Z",
            "text": text,
        }
        for number, (direction, text) in enumerate(MESSAGES, start=1)
    ]
    labels = {
        "questions_to_owner": [
            {
                "id": f"{cid}.q1",
                "source_message_ids": [f"{cid}.m01"],
                "summary": "Meeting tomorrow at 15:00?",
                "status": "open",
                "values": ["15:00"],
            },
            {
                "id": f"{cid}.q2",
                "source_message_ids": [f"{cid}.m02"],
                "summary": "Will the owner send the payment?",
                "status": "open",
                "values": ["250 ming"],
            },
            {
                "id": f"{cid}.q3",
                "source_message_ids": [f"{cid}.m01"],
                "summary": "Meeting? (answered)",
                "status": "answered",
                "answered_in": [f"{cid}.m03"],
                "values": ["15:00"],
            },
        ],
        "commitments": [
            {
                "id": f"{cid}.c1",
                "source_message_ids": [f"{cid}.m03"],
                "who": "owner",
                "who_name": "Bekzod",
                "what": "send the report",
                "when": "Friday",
                "status": "open",
                "values": ["juma kuni"],
            },
            {
                "id": f"{cid}.c2",
                "source_message_ids": [f"{cid}.m04"],
                "who": "contact",
                "who_name": "Dilnoza",
                "what": "will confirm the address",
                "status": "open",
            },
        ],
        "expected_caveats": caveats or [],
    }
    record = {
        "id": cid,
        "variant": "uz-Latn",
        "scenario": "synthetic metrics fixture",
        "owner": {"display_name": "Bekzod"},
        "contact": {"display_name": "Dilnoza"},
        "coverage_started_at": coverage_started_at,
        "coverage_gaps": gaps or [],
        "messages": messages,
        "labels": labels,
    }
    return Conversation.model_validate_json(json.dumps(record))


GAP_POSSIBLE = {
    "id": "c90.g1",
    "kind": "outage",
    "certainty": "possible",
    "at_risk_from": "2026-10-20T10:01:30Z",
    "at_risk_to": "2026-10-20T10:02:30Z",
}
GAP_DELAYED = {**GAP_POSSIBLE, "id": "c90.g2", "certainty": "delayed"}
CAVEAT_GAP = {
    "id": "c90.v1",
    "reason": "possible_gap",
    "gap_id": "c90.g1",
    "summary": "Messages may be missing around 10:02.",
}
CAVEAT_PRE = {
    "id": "c90.v2",
    "reason": "pre_connection",
    "summary": "History before 20 Oct is not covered.",
}

SURFACES_EVERYTHING = (
    "Встреча завтра в 15:00 ждёт ответа; просят отправить 250 минг сум. "
    "Вы обещали отправить отчёт juma kuni."
)


# ------------------------------------------------------------------ recall
def test_open_loops_are_the_open_questions_and_commitments_only() -> None:
    ids = [loop.id for loop in open_loops(conversation())]
    assert ids == ["c90.q1", "c90.q2", "c90.c1", "c90.c2"]  # not the answered q3


def test_values_match_across_scripts_and_apostrophes() -> None:
    coverage = open_loop_coverage(conversation(), SURFACES_EVERYTHING)
    # Cyrillic "250 минг" matches the Latin value; c2 has no values to match.
    assert coverage == {"c90.q1": True, "c90.q2": True, "c90.c1": True, "c90.c2": None}


def test_a_dropped_value_is_a_miss_and_partial_words_do_not_match() -> None:
    coverage = open_loop_coverage(conversation(), "Встреча в 15:00. Отчёт: juma.")
    assert coverage["c90.q2"] is False
    assert coverage["c90.c1"] is False  # "juma" alone is not "juma kuni"


def test_values_matcher_needs_every_value() -> None:
    both = (
        conversation()
        .labels.questions_to_owner[1]
        .model_copy(update={"values": ["250 ming", "15:00"]})
    )
    assert values_matcher(both, "250 ming") is False
    assert values_matcher(both, "250 ming va 15:00") is True


def test_recall_prescreen_counts_uncheckable_items_as_missed_and_the_gate_stays_open() -> None:
    coverage = open_loop_coverage(conversation(), SURFACES_EVERYTHING)
    metric = recall_metric(coverage, ReviewLedger.empty())
    assert metric.all_items == Measure(3, 4)
    assert metric.prescreen_status() is Status.FAIL  # 75 % < 95 %
    assert metric.confirmed == Measure(0, 0)
    assert metric.gate_status() is Status.NOT_EVALUABLE
    assert "1 open loop(s) have no `values`" in metric.note


def test_recall_gate_needs_the_owners_verdict_for_uncheckable_items() -> None:
    coverage = open_loop_coverage(conversation(), SURFACES_EVERYTHING)
    confirmed = ReviewLedger(frozenset({"c90"}), {}, {})
    open_gate = recall_metric(coverage, confirmed)
    assert open_gate.pending == 1
    assert open_gate.confirmed == Measure(3, 3)
    assert open_gate.gate_status() is Status.NOT_EVALUABLE

    ruled = ReviewLedger(frozenset({"c90"}), {}, {"c90.c2": "covered"})
    closed_gate = recall_metric(coverage, ruled)
    assert closed_gate.pending == 0
    assert closed_gate.confirmed == Measure(4, 4)
    assert closed_gate.gate_status() is Status.PASS


def test_recall_gate_fails_on_a_confirmed_miss_even_with_items_pending() -> None:
    coverage = open_loop_coverage(conversation(), "Встреча в 15:00.")
    metric = recall_metric(coverage, ReviewLedger(frozenset({"c90"}), {}, {}))
    assert metric.pending == 1
    assert metric.gate_status() is Status.FAIL


def test_a_judge_verdict_moves_the_prescreen_only() -> None:
    coverage = open_loop_coverage(conversation(), SURFACES_EVERYTHING)
    confirmed = ReviewLedger(frozenset({"c90"}), {}, {})
    metric = recall_metric(coverage, confirmed, judged={"c90.c2": True})
    assert metric.all_items == Measure(4, 4)
    assert metric.prescreen_status() is Status.PASS
    assert metric.gate_status() is Status.NOT_EVALUABLE  # still the owner's call
    assert metric.confirmed == Measure(3, 3)


def test_the_owner_can_rule_an_uncheckable_item_missed() -> None:
    coverage = open_loop_coverage(conversation(), SURFACES_EVERYTHING)
    ledger = ReviewLedger(frozenset({"c90"}), {}, {"c90.c2": "missed"})
    assert recall_metric(coverage, ledger).gate_status() is Status.FAIL  # 3/4 < 95 %


# ----------------------------------------------------------------- caveats
def full_caveat_conversation() -> Conversation:
    return conversation(
        gaps=[GAP_POSSIBLE, GAP_DELAYED],
        caveats=[CAVEAT_GAP, CAVEAT_PRE],
        coverage_started_at="2026-10-20T00:00:00Z",
    )


def output(**kwargs: Any) -> SummaryOutput:
    return SummaryOutput("c90", "text", "en", **kwargs)


def test_a_summary_with_every_expected_caveat_passes() -> None:
    check = check_caveats(
        full_caveat_conversation(),
        output(caveat_gap_ids=frozenset({"c90.g1"}), pre_connection_caveat=True),
    )
    assert (check.missing, check.spurious, check.expected) == ((), (), 2)


def test_a_missing_gap_caveat_and_a_missing_pre_connection_caveat_are_found() -> None:
    check = check_caveats(full_caveat_conversation(), output())
    assert check.missing == ("c90.v1", "c90.v2")


def test_a_caveat_for_a_delayed_gap_is_spurious() -> None:
    """A delayed gap lost nothing: saying messages may be missing is wrong."""
    check = check_caveats(
        full_caveat_conversation(),
        output(caveat_gap_ids=frozenset({"c90.g1", "c90.g2"}), pre_connection_caveat=True),
    )
    assert check.missing == ()
    assert check.spurious == ("gap:c90.g2",)


def test_a_pre_connection_note_without_a_coverage_start_is_spurious() -> None:
    check = check_caveats(conversation(), output(pre_connection_caveat=True))
    assert check.spurious == ("pre_connection",)
    assert check.expected == 0


def test_caveat_metrics_only_count_conversations_that_have_caveats() -> None:
    with_gaps = full_caveat_conversation()
    plain = conversation("c91")
    checks = [
        check_caveats(with_gaps, output(caveat_gap_ids=frozenset({"c90.g1"}))),
        check_caveats(plain, SummaryOutput("c91", "text", "en")),
    ]
    presence, spurious = caveat_metrics(checks, ReviewLedger.empty())
    assert presence.all_items == Measure(0, 1)  # c90 lacks its pre-connection caveat
    assert presence.failed_items == ("c90",)
    assert spurious.all_items == Measure(0, 2)
    assert spurious.prescreen_status() is Status.PASS


# ---------------------------------------------------------------- language
UZ_LATN = (
    "Dilnoza sizdan tortni kim olib kelishini soradi va kafe uchun 600 ming to'lov "
    "qilganini aytdi. Siz ertaga javob bering."
)
UZ_CYRL = (
    "Дилноза сиздан тортни ким олиб келишини сўради ва кафе учун 600 минг тўлов "
    "қилганини айтди. Сиз эртага жавоб беринг."
)
RU = "Дилноза спросила, кто привезёт торт, и сообщила, что оплатила кафе. Нужно ответить до завтра."
EN = (
    "Dilnoza asked who will bring the cake and said that she paid the cafe. "
    "You need to reply by tomorrow."
)


@pytest.mark.parametrize(
    ("text", "language"),
    [
        (UZ_LATN, "uz"),
        (UZ_CYRL, "uz"),
        (RU, "ru"),
        (EN, "en"),
        ("12345 :)", None),
        ("Rahmat", None),
    ],
)
def test_detect_language(text: str, language: str | None) -> None:
    assert detect_language(text) == language


@pytest.mark.parametrize(
    ("text", "expected"),
    [(UZ_LATN, "uz-Latn"), (UZ_CYRL, "uz-Cyrl"), (RU, "ru"), (EN, "en")],
)
def test_the_right_language_and_script_pass(text: str, expected: Any) -> None:
    assert check_language(text, expected).ok


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (UZ_LATN, "uz-Cyrl"),  # right language, wrong script
        (UZ_CYRL, "uz-Latn"),
        (RU, "uz-Cyrl"),  # right script, wrong language
        (UZ_LATN, "en"),
        (EN, "uz-Latn"),
        (RU, "en"),
        ("Rahmat", "uz-Latn"),  # too little evidence is a failure, never a pass
    ],
)
def test_the_wrong_language_or_script_fails(text: str, expected: Any) -> None:
    assert not check_language(text, expected).ok


def test_a_few_verbatim_latin_fragments_do_not_break_a_russian_summary() -> None:
    text = RU + " Адрес: Qorasuv-3."
    verdict = check_language(text, "ru")
    assert verdict.ok
    assert verdict.script_share > 0.9


def test_language_metric_is_all_or_nothing() -> None:
    verdicts = {"c01": check_language(RU, "ru"), "c02": check_language(RU, "en")}
    metric = language_metric(verdicts, ReviewLedger.empty())
    assert metric.all_items == Measure(1, 2)
    assert metric.failed_items == ("c02",)
    assert metric.prescreen_status() is Status.FAIL


# ------------------------------------------------------------- gate semantics
def test_no_owner_confirmed_items_means_not_evaluable_never_pass() -> None:
    metric = ratio_metric(
        key="k",
        label="k",
        outcomes={"c01": True, "c02": True},
        threshold=0.95,
        ledger=ReviewLedger.empty(),
    )
    assert metric.prescreen_status() is Status.PASS
    assert metric.gate_status() is Status.NOT_EVALUABLE


def test_the_gate_counts_only_confirmed_items() -> None:
    ledger = ReviewLedger(frozenset({"c01"}), {}, {})
    metric = ratio_metric(
        key="k",
        label="k",
        outcomes={"c01": True, "c02": False},
        threshold=0.95,
        ledger=ledger,
    )
    assert metric.prescreen_status() is Status.FAIL
    assert metric.confirmed == Measure(1, 1)
    assert metric.gate_status() is Status.PASS


def test_an_incomplete_run_never_passes_but_a_confirmed_miss_still_fails() -> None:
    ledger = ReviewLedger(frozenset({"c01", "c02"}), {}, {})
    good = ratio_metric(key="k", label="k", outcomes={"c01": True}, threshold=0.5, ledger=ledger)
    bad = ratio_metric(key="k", label="k", outcomes={"c02": False}, threshold=0.5, ledger=ledger)
    assert good.gate_status(complete=True) is Status.PASS
    assert good.gate_status(complete=False) is Status.NOT_EVALUABLE
    assert bad.gate_status(complete=False) is Status.FAIL


def test_a_count_metric_with_nothing_measured_is_not_a_pass() -> None:
    metric = count_metric(
        key="k",
        label="k",
        items=["c01"],
        offending=[],
        threshold=0,
        ledger=ReviewLedger.empty(),
    )
    assert metric.prescreen_status() is Status.PASS
    assert metric.gate_status() is Status.NOT_EVALUABLE


def test_a_metric_without_a_threshold_is_informational() -> None:
    metric = MetricResult(
        key="k",
        label="k",
        unit="ratio",
        goal="at_least",
        threshold=None,
        all_items=Measure(1, 2),
        confirmed=Measure(0, 0),
    )
    assert metric.prescreen_status() is Status.INFO
    assert metric.gate_status() is Status.INFO


# ------------------------------------------------------------------ ledger
def test_label_ids_are_confirmed_through_their_conversation() -> None:
    ledger = ReviewLedger(frozenset({"c05", "r07"}), {}, {})
    assert ledger.is_confirmed("c05")
    assert ledger.is_confirmed("c05.q1")
    assert ledger.is_confirmed("r07")
    assert not ledger.is_confirmed("c06.q1")
    assert not ledger.is_confirmed("c050")


def test_the_ledger_loads_from_json_and_refuses_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    path.write_text(
        json.dumps(
            {
                "confirmed_items": ["c01"],
                "findings": {"c01#aaaaaaaa": "dismissed"},
                "coverage": {"c01.q1": "covered"},
            }
        ),
        encoding="utf-8",
    )
    ledger = ReviewLedger.load(path)
    assert ledger.is_confirmed("c01.c3")
    assert ledger.finding_status("c01#aaaaaaaa") == "dismissed"
    assert ledger.finding_status("c01#bbbbbbbb") == "pending_owner"
    assert ledger.coverage_verdict("c01.q1") is True
    assert ledger.coverage_verdict("c01.q2") is None
    assert ledger.source == "review.json"

    path.write_text(json.dumps({"confirmed": ["c01"]}), encoding="utf-8")
    with pytest.raises(ValidationError):
        ReviewLedger.load(path)


def test_finding_ids_are_stable_and_ignore_case_and_padding() -> None:
    assert finding_id("c01", "  Meeting at 14:00 ") == finding_id("c01", "meeting at 14:00")
    assert finding_id("c01", "a") != finding_id("c02", "a")
