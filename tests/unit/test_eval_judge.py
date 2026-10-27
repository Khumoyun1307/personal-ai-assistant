"""S03.4: the LLM-judge pre-screens and what a model may see of E1, on FakeLLM.

The judge only pre-screens: its fabrication flags start as pending owner
confirmation, and the gate counts only flags the owner confirmed
(IMPLEMENTATION_PLAN §10.1).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from evals import corpus, render
from evals import judge as judge_module
from evals.assets import load_asset
from evals.judge import (
    FabricationFinding,
    LLMFabricationJudge,
    LLMOpenLoopJudge,
    LoopVerdict,
    QuoteMatch,
    ScreenResult,
    evidence_in_summary,
    fabrication_metric,
    quote_match,
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
#: A summary holding the lines the loop judge quotes in these tests (its quotes are
#: checked against the summary, S03.7 A2).
LOOP_OUTPUT = SummaryOutput(
    "c90",
    "Overview: Meeting at 14:00 tomorrow.\n"
    "Commitments not yet done:\n"
    "- Dilnoza promised you: Dilnoza manzilni aytadi.\n"
    "Key facts:\n"
    "- To‘lov 250 ming so'm?",
    "en",
)


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
    # v1 showed UTC; v2 local times; v3 (S03.7 A2) when an edited message was edited.
    assert render.RENDER_VERSION == "render.v3"


def test_the_rendering_says_when_an_edited_message_was_edited() -> None:
    """c36 (S03.7 A2): the judge reads what the summarizer reads: an edited
    message's edit time and that its text is the current one."""
    checked = 0
    for conv in load_e1():
        rendered = render_conversation(conv)
        for message in conv.messages:
            if message.edited_at is None or message.deleted:
                continue
            checked += 1
            local = message.edited_at.astimezone(ZoneInfo(render.OWNER_TIMEZONE))
            edited = local.strftime("%Y-%m-%d %H:%M")
            assert f"(edited at {edited}; this is its current text): {message.text}" in rendered
    assert checked >= 1


# -------------------------------------------------------- fabrication judge
async def test_the_judges_are_told_that_you_in_the_summary_is_the_owner() -> None:
    """S03.7 A2: 3 of 11 over-flags in the v5 run were correct "You promised ..."
    lines; the summary addresses the owner, whom the chat marks "(owner)"."""
    assert (judge_module.RUBRIC_ID, judge_module.LOOPS_RUBRIC_ID) == (
        "fabrication.v2",
        "open_loops.v4",
    )
    for older in ("fabrication.v1", "open_loops.v3"):  # reports name them: kept
        assert load_asset("rubrics", older).text
    llm = FakeLLM()
    llm.parsed = [{"flags": []}, {"items": []}]
    await judge(llm).screen(conversation(), OUTPUT)
    await LLMOpenLoopJudge(llm, model="gpt-6-luna").verdicts(conversation(), LOOP_OUTPUT)
    fabrication, loops = llm.requests
    for request in (fabrication, loops):
        assert '"you"' in request.instructions
        assert "is the owner" in request.instructions
    assert "(owner)" in fabrication.instructions


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
    verdicts = await LLMOpenLoopJudge(llm, model="gpt-6-luna").judge(conversation(), LOOP_OUTPUT)

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
                {"id": "c90.q2", "evidence": "To'lov 250  ming so'm?", "surfaced": True},
                {"id": "c90.c2", "evidence": "", "surfaced": False},
            ]
        }
    ]
    verdicts = await LLMOpenLoopJudge(llm, model="gpt-6-luna").judge(
        conversation(), LOOP_OUTPUT, ["c90.q2", "c90.c2"]
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
    verdicts = await LLMOpenLoopJudge(llm, model="gpt-6-luna").verdicts(conversation(), LOOP_OUTPUT)
    assert verdicts["c90.c2"].evidence == "Manzilni aytadi."
    assert verdicts["c90.c2"].surfaced
    assert list(LoopVerdict.model_json_schema()["properties"]) == ["id", "evidence", "surfaced"]


async def test_a_loop_verdict_quoting_a_line_not_in_the_summary_is_not_surfaced() -> None:
    """S03.7 A2 (c05.c3 in the v5 run): the judge quoted a line the summary does
    not contain. Its quote is checked in code; a quote that does not occur is no
    evidence, so the loop is not surfaced in the pre-screen, and the report says so."""
    llm = FakeLLM()
    llm.parsed = [
        {
            "items": [
                {"id": "c90.c2", "evidence": "Siz va'dangizni bajarmadingiz", "surfaced": True},
                {"id": "c90.q2", "evidence": "", "surfaced": True},
            ]
        }
    ]
    verdicts = await LLMOpenLoopJudge(llm, model="gpt-6-luna").verdicts(
        conversation(), LOOP_OUTPUT, ["c90.c2", "c90.q2"]
    )
    for loop in ("c90.c2", "c90.q2"):
        assert not verdicts[loop].surfaced
        assert verdicts[loop].judge_surfaced
        assert not verdicts[loop].quoted
    assert verdicts["c90.c2"].evidence == "Siz va'dangizni bajarmadingiz"


def test_a_quote_counts_when_every_quoted_line_occurs_in_the_summary() -> None:
    summary = LOOP_OUTPUT.text
    # Line breaks, bullets, spacing, case and apostrophe forms may differ.
    assert evidence_in_summary(
        "- Dilnoza promised you: Dilnoza manzilni aytadi.\nTo'lov 250 ming so’m?", summary
    )
    # An ellipsis may join two parts of one line.
    assert evidence_in_summary("Dilnoza promised you: ... manzilni aytadi.", summary)
    assert not evidence_in_summary("Dilnoza manzilni aytadi.\nUchrashuv bekor", summary)
    assert not evidence_in_summary("", summary)
    assert not evidence_in_summary(" ... ", summary)


#: Summary lines of the summarize.v6 E1 run (synthetic chats) and the judge's copies
#: of them that the exact check rejected although each line is there (E-af round 3).
_V6_LINES = (
    "- You promised Мавлуда хола: 20 ta stulni mahalladan so‘rab, kechqurun javob berish "
    "(due: 2026-10-10 kechqurun)\n"
    "- You promised Гавҳар Раҳимовна (синф раҳбари): Ota-onalar majlisida qatnashish "
    "(due: 2026-10-16 soat 18:00)\n"
    "- You promised Гавҳар Раҳимовна (синф раҳбари): Majlisdan keyin 15 daqiqa qolish "
    "(due: 2026-10-16, majlisdan keyin)\n"
    "- Гавҳар Раҳимовна (синф раҳбари) promised you: Faylni qayta yuborish "
    "(due: 2026-10-12 kechqurun)\n"
    "- Laylo asks you to (not agreed yet): Yakshanba kuni soat 5:00 ga o‘quv xonasini band "
    "qilish (due: yakshanba kuni soat 5:00)\n"
    "- You promised Timur: Palatk a olib kelish"
)
_V6_SLIPS = {
    # A Cyrillic "ули" copied into a Latin word.
    "c04.c2": "You promised Мавлуда хола: 20 ta stулни mahalladan so‘rab, kechqurun javob "  # noqa: RUF001
    "berish (due: 2026-10-10 kechqurun)",
    # Latin "ari" copied into a Cyrillic word, on both quoted lines.
    "c18.c1": "- You promised Гавҳар Раҳимовна (синф раҳбari): Ota-onalar majlisida "  # noqa: RUF001
    "qatnashish (due: 2026-10-16 soat 18:00)\n- You promised Гавҳар Раҳимовна (синф "
    "раҳбari): Majlisdan keyin 15 daqiqa qolish (due: 2026-10-16, majlisdan keyin)",  # noqa: RUF001
    "c18.c3": "- Гавҳар Раҳимовна (синф раҳбari) promised you: Faylni qayta yuborish "  # noqa: RUF001
    "(due: 2026-10-12 kechqurun)",
    # "(not agreed yet)" left out.
    "c28.q2": "- Laylo asks you to: Yakshanba kuni soat 5:00 ga o‘quv xonasini band qilish "
    "(due: yakshanba kuni soat 5:00)",
}


def test_a_quote_copied_with_a_slip_counts_as_a_near_quote() -> None:
    """E-af round 3: in the summarize.v6 run the exact check rejected four correct
    verdicts whose quotes mixed Latin and Cyrillic letters or dropped a parenthesis.
    A quoted line at least 0.90 similar to a summary line counts, as a near quote
    (listed apart so the owner sees it)."""
    for loop, quote in _V6_SLIPS.items():
        assert quote_match(quote, _V6_LINES) is QuoteMatch.NEAR, loop
        assert evidence_in_summary(quote, _V6_LINES), loop
    exact = "You promised Мавлуда хола: 20 ta stulni mahalladan so‘rab"
    assert quote_match(exact, _V6_LINES) is QuoteMatch.EXACT


def test_an_invented_or_empty_quote_is_still_no_evidence() -> None:
    """The v5 run's invented line (c05.c3: an item text under a made-up "you did not
    keep your promise" lead-in) is about 0.55 similar to its closest line."""
    invented = "Siz va’dangizni bajarmadingiz: Palatk a olib kelish"
    assert quote_match(invented, _V6_LINES) is QuoteMatch.NONE
    assert not evidence_in_summary(invented, _V6_LINES)
    assert quote_match("", _V6_LINES) is QuoteMatch.NONE
    assert quote_match(" ... ", _V6_LINES) is QuoteMatch.NONE


def test_a_near_quote_must_carry_the_lines_numbers() -> None:
    """Similarity alone would accept a copy with another time or amount (one digit
    in a long line); a near quote must have exactly the line's numbers."""
    wrong_time = _V6_SLIPS["c28.q2"].replace("5:00", "6:00")
    assert quote_match(wrong_time, _V6_LINES) is QuoteMatch.NONE
    wrong_count = _V6_SLIPS["c04.c2"].replace("20 ta", "25 ta")
    assert quote_match(wrong_count, _V6_LINES) is QuoteMatch.NONE


async def test_a_loop_verdict_with_a_near_quote_is_surfaced_and_marked_near() -> None:
    llm = FakeLLM()
    llm.parsed = [
        {
            "items": [
                # "Dilnoza" with a Cyrillic "о": the same line, copied with a slip.  # noqa: RUF003
                {
                    "id": "c90.c2",
                    "evidence": "- Dilnoza promised you: Dilnоza manzilni aytadi.",  # noqa: RUF001
                    "surfaced": True,
                },
                {"id": "c90.q2", "evidence": "To‘lov 250 ming so'm?", "surfaced": True},
            ]
        }
    ]
    verdicts = await LLMOpenLoopJudge(llm, model="gpt-6-luna").verdicts(
        conversation(), LOOP_OUTPUT, ["c90.c2", "c90.q2"]
    )
    near, exact = verdicts["c90.c2"], verdicts["c90.q2"]
    assert near.surfaced
    assert near.quoted
    assert near.near
    assert exact.surfaced
    assert exact.quoted
    assert not exact.near


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
