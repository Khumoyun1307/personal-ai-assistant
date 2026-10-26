"""S03.4b: the summary target's pure parts, without a database or a model.

What runs against the real summarize path and a database is in
`tests/integration/test_eval_summary.py`. Here: the catch-up window, how a
summary is rendered for the checks and the judges, what the coverage renderer's
output means for the caveat metric, the pre-run estimate (every call, retries
included) and the set sizes that keep a trial from ever being a gate.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from evals.metrics import (
    CaveatCheck,
    LanguageVerdict,
    caveat_metrics,
    language_metric,
    recall_metric,
)
from evals.review import ReviewLedger, coverage_key
from evals.schema import Conversation, load_e1
from evals.summary import (
    FABRICATION_PURPOSE,
    LOOPS_PURPOSE,
    MAX_SUMMARY_CHARS,
    SUMMARIZE_PURPOSE,
    WINDOW_LEAD,
    SummaryTarget,
    carried_caveats,
    catch_up_window,
    evaluation_instant,
    gap_key,
    labelled_text,
    plain_text,
)
from evals.targets import RunContext, Status

from assistant.agent import prompt_registry
from assistant.agent.schemas import (
    MAX_DUE,
    MAX_ITEM_TEXT,
    MAX_ITEMS,
    MAX_OVERVIEW,
    MAX_REASON,
    Commitment,
    ConversationSummaryOutput,
    SummaryPoint,
    sanitize,
)
from assistant.agent.summarize import SummarizeConfig
from assistant.memory.catch_up import GapFact, PeriodCoverage

E1 = load_e1()
BY_ID = {c.id: c for c in E1}
T0 = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
TZ = "Asia/Tashkent"


def coverage(**kwargs: Any) -> PeriodCoverage:
    base: dict[str, Any] = {
        "start": T0,
        "end": T0 + timedelta(days=3),
        "coverage_began": T0 - timedelta(days=1),
        "single_conversation": True,
    }
    return PeriodCoverage(**{**base, **kwargs})


def gap(certainty: str, kind: str = "outage", chat: int | None = 7) -> GapFact:
    return GapFact(kind, certainty, T0 + timedelta(hours=2), T0 + timedelta(hours=9), chat)


# --------------------------------------------------------------------- the window
def test_the_window_starts_before_everything_the_chat_mentions() -> None:
    for conversation in E1:
        start, end = catch_up_window(conversation)
        moments = [m.sent_at for m in conversation.messages]
        moments += [g.at_risk_from for g in conversation.coverage_gaps]
        if conversation.coverage_started_at is not None:
            moments.append(conversation.coverage_started_at)
        assert start == min(moments) - WINDOW_LEAD
        assert end == evaluation_instant(conversation) > conversation.messages[-1].sent_at


def test_a_chat_that_refers_to_history_before_coverage_gets_a_window_that_reaches_before_it() -> (
    None
):
    c20 = BY_ID["c20"]
    assert c20.coverage_started_at is not None
    assert catch_up_window(c20)[0] < c20.coverage_started_at


# ---------------------------------------------------------------------- rendering
SUMMARY: dict[str, Any] = {
    "overview": "Bu suhbat haqida",
    "open_questions_to_owner": [{"text": "Qaysi manzil?", "message_ids": [1]}],
    "commitments": [
        {"by": "owner", "text": "Hujjatni yuboraman", "due": "juma 10:00", "message_ids": [2]},
        {"by": "contact", "text": "Pulni beradi", "due": None, "message_ids": [3]},
    ],
    "decisions_requested": [{"text": "Soat nechada?", "message_ids": [4]}],
    "key_facts": [{"text": "Manzil: Qorasuv 3", "message_ids": [5]}],
    "needs_reply": True,
    "needs_reply_reason": "Savol berildi",
    "language": "uz",
    "items_omitted": 0,
}


def test_the_plain_text_is_the_summarys_own_words_and_nothing_else() -> None:
    text = plain_text(SUMMARY)
    for words in (
        "Bu suhbat haqida",
        "Qaysi manzil?",
        "Hujjatni yuboraman juma 10:00",
        "Pulni beradi",
    ):
        assert words in text
    assert "Qorasuv 3" in text
    assert "Savol berildi" in text
    # No label, no id, no flag: the language and value checks read only the model's words.
    for foreign in ("promised", "Overview", "yes", "owner", "uz", "[1]"):
        assert foreign not in text


def test_the_judges_text_names_who_promised_what() -> None:
    text = labelled_text(SUMMARY)
    assert "promised by the owner: Hujjatni yuboraman (due: juma 10:00)" in text
    assert "promised by the contact: Pulni beradi" in text
    assert "Needs a reply from the owner: yes - Savol berildi" in text
    assert labelled_text({**SUMMARY, "commitments": []}).count("(none)") == 1


def test_the_judges_are_budgeted_for_the_largest_summary_sanitize_can_store() -> None:
    biggest = ConversationSummaryOutput(
        overview="o" * (MAX_OVERVIEW + 100),
        open_questions_to_owner=[
            SummaryPoint(text="q" * (MAX_ITEM_TEXT + 50), message_ids=[1]) for _ in range(20)
        ],
        commitments=[
            Commitment(
                by="owner",
                text="c" * (MAX_ITEM_TEXT + 50),
                due="d" * (MAX_DUE + 50),
                message_ids=[1],
            )
            for _ in range(20)
        ],
        decisions_requested=[
            SummaryPoint(text="d" * (MAX_ITEM_TEXT + 50), message_ids=[1]) for _ in range(20)
        ],
        key_facts=[
            SummaryPoint(text="f" * (MAX_ITEM_TEXT + 50), message_ids=[1]) for _ in range(20)
        ],
        needs_reply=True,
        needs_reply_reason="r" * (MAX_REASON + 50),
        language="uz",
    )
    stored, _, capped = sanitize(biggest, frozenset({1}))
    assert capped == 4 * 5  # the lists really were at their caps
    assert len(labelled_text(stored)) <= MAX_SUMMARY_CHARS
    assert len(stored["open_questions_to_owner"]) == MAX_ITEMS


# ----------------------------------------------------------------------- caveats
def test_possible_and_definite_gaps_are_carried_by_their_dataset_id() -> None:
    possible, definite = gap("possible"), gap("definite", kind="dead_letter")
    ids = {gap_key(possible): "c99.g1", gap_key(definite): "c99.g2"}
    facts = carried_caveats(coverage(gaps=(possible, definite)), ids, TZ)
    assert facts.gap_ids == {"c99.g1", "c99.g2"}
    assert facts.lines == 2
    assert not facts.pre_connection


def test_a_delayed_gap_is_never_carried_even_if_the_renderer_is_handed_it() -> None:
    delayed = gap("delayed")
    facts = carried_caveats(coverage(gaps=(delayed,)), {gap_key(delayed): "c99.g1"}, TZ)
    assert facts.gap_ids == frozenset()
    assert facts.lines == 0


def test_a_gap_the_dataset_does_not_know_is_carried_under_an_unknown_id() -> None:
    facts = carried_caveats(coverage(gaps=(gap("possible"),)), {}, TZ)
    assert facts.gap_ids == {"unknown-gap-1"}  # the metric counts it as spurious


def test_the_coverage_start_note_is_carried_only_when_the_range_reaches_before_it() -> None:
    before = coverage(coverage_began=T0 + timedelta(hours=1))
    assert carried_caveats(before, {}, TZ).pre_connection
    assert not carried_caveats(coverage(), {}, TZ).pre_connection


def test_other_notes_are_not_mistaken_for_a_coverage_start_note() -> None:
    noisy = coverage(deleted_in_period=2, expired_in_period=1, possibly_paused=1)
    facts = carried_caveats(noisy, {}, TZ)
    assert not facts.pre_connection
    assert facts.gap_ids == frozenset()
    assert facts.lines == 3


# ------------------------------------------------------------------- the estimate
def full_plan(owner_language: str = "uz-Latn") -> list[Any]:
    target = SummaryTarget(E1, owner_language=owner_language)  # type: ignore[arg-type]
    return list(target.plan(RunContext()))


def test_the_estimate_covers_the_summarize_call_its_truncation_retry_and_both_judges() -> None:
    plan = full_plan()
    config = SummarizeConfig(model="")
    larger = min(config.max_output_tokens * 2, config.max_output_tokens_ceiling)
    assert larger > config.max_output_tokens  # there is a retry to cover
    with_loops = sum(1 for c in E1 if _loops_call_expected(c))
    purposes = Counter((call.purpose, call.max_output_tokens) for call in plan)
    assert purposes[(SUMMARIZE_PURPOSE, config.max_output_tokens)] == len(E1)
    assert purposes[(SUMMARIZE_PURPOSE, larger)] == len(E1)
    assert purposes[(FABRICATION_PURPOSE, 3000)] == len(E1)
    assert purposes[(LOOPS_PURPOSE, 3000)] == with_loops
    assert 0 < with_loops < len(E1)  # the judge is planned only where it will be called
    assert len(plan) == 3 * len(E1) + with_loops


def _loops_call_expected(conversation: Conversation) -> bool:
    open_items = [
        *(q for q in conversation.labels.questions_to_owner if q.status == "open"),
        *(c for c in conversation.labels.commitments if c.status == "open"),
    ]
    return any(not item.values for item in open_items)


def test_the_summarize_estimate_counts_the_real_prompt_and_the_messages() -> None:
    plan = full_plan()
    prompt = prompt_registry.active("summarize").text
    first = next(call for call in plan if call.purpose == SUMMARIZE_PURPOSE)
    c01 = E1[0]
    typed = sum(len(m.text or "") for m in c01.messages)
    assert first.input_chars >= len(prompt) + typed


def test_limit_plans_only_the_first_conversations() -> None:
    target = SummaryTarget(E1, owner_language="uz-Latn")
    assert len(target.plan(RunContext(limit=2))) < len(target.plan(RunContext(limit=4)))
    assert len(list(target.plan(RunContext(limit=1)))) in (3, 4)


def test_the_owner_language_is_part_of_the_estimate_only_through_the_profile() -> None:
    # Same set, same calls; the language changes the profile line, not the call count.
    assert len(full_plan("uz-Latn")) == len(full_plan("ru"))


# ----------------------------------------------------------------------- the gate
def test_a_limited_or_stopped_run_can_never_pass_even_with_everything_confirmed() -> None:
    """The set sizes are the whole set's, so measuring part of it is never complete."""
    everything = ReviewLedger(frozenset(c.id for c in E1), {}, {})
    checks = [CaveatCheck("c13", (), (), 1), CaveatCheck("c01", (), (), 0)]
    presence, spurious = caveat_metrics(
        checks, everything, presence_set_size=6, spurious_set_size=len(E1)
    )
    language = language_metric({"c01": LanguageVerdict(True, "uz", 1.0)}, everything, len(E1))
    for metric in (presence, spurious, language):
        assert metric.gate_status() is Status.NOT_EVALUABLE
    # Without the set size a metric would read the part for the whole.
    assert caveat_metrics(checks, everything)[0].gate_status() is Status.PASS


def test_an_owner_verdict_on_an_open_loop_is_bound_to_the_summary_text() -> None:
    ledger = ReviewLedger(
        frozenset({"c90"}),
        {},
        {coverage_key("c90.q1", "summary A"): "covered", "c90.q2": "covered"},
    )
    loops: dict[str, bool | None] = {"c90.q1": None, "c90.q2": None}
    same = recall_metric(loops, ledger, summary_texts=dict.fromkeys(loops, "summary A"))
    other = recall_metric(loops, ledger, summary_texts=dict.fromkeys(loops, "summary B"))
    assert (same.confirmed.numerator, same.pending) == (1, 1)  # q2's bare-id verdict is ignored
    assert (other.confirmed.numerator, other.pending) == (0, 2)
    assert other.gate_status() is Status.NOT_EVALUABLE
