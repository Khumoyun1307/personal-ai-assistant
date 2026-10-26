"""S03.4: the LLM-judge pre-screens and what a model may see of E1, on FakeLLM.

The judge only pre-screens: its fabrication flags start as pending owner
confirmation, and the gate counts only flags the owner confirmed
(IMPLEMENTATION_PLAN §10.1).
"""

from __future__ import annotations

from evals import corpus, render
from evals.judge import (
    FabricationFinding,
    LLMFabricationJudge,
    LLMOpenLoopJudge,
    LoopVerdict,
    ScreenResult,
    fabrication_metric,
)
from evals.metrics import SummaryOutput
from evals.render import render_conversation
from evals.review import ReviewLedger, finding_id
from evals.schema import load_e1
from evals.targets import Measure, Status

from assistant.llm.port import LLMError, LLMErrorKind, Provenance, StopReason
from tests.fakes.llm import FakeLLM
from tests.unit.test_eval_metrics import CAVEAT_GAP, GAP_DELAYED, GAP_POSSIBLE, conversation

OUTPUT = SummaryOutput("c90", "Meeting at 14:00 tomorrow.", "en")


# ------------------------------------------------------ what a model may see
def test_a_model_never_sees_labels_pre_edit_or_purged_text() -> None:
    checked = 0
    for conv in load_e1():
        rendered = render_conversation(conv)
        for message in conv.messages:
            for hidden in (message.original_text, message.purged_text):
                if hidden:
                    checked += 1
                    assert hidden not in rendered, message.id
        labels = conv.labels
        hidden_labels = [q.summary for q in labels.questions_to_owner]
        hidden_labels += [c.what for c in labels.commitments]
        hidden_labels += [t.statement for t in labels.must_not_state]
        hidden_labels += [e.summary for e in labels.expected_caveats]
        assert not any(label in rendered for label in hidden_labels), conv.id
    assert checked >= 5  # the set does contain edits and deletions


def test_the_rendering_shows_current_text_markers_and_coverage_facts() -> None:
    conv = conversation(gaps=[GAP_POSSIBLE, GAP_DELAYED], caveats=[CAVEAT_GAP])
    rendered = render_conversation(conv)
    assert "[c90.m01]" in rendered
    assert "Coverage gap c90.g1 (possible, outage)" in rendered
    assert "Coverage gap c90.g2 (delayed, outage)" in rendered
    assert "c90.q1" not in rendered  # label ids are not shown


def test_the_rendering_shows_times_in_the_owners_timezone() -> None:
    """E-w, the defect: judges read UTC while the summary is written from local
    times, so a correct "15:01" (10:01 UTC in Tashkent) read as a contradiction."""
    rendered = render_conversation(conversation())
    assert "[c90.m01] 2026-10-20 15:01 Dilnoza (contact)" in rendered
    assert "UTC" not in rendered
    assert "Asia/Tashkent" in rendered
    assert render.OWNER_TIMEZONE == corpus.OWNER_TIMEZONE
    assert render.RENDER_VERSION == "render.v2"  # v1 showed UTC


# -------------------------------------------------------- fabrication judge
def judge(llm: FakeLLM, ledger: ReviewLedger | None = None) -> LLMFabricationJudge:
    return LLMFabricationJudge(llm, model="gpt-6-luna", ledger=ledger or ReviewLedger.empty())


async def test_flags_become_pending_findings_with_validated_evidence() -> None:
    llm = FakeLLM()
    llm.parsed = [
        {
            "flags": [
                {
                    "statement": "Meeting at 14:00",
                    "reason": "superseded_value",
                    "evidence_message_ids": ["c90.m01", "c90.m99"],
                }
            ]
        }
    ]
    result = await judge(llm).screen(conversation(), OUTPUT)

    assert result.screened
    (finding,) = result.findings
    assert finding.status == "pending_owner"
    assert finding.finding_id == finding_id("c90", "Meeting at 14:00")
    assert finding.evidence_message_ids == ("c90.m01",)  # the invented id is dropped


async def test_the_judge_request_carries_the_summary_and_traps_but_no_tools() -> None:
    llm = FakeLLM()
    llm.parsed = [{"flags": []}]
    await judge(llm).screen(conversation(), OUTPUT)

    (request,) = llm.requests
    assert request.purpose == "eval_fabrication_screen"
    assert request.tools == ()
    (item,) = request.items
    assert item.provenance is Provenance.MANAGED
    assert "Meeting at 14:00 tomorrow." in item.text  # type: ignore[union-attr]
    assert "Known traps" in item.text  # type: ignore[union-attr]


async def test_the_owners_verdict_on_a_flag_is_applied() -> None:
    fid = finding_id("c90", "Meeting at 14:00")
    llm = FakeLLM()
    llm.parsed = [
        {
            "flags": [
                {
                    "statement": "Meeting at 14:00",
                    "reason": "unsupported",
                    "evidence_message_ids": [],
                }
            ]
        }
    ]
    ledger = ReviewLedger(frozenset(), {fid: "dismissed"}, {})
    result = await judge(llm, ledger).screen(conversation(), OUTPUT)
    assert result.findings[0].status == "dismissed"


async def test_a_judge_failure_is_unscreened_never_clean() -> None:
    errors = FakeLLM()
    errors.parsed = [LLMError(LLMErrorKind.UNAVAILABLE)]
    result = await judge(errors).screen(conversation(), OUTPUT)
    assert not result.screened
    assert result.error == "unavailable"


def finding(
    conversation_id: str, statement: str, status: str = "pending_owner"
) -> FabricationFinding:
    return FabricationFinding(
        finding_id(conversation_id, statement),
        conversation_id,
        statement,
        "unsupported",
        (),
        status,  # type: ignore[arg-type]
    )


def test_pending_flags_keep_the_fabrication_gate_open() -> None:
    results = [
        ScreenResult("c01", (finding("c01", "a"),)),
        ScreenResult("c02", ()),
    ]
    metric = fabrication_metric(results, ReviewLedger(frozenset({"c01", "c02"}), {}, {}))
    assert metric.all_items == Measure(1, 2)
    assert metric.pending == 1
    assert metric.gate_status() is Status.NOT_EVALUABLE
    assert "pending owner confirmation" in metric.note


def test_no_flags_but_no_confirmed_conversations_is_not_a_pass() -> None:
    results = [ScreenResult("c01", ()), ScreenResult("c02", ())]
    metric = fabrication_metric(results, ReviewLedger.empty())
    assert metric.prescreen_status() is Status.PASS
    assert metric.gate_status() is Status.NOT_EVALUABLE


def test_no_flags_over_confirmed_conversations_passes_only_when_all_are_confirmed() -> None:
    results = [ScreenResult("c01", ()), ScreenResult("c02", ())]
    partly = fabrication_metric(results, ReviewLedger(frozenset({"c01"}), {}, {}))
    assert partly.confirmed == Measure(0, 1)
    assert partly.gate_status() is Status.NOT_EVALUABLE  # 1 of 2 confirmed
    fully = fabrication_metric(results, ReviewLedger(frozenset({"c01", "c02"}), {}, {}))
    assert fully.confirmation() == (2, 2)
    assert fully.gate_status() is Status.PASS


def test_a_judge_negative_decides_unless_the_owner_records_a_fabrication() -> None:
    """The judge can only flag what it sees: a conversation it did not flag counts
    as clean. The owner overrides that in the ledger (`fabrications`)."""
    results = [ScreenResult("c01", ())]
    clean = fabrication_metric(results, ReviewLedger(frozenset({"c01"}), {}, {}))
    assert clean.gate_status() is Status.PASS
    ledger = ReviewLedger(frozenset({"c01"}), {}, {}, None, frozenset({"c01"}))
    found = fabrication_metric(results, ledger)
    assert found.confirmed == Measure(1, 1)
    assert found.all_items == Measure(1, 1)
    assert found.gate_status() is Status.FAIL


def test_an_owner_confirmed_fabrication_fails_the_gate_even_unreviewed_elsewhere() -> None:
    flagged = finding("c01", "a", "confirmed")
    metric = fabrication_metric([ScreenResult("c01", (flagged,))], ReviewLedger.empty())
    assert metric.confirmed == Measure(1, 1)
    assert metric.gate_status() is Status.FAIL


def test_a_dismissed_flag_is_not_a_fabrication() -> None:
    dismissed = finding("c01", "a", "dismissed")
    metric = fabrication_metric(
        [ScreenResult("c01", (dismissed,))], ReviewLedger(frozenset({"c01"}), {}, {})
    )
    assert metric.all_items == Measure(0, 1)
    assert metric.gate_status() is Status.PASS


def test_an_unscreened_conversation_leaves_the_gate_open() -> None:
    results = [ScreenResult("c01", (), screened=False, error="max_tokens")]
    metric = fabrication_metric(results, ReviewLedger(frozenset({"c01"}), {}, {}))
    assert metric.pending == 1
    assert "not screened" in metric.note
    assert metric.gate_status() is Status.NOT_EVALUABLE


# --------------------------------------------------------- open-loop judge
async def test_the_loop_judge_asks_only_about_loops_without_values() -> None:
    llm = FakeLLM()
    llm.parsed = [
        {
            "items": [
                {"id": "c90.c2", "evidence": "Dilnoza manzilni aytadi.", "surfaced": True},
                {"id": "c90.q1", "evidence": "", "surfaced": True},
            ]
        }
    ]
    verdicts = await LLMOpenLoopJudge(llm, model="gpt-6-luna").judge(conversation(), OUTPUT)

    assert verdicts == {"c90.c2": True}  # q1 was not asked about: ignored
    (request,) = llm.requests
    text = request.items[0].text  # type: ignore[union-attr]
    assert "c90.c2" in text
    assert "c90.q1" not in text  # checkable by values: not sent
    assert "Dilnoza" in text  # the commitment's description
    # v3: the message the loop cites, and no other message of the chat.
    assert "[contact] Manzil: Qorasuv-3, 17-uy" in text
    assert "Rahmat" not in text


async def test_the_loop_judge_is_asked_about_values_the_string_match_did_not_find() -> None:
    """E-s: a loop whose values were not found literally goes to the judge, with its
    values, which it must find in the summary in any language or format."""
    llm = FakeLLM()
    llm.parsed = [
        {
            "items": [
                {"id": "c90.q2", "evidence": "250 ming so'm?", "surfaced": True},
                {"id": "c90.c2", "evidence": "", "surfaced": False},
            ]
        }
    ]
    verdicts = await LLMOpenLoopJudge(llm, model="gpt-6-luna").judge(
        conversation(), OUTPUT, ["c90.q2", "c90.c2"]
    )

    assert verdicts == {"c90.q2": True, "c90.c2": False}
    (request,) = llm.requests
    text = request.items[0].text  # type: ignore[union-attr]
    assert "c90.q2" in text
    assert "[key values: 250 ming]" in text
    assert "c90.q1" not in text
    assert "c90.c1" not in text
    assert "same meaning, in any language, script or format" in request.instructions


async def test_the_loop_judge_quotes_its_evidence_before_the_verdict() -> None:
    """v3: the judge copies the summary line that surfaces a loop (kept in the
    report for the owner's review); its schema puts the evidence first."""
    llm = FakeLLM()
    llm.parsed = [{"items": [{"id": "c90.c2", "evidence": "Manzilni aytadi.", "surfaced": True}]}]
    verdicts = await LLMOpenLoopJudge(llm, model="gpt-6-luna").verdicts(conversation(), OUTPUT)
    assert verdicts["c90.c2"].evidence == "Manzilni aytadi."
    assert list(LoopVerdict.model_json_schema()["properties"]) == ["id", "evidence", "surfaced"]


async def test_the_loop_judge_makes_no_call_when_every_loop_is_checkable() -> None:
    conv = conversation()
    conv = conv.model_copy(
        update={
            "labels": conv.labels.model_copy(
                update={"commitments": [c for c in conv.labels.commitments if c.id != "c90.c2"]}
            )
        }
    )
    llm = FakeLLM()
    assert await LLMOpenLoopJudge(llm, model="gpt-6-luna").judge(conv, OUTPUT) == {}
    assert llm.requests == []


async def test_a_loop_judge_failure_gives_no_verdicts_so_loops_stay_missed() -> None:
    llm = FakeLLM()
    llm.parsed = [LLMError(LLMErrorKind.RATE_LIMITED)]
    assert await LLMOpenLoopJudge(llm, model="gpt-6-luna").judge(conversation(), OUTPUT) == {}


async def test_a_truncated_loop_verdict_is_ignored() -> None:
    class Truncating(FakeLLM):
        async def parse(self, request, schema):  # type: ignore[no-untyped-def, override]
            from assistant.llm.port import ParsedResponse, Usage

            return ParsedResponse(StopReason.MAX_TOKENS, "gpt-6-luna", Usage(1, 1))

    verdicts = await LLMOpenLoopJudge(Truncating(), model="gpt-6-luna").judge(
        conversation(), OUTPUT
    )
    assert verdicts == {}
