"""PF-16b: the `owner_drafting` deterministic checks (`check_case`), pure.

Each check is shown passing and failing on a hand-built result. Every text
is synthetic."""

from __future__ import annotations

import json
from typing import Any

import pytest
from evals.owner_drafting import (
    CardRecord,
    Observed,
    OwnerDraftingCase,
    ToolCallRecord,
    check_case,
    judge_input,
    judged_text,
)
from pydantic import ValidationError

from tests.unit.test_owner_drafting_dataset import card_expectation, case_record

LIVE_TEXT = "SYNTH one two three four five six seven eight nine ten"
LIVE_DRAFT = {"chat": "a", "origin": "agent_proposal", "text": LIVE_TEXT}
HISTORY = [{"role": "owner", "text": "SYNTH reply"}, {"role": "assistant", "text": "SYNTH ok"}]


def case(card: dict[str, Any] | None = None, **changes: Any) -> OwnerDraftingCase:
    record = case_record(**changes)
    record["expect"]["card"] = card
    return OwnerDraftingCase.model_validate_json(json.dumps(record))


def live_case(card: dict[str, Any]) -> OwnerDraftingCase:
    record = case_record(live_draft=LIVE_DRAFT, history=HISTORY)
    record["expect"].update(card=card, tools_called=["revise_draft"])
    return OwnerDraftingCase.model_validate_json(json.dumps(record))


def tool(name: str, status: str = "awaiting_approval", **args: Any) -> ToolCallRecord:
    return ToolCallRecord(name, args, status)


def card_row(
    text: str,
    *,
    action_id: int = 2,
    chat: str = "a",
    origin: str = "agent_proposal",
    version: int = 1,
    answered: str | None = None,
) -> CardRecord:
    return CardRecord(action_id, chat, origin, "awaiting_approval", version, text, answered)


LIVE = card_row(LIVE_TEXT, action_id=1)


def seen(
    *tools: ToolCallRecord,
    reply: str = "",
    new: tuple[CardRecord, ...] = (),
    after: CardRecord | None = None,
    before: CardRecord | None = None,
) -> Observed:
    return Observed(tuple(tools), reply, new, after, before)


# ------------------------------------------------------------ routing
def test_a_dictated_card_with_the_exact_text_passes() -> None:
    dictated = case(card_expectation(origin="owner_dictated", exact_text="SYNTH ertaga keldim"))
    result = seen(
        tool("propose_message"),
        new=(card_row("SYNTH ertaga keldim.", origin="owner_dictated"),),  # end punctuation aside
    )
    assert check_case(dictated, result) == ()


def test_routing_problems_are_named() -> None:
    dictated = case(card_expectation(origin="owner_dictated", exact_text="SYNTH ertaga keldim"))
    assert check_case(dictated, seen(tool("propose_reply"))) == (
        "routing:missing_tool:propose_message",
        "routing:no_card",
    )
    other_chat = seen(tool("propose_message"), new=(card_row("x", chat="b"),))
    assert "routing:card_on_another_chat" in check_case(dictated, other_chat)


def test_no_card_means_none_staged_and_the_live_card_unchanged() -> None:
    advice = case(
        None,
        expect={
            **case_record()["expect"],
            "card": None,
            "tools_called": [],
            "tools_not_called": ["propose_reply", "propose_message", "revise_draft"],
        },
    )
    assert check_case(advice, seen(reply="SYNTH advice")) == ()
    problems = check_case(advice, seen(tool("propose_reply"), new=(card_row("x"),)))
    assert problems == ("routing:forbidden_tool:propose_reply", "routing:unexpected_card")


def test_an_unexpected_revision_of_the_live_card_is_caught() -> None:
    record = case_record(live_draft=LIVE_DRAFT, history=HISTORY)
    record["expect"].update(card=None, tools_called=[], reply_states_word_count=True)
    counting = OwnerDraftingCase.model_validate_json(json.dumps(record))
    revised = card_row("SYNTH changed", action_id=1, version=2)
    assert check_case(counting, seen(reply="It has 11 words.", after=LIVE, before=LIVE)) == ()
    problems = check_case(counting, seen(reply="It has 11 words.", after=revised, before=LIVE))
    assert problems == ("routing:unexpected_revision",)
    assert check_case(counting, seen(reply="It has 111 words.", after=LIVE, before=LIVE)) == (
        "reply:word_count_not_stated",
    )


# ------------------------------------------------------------ card content
def test_origin_mode_and_answered_message() -> None:
    reply = case(card_expectation(origin="agent_proposal", mode="reply", answered="m1"))
    assert check_case(reply, seen(new=(card_row("SYNTH", answered="m1"),))) == (
        "routing:missing_tool:propose_message",
    )
    problems = check_case(reply, seen(tool("propose_message"), new=(card_row("SYNTH"),)))
    assert problems == ("card:answers_nothing", "card:wrong_answered_message")
    wrong = seen(
        tool("propose_message"), new=(card_row("S", origin="owner_dictated", answered="m3"),)
    )
    assert check_case(reply, wrong) == ("card:origin:owner_dictated", "card:wrong_answered_message")
    compose = case(card_expectation(mode="new_message"))
    answered = seen(tool("propose_message"), new=(card_row("S", answered="m3"),))
    assert check_case(compose, answered) == ("card:answers_a_message",)


def test_includes_compare_values_and_excludes_fold_case() -> None:
    want = case(
        card_expectation(includes=["250000", "15:00", "seshanba"], excludes=["Yangi xabar"])
    )
    good = card_row("Narxi 250 ming so'm, seshanbaga, soat 15:00 da")
    assert check_case(want, seen(tool("propose_message"), new=(good,))) == ()
    bad = card_row("YANGI XABAR yoz: narxi 300 ming, 16:00")
    assert check_case(want, seen(tool("propose_message"), new=(bad,))) == (
        "card:missing:250000",
        "card:missing:15:00",
        "card:missing:seshanba",
        "card:contains:Yangi xabar",
    )


def test_word_bounds_are_counted_on_whitespace() -> None:
    bounded = live_case(card_expectation(same_card=True, min_words=5, max_words=7))
    calls = (tool("revise_draft", status="revised"),)
    six = card_row("one two three four five six", action_id=1, version=2)
    assert check_case(bounded, seen(*calls, after=six, before=LIVE)) == ()
    four = card_row("one two three four", action_id=1, version=2)
    assert check_case(bounded, seen(*calls, after=four, before=LIVE)) == ("card:too_few_words",)
    eight = card_row("one two three four five six seven eight", action_id=1, version=2)
    assert check_case(bounded, seen(*calls, after=eight, before=LIVE)) == ("card:too_many_words",)


def test_an_append_keeps_the_old_text_as_its_prefix_on_the_same_card() -> None:
    append = live_case(card_expectation(same_card=True, keeps_prefix=True, includes=["See you"]))
    calls = (tool("revise_draft", status="revised"),)
    kept = card_row(f"{LIVE_TEXT} See you there!", action_id=1, version=2)
    assert check_case(append, seen(*calls, after=kept, before=LIVE)) == ()
    rewritten = card_row("SYNTH rewritten. See you there!", action_id=1, version=2)
    assert check_case(append, seen(*calls, after=rewritten, before=LIVE)) == ("card:prefix_lost",)
    # A new card instead of the same one revised.
    superseded = seen(*calls, new=(card_row(f"{LIVE_TEXT} See you"),), after=LIVE, before=LIVE)
    assert check_case(append, superseded) == (
        "routing:new_card_instead_of_revision",
        "routing:live_card_not_revised",
    )


def test_shorter_means_fewer_words_than_the_live_card() -> None:
    shorter = live_case(card_expectation(same_card=True, fewer_words=True))
    calls = (tool("revise_draft", status="revised"),)
    short = card_row("SYNTH short", action_id=1, version=2)
    assert check_case(shorter, seen(*calls, after=short, before=LIVE)) == ()
    longer = card_row(f"{LIVE_TEXT} and more", action_id=1, version=2)
    assert check_case(shorter, seen(*calls, after=longer, before=LIVE)) == ("card:not_shorter",)


def test_reply_identity_checks_fold_case() -> None:
    record = case_record()
    record["expect"].update(
        card=None,
        tools_called=[],
        reply_includes=["Alviro"],
        reply_excludes=["ChatGPT"],
    )
    identity = OwnerDraftingCase.model_validate_json(json.dumps(record))
    assert check_case(identity, seen(reply="Men ALVIRO, AI yordamchingizman.")) == ()
    assert check_case(identity, seen(reply="I am chatgpt.")) == (
        "reply:missing:Alviro",
        "reply:contains:ChatGPT",
    )


def test_a_reply_value_may_list_alternatives_in_another_script() -> None:
    """v1.1 (od03): the 2026-10-30 eval failed "Меня зовут Алвиро." though
    the name was right, only written in Cyrillic."""
    record = case_record()
    record["expect"].update(
        card=None, tools_called=[], reply_includes=[["Alviro", "Алвиро", "Альвиро"]]
    )
    identity = OwnerDraftingCase.model_validate_json(json.dumps(record))
    for reply in ("Меня зовут Алвиро.", "Я Альвиро.", "Меня зовут Alviro."):
        assert check_case(identity, seen(reply=reply)) == (), reply
    assert check_case(identity, seen(reply="Я ассистент.")) == (
        "reply:missing:Alviro|Алвиро|Альвиро",
    )
    for bad in ([[]], [["Alviro", ""]]):
        record["expect"]["reply_includes"] = bad
        with pytest.raises(ValidationError):
            OwnerDraftingCase.model_validate_json(json.dumps(record))


# ------------------------------------------------------------ the judge's input
def test_the_judge_reads_the_staged_text_and_the_criterion() -> None:
    record = case_record()
    record["expect"]["judge"] = "SYNTH criterion: playful"
    judged = OwnerDraftingCase.model_validate_json(json.dumps(record))
    staged = seen(tool("propose_message"), new=(card_row("SYNTH staged text"),))
    text = judged_text(judged, staged)
    assert text == "SYNTH staged text"
    rendered = judge_input(judged, text)
    assert "SYNTH criterion: playful" in rendered
    assert "SYNTH staged text" in rendered
    assert "Dilnoza" in rendered
    assert judged_text(judged, seen()) is None
