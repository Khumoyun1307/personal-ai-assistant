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
from evals.errors import LedgerError
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


def test_a_value_not_found_is_left_to_the_judge_and_partial_words_do_not_match() -> None:
    """E-s: a literal miss is not evidence of a miss (the summary may translate the
    value), so the loop is undecided (None), never a definite miss."""
    coverage = open_loop_coverage(conversation(), "Встреча в 15:00. Отчёт: juma.")
    assert coverage["c90.q1"] is True
    assert coverage["c90.q2"] is None
    assert coverage["c90.c1"] is None  # "juma" alone is not "juma kuni"


def test_values_matcher_needs_every_value() -> None:
    both = (
        conversation()
        .labels.questions_to_owner[1]
        .model_copy(update={"values": ["250 ming", "15:00"]})
    )
    assert values_matcher(both, "250 ming") is None
    assert values_matcher(both, "250 ming va 15:00") is True


def loop_with(values: list[str]) -> Any:
    return conversation().labels.questions_to_owner[1].model_copy(update={"values": values})


@pytest.mark.parametrize(
    ("value", "summary"),
    [
        # Observed in the 2026-10-26 Luna run: Uzbek inflects what it quotes.
        ("900 ming", "Cobalt avtomobilini 2 kunga 900 mingga ijaraga oladi."),
        ("15-oktabr", "15-oktabrdagi darsni ko‘chirish mumkinmi?"),
        ("narxlar ro'yxati", "Narxlar ro‘yxatini dushanbagacha tayyorlash."),
        ("1 million 150 ming", "1 million 150 mingni ustaga beradimi?"),
        ("shokolad", "Shokoladli tortni olib keladi."),
        ("30-октябр", "Vannaxona 30-oktyabrgacha bitadi."),
        ("16:30", "Notariusga 16:30da boradi."),
    ],
)
def test_an_inflected_value_is_found(value: str, summary: str) -> None:
    """The defect: these summaries carry the value, but the whole-word match
    counted every one of them as a miss."""
    assert values_matcher(loop_with([value]), summary) is True


@pytest.mark.parametrize(
    ("value", "summary"),
    [
        ("80%", "Hisobotning 800 qismi"),  # only letters may follow
        ("12-й", "12 yanvarda kalitni qoldiradi"),  # a one-letter last word matches whole
        ("narxlar ro'yxati", "narxlari ro‘yxatini"),  # only the last word inflects
        ("ming", "Toshkent"),
        # S03.6 review: only case/possessive endings, never a longer stem or a negation
        ("Ali", "Alisher ertaga keladi."),
        ("Olim", "Olimpiada natijalari chiqdi."),
        ("Jasur", "Jasurbek aka pulni oldi."),
        ("Aziz", "Aziza opa qo‘ng‘iroq qiladi."),
        ("bor", "U ertaga bormaydi."),
        ("kelish", "Narx bo‘yicha kelishmadi."),
        ("Karim", "Karimov hisobotni yubordi."),
    ],
)
def test_a_suffix_never_turns_another_value_into_a_match(value: str, summary: str) -> None:
    assert values_matcher(loop_with([value]), summary) is None


@pytest.mark.parametrize(
    ("value", "summary"),
    [
        ("3 nusxa", "3 nusxasini olib keladi."),  # possessive + accusative
        ("Ali", "Aliga aytib qo‘ying."),
        ("100 ming", "100 minglik chek."),
        ("Jasur", "Jasurning raqami."),
    ],
)
def test_case_and_possessive_endings_chain(value: str, summary: str) -> None:
    assert values_matcher(loop_with([value]), summary) is True


def test_the_owners_verdict_overrides_a_string_pass() -> None:
    """S03.6 review: a string pass (an inflected value, or a value surfaced in a
    sentence that says something else) never outranks the owner's word."""
    passed = values_matcher(loop_with(["900 ming"]), "900 mingga ijaraga oladi.")
    assert passed is True
    ledger = ReviewLedger(frozenset({"c90"}), {}, {"c90.c1": "missed"})
    metric = recall_metric({"c90.c1": passed}, ledger)
    assert metric.all_items == Measure(1, 1)  # the pre-screen keeps the string pass
    assert metric.confirmed == Measure(0, 1)  # the gate takes the owner's verdict
    assert metric.gate_status() is Status.FAIL


def test_no_verdict_turns_a_conversation_that_failed_into_a_pass() -> None:
    ledger = ReviewLedger(frozenset({"c90"}), {}, {"c90.c1": "covered"})
    assert recall_metric({"c90.c1": False}, ledger).confirmed == Measure(0, 1)


def test_a_translated_value_goes_to_the_judge_not_to_a_miss() -> None:
    """The defect: "до пятницы" written in an Uzbek summary as "juma kunigacha" was a
    definite miss that not even the owner could overrule."""
    coverage = recall_metric(
        {"c90.c1": values_matcher(loop_with(["до пятницы"]), "Juma kunigacha o‘tkazadi.")},
        ReviewLedger(frozenset({"c90"}), {}, {"c90.c1": "covered"}),
        judged={"c90.c1": True},
    )
    assert coverage.all_items == Measure(1, 1)  # the judge's pre-screen
    assert coverage.confirmed == Measure(1, 1)  # the owner's verdict decides the gate


def test_recall_prescreen_counts_uncheckable_items_as_missed_and_the_gate_stays_open() -> None:
    coverage = open_loop_coverage(conversation(), SURFACES_EVERYTHING)
    metric = recall_metric(coverage, ReviewLedger.empty())
    assert metric.all_items == Measure(3, 4)
    assert metric.prescreen_status() is Status.FAIL  # 75 % < 95 %
    assert metric.confirmed == Measure(0, 0)
    assert metric.gate_status() is Status.NOT_EVALUABLE
    assert "1 open loop(s) not matched on `values`" in metric.note


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
    # q2 and c1 are undecided by string (E-s); the owner rules q2 missed.
    ledger = ReviewLedger(frozenset({"c90"}), {}, {"c90.q2": "missed"})
    metric = recall_metric(coverage, ledger)
    assert metric.pending == 2
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


def test_a_tiny_confirmed_subset_is_not_a_gate() -> None:
    """1 of 1 confirmed items passing says nothing about the set: the gate needs
    every item of the set owner-confirmed (E-k)."""
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
    assert metric.gate_status() is Status.NOT_EVALUABLE
    assert metric.confirmation() == (1, 2)


def test_the_gate_passes_only_when_every_item_of_the_set_is_confirmed() -> None:
    ledger = ReviewLedger(frozenset({"c01", "c02"}), {}, {})
    metric = ratio_metric(
        key="k", label="k", outcomes={"c01": True, "c02": True}, threshold=0.95, ledger=ledger
    )
    assert metric.confirmation() == (2, 2)
    assert metric.gate_status() is Status.PASS


def test_a_confirmed_set_below_the_threshold_fails() -> None:
    ledger = ReviewLedger(frozenset({"c01", "c02"}), {}, {})
    metric = ratio_metric(
        key="k", label="k", outcomes={"c01": True, "c02": False}, threshold=0.95, ledger=ledger
    )
    assert metric.gate_status() is Status.FAIL


def test_enough_confirmed_misses_fail_the_gate_before_the_rest_is_reviewed() -> None:
    """With 10 items and a 95 % bar, two confirmed misses cannot be undone."""
    outcomes = {f"c{n:02d}": n > 2 for n in range(1, 11)}  # c01, c02 miss
    ledger = ReviewLedger(frozenset({"c01", "c02"}), {}, {})
    metric = ratio_metric(key="k", label="k", outcomes=outcomes, threshold=0.95, ledger=ledger)
    assert metric.gate_status() is Status.FAIL
    one_miss = ReviewLedger(frozenset({"c01"}), {}, {})
    # 9/10 = 90 % < 95 %: one confirmed miss already makes the bar unreachable.
    assert (
        ratio_metric(
            key="k", label="k", outcomes=outcomes, threshold=0.95, ledger=one_miss
        ).gate_status()
        is Status.FAIL
    )
    lenient = ratio_metric(key="k", label="k", outcomes=outcomes, threshold=0.8, ledger=one_miss)
    assert lenient.gate_status() is Status.NOT_EVALUABLE  # 9/10 could still reach 80 %


def test_a_run_over_part_of_the_set_never_passes_even_fully_confirmed() -> None:
    """A trial or stopped run measured 3 of 38 items: the set is 38."""
    ledger = ReviewLedger(frozenset({"r01", "r02", "r03"}), {}, {})
    metric = ratio_metric(
        key="k",
        label="k",
        outcomes={"r01": True, "r02": True, "r03": True},
        threshold=0.85,
        ledger=ledger,
        set_size=38,
    )
    assert metric.confirmed == Measure(3, 3)
    assert metric.gate_status() is Status.NOT_EVALUABLE
    assert metric.confirmation() == (3, 38)


def test_a_metric_whose_gate_is_defined_elsewhere_is_informational_at_the_gate() -> None:
    """The retrieval gate is defined with model-generated variants; the
    deterministic baseline compares against 85 % only as a pre-screen."""
    ledger = ReviewLedger(frozenset({"r01"}), {}, {})
    metric = ratio_metric(
        key="k",
        label="k",
        outcomes={"r01": True},
        threshold=0.85,
        ledger=ledger,
        gated=False,
    )
    assert metric.prescreen_status() is Status.PASS
    assert metric.gate_status() is Status.INFO


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
    with pytest.raises(LedgerError):
        ReviewLedger.load(path)


@pytest.mark.parametrize(
    "body",
    ["{not json", "[]", '{"confirmed": []}', '{"findings": {"c01#aaaaaaaa": "maybe"}}', ""],
)
def test_a_malformed_ledger_is_a_clean_error_not_a_traceback(tmp_path: Path, body: str) -> None:
    path = tmp_path / "review.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(LedgerError, match=r"review\.json"):
        ReviewLedger.load(path)


def test_a_missing_ledger_file_is_a_clean_error(tmp_path: Path) -> None:
    with pytest.raises(LedgerError, match="cannot be read"):
        ReviewLedger.load(tmp_path / "absent.json")


def test_the_owner_can_record_a_fabrication_the_judge_missed(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    path.write_text(json.dumps({"confirmed_items": ["c01"], "fabrications": ["c01"]}), "utf-8")
    ledger = ReviewLedger.load(path)
    assert ledger.owner_fabrications == frozenset({"c01"})


def test_finding_ids_are_stable_and_ignore_case_and_padding() -> None:
    assert finding_id("c01", "  Meeting at 14:00 ") == finding_id("c01", "meeting at 14:00")
    assert finding_id("c01", "a") != finding_id("c02", "a")


def test_an_apostrophe_does_not_split_an_uzbek_word_into_english() -> None:
    """E-v, the defect: "to‘lov" was read as the English word "to", so a clean
    Uzbek Latin summary (2026-10-26, c16) was detected as English and failed."""
    text = (
        "Ombor ijarasi va elektr uchun to‘lov amalga oshgan.\n"
        "To‘lov 7-oktyabrgacha so‘ralgan, kontakt to‘lovi kelganini aytgan.\n"
        "O‘t o‘chirgichni tekshirib ko‘rish."
    )
    assert detect_language(text) == "uz"
    assert check_language(text, "uz-Latn").ok
