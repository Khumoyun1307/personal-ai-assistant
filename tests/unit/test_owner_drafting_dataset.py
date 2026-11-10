"""PF-16b: the `owner_drafting` cases load, cover what the plan asks for, and
the loader refuses records it cannot score. Every text here is synthetic."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from evals.owner_drafting import (
    CATEGORIES,
    DATASET_PATH,
    OwnerDraftingCase,
    load_cases,
    value_alternatives,
)

#: Every contact name the cases may use: invented for this set.
SYNTHETIC_NAMES = frozenset(
    {
        "Dilnoza",
        "Rustam",
        "Jasur",
        "Shuhrat",
        "Xurshida",
        "Nodira",
        "Kamola",
        "Otabek",
        "Malika",
        "Sardor",
        "Daniel",
        "Sophie",
        "Emily",
        "Victor",
        "Марина",
        "Олег",
        "Ольга",
    }
)
CARD_TOOLS = {"propose_reply", "propose_message", "revise_draft"}


def dictation_source(text: str, body: str, *, turn_index: int = 0) -> dict[str, Any]:
    start = text.index(body)
    return {
        "turn_index": turn_index,
        "start": start,
        "end": start + len(body),
        "field_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def case_record(**changes: Any) -> dict[str, Any]:
    """A valid case (a dictated message), as one JSON line holds it."""
    record: dict[str, Any] = {
        "id": "od01",
        "variant": "uz-Latn",
        "category": "dictate",
        "chats": [
            {
                "key": "a",
                "contact": "Dilnoza",
                "messages": [
                    {"key": "m1", "author": "contact", "minutes_ago": 30, "text": "Qachon?"},
                    {"key": "m2", "author": "owner", "minutes_ago": 20, "text": "Kechqurun."},
                    {"key": "m3", "author": "contact", "minutes_ago": 10, "text": "Yaxshi."},
                ],
            }
        ],
        "history": [],
        "live_draft": None,
        "turn": "Dilnozaga yoz: SYNTH ertaga keldim",
        "dictation_sources": [
            dictation_source("Dilnozaga yoz: SYNTH ertaga keldim", "SYNTH ertaga keldim")
        ],
        "expect": {
            "tools_called": ["propose_message"],
            "tools_not_called": [],
            "card": card_expectation(exact_text="SYNTH ertaga keldim", origin="owner_dictated"),
            "reply_includes": [],
            "reply_excludes": [],
            "reply_states_word_count": False,
            "judge": None,
        },
    }
    record.update(changes)
    return record


def card_expectation(**changes: Any) -> dict[str, Any]:
    return {
        "chat": "a",
        "origin": None,
        "mode": None,
        "answered": None,
        "exact_text": None,
        "includes": [],
        "excludes": [],
        "min_words": None,
        "max_words": None,
        "same_card": False,
        "keeps_prefix": False,
        "fewer_words": False,
        **changes,
    }


def build(**changes: Any) -> OwnerDraftingCase:
    return OwnerDraftingCase.model_validate_json(json.dumps(case_record(**changes)))


def with_card(**changes: Any) -> dict[str, Any]:
    record = case_record()
    record["expect"]["card"] = card_expectation(**changes)
    return record


# ------------------------------------------------------------ the set
CASES = load_cases()


def test_the_set_has_about_thirty_cases_in_every_category() -> None:
    assert 28 <= len(CASES) <= 34
    assert [c.id for c in CASES] == [f"od{n:02d}" for n in range(1, len(CASES) + 1)]
    assert {c.category for c in CASES} == set(CATEGORIES)


def test_most_cases_are_uzbek_latin_with_russian_and_english() -> None:
    variants = Counter(c.variant for c in CASES)
    assert variants["uz-Latn"] > len(CASES) / 2
    assert variants["ru"] >= 3
    assert variants["en"] >= 3


def test_every_contact_is_a_synthetic_name() -> None:
    names = {chat.contact for case in CASES for chat in case.chats}
    assert names <= SYNTHETIC_NAMES


def test_cases_that_expect_no_card_forbid_the_card_tools() -> None:
    for case in CASES:
        if case.expect.card is None:
            assert set(case.expect.tools_not_called) >= CARD_TOOLS, case.id


def test_the_plans_behaviours_are_each_pinned() -> None:
    by = {c.id: c for c in CASES}
    identity = [c for c in CASES if c.category == "identity"]
    assert all(
        any("Alviro" in value_alternatives(v) for v in c.expect.reply_includes) for c in identity
    )
    assert all("ChatGPT" in c.expect.reply_excludes for c in identity)
    cards = [c.expect.card for c in by.values() if c.expect.card is not None]
    assert any(card.answered is not None for card in cards)  # reply to a named message
    assert any(card.mode == "new_message" and card.origin == "agent_proposal" for card in cards)
    assert any(card.keeps_prefix for card in cards)  # append
    assert any(card.fewer_words for card in cards)  # shorter
    assert any((card.min_words, card.max_words) == (5, 7) for card in cards)
    assert any(
        card.exact_text is not None and card.origin == "owner_dictated" and card.excludes
        for card in cards
    )  # dictation with words addressed to the bot left out
    picked = [c for c in CASES if c.category == "pick_suggestion"]
    assert picked
    assert all(c.expect.card and c.expect.card.origin == "agent_proposal" for c in picked)
    counted = [c for c in CASES if c.category == "word_count"]
    assert all(c.expect.reply_states_word_count for c in counted)
    assert all("get_draft" in c.expect.tools_called for c in counted)
    judged = [c for c in CASES if c.expect.judge is not None]
    assert {c.category for c in judged} >= {"tone", "substance"}
    assert all(c.expect.card is not None for c in judged)


def test_compose_cases_are_in_chats_where_no_reply_is_needed() -> None:
    """The contact's last word is a closing one or the owner wrote last: a
    "no reply needed" veto would refuse them (owner decision D1)."""
    for case in CASES:
        if case.category == "compose":
            card = case.expect.card
            assert card is not None
            assert card.mode == "new_message"
            last = case.chat(card.chat).messages[-1]
            assert last.author == "owner" or "?" not in last.text, case.id


# ------------------------------------------------------------ the loader
def test_a_valid_record_loads() -> None:
    case = build()
    assert case.expect.card is not None
    assert case.expect.card.exact_text == "SYNTH ertaga keldim"


def test_dictation_can_reference_an_earlier_owner_turn_after_clarification(tmp_path: Path) -> None:
    original = "SYNTH yoz: X1 x/h — Oʻgʻil, e\u0301!\n  Ha."
    body = original.removeprefix("SYNTH yoz: ")
    record = case_record(
        history=[
            {"role": "owner", "text": original},
            {"role": "assistant", "text": "SYNTH Kimga?"},
            {"role": "owner", "text": "SYNTH Dilnozaga"},
        ],
        turn="SYNTH yozaver",
        dictation_sources=[dictation_source(original, body)],
    )
    record["expect"]["card"]["exact_text"] = body
    loaded = OwnerDraftingCase.model_validate_json(json.dumps(record))
    assert loaded.expect.card is not None
    assert loaded.expect.card.exact_text == body
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert load_cases(path) == [loaded]


def test_picked_suggestion_has_explicit_artifact_and_owner_selection_sources() -> None:
    case = next(case for case in CASES if case.id == "od31")
    assert case.suggestion_selection is not None
    assert case.expect.card is not None
    assert case.suggestion_text() == case.expect.card.exact_text
    assert case.suggestion_selection.instruction.turn_index == len(case.history)


@pytest.mark.parametrize("invalid", ["missing", "hash", "role", "chronology"])
def test_picked_suggestion_rejects_missing_or_invalid_selection_metadata(invalid: str) -> None:
    record = next(case for case in CASES if case.id == "od31").model_dump(mode="json")
    if invalid == "missing":
        record["suggestion_selection"] = None
    elif invalid == "hash":
        record["suggestion_selection"]["sources"][0]["field_sha256"] = "0" * 64
    elif invalid == "role":
        record["history"][1]["role"] = "owner"
    else:
        record["suggestion_selection"]["instruction"] = record["suggestion_selection"]["sources"][0]
    with pytest.raises(ValueError):  # noqa: PT011
        OwnerDraftingCase.model_validate_json(json.dumps(record))


@pytest.mark.parametrize(
    "change",
    [
        {"dictation_sources": []},
        {"turn": "Dilnozaga yoz: SYNTH ertaga keldim!"},  # stale field hash
        {"turn_quoted_text": "SYNTH ertaga keldim", "turn": "SYNTH write it"},
        {"turn_third_party": True},  # forwarded, no owner component
        {"dictation_sources": [{**dictation_source("SYNTH", "SYNTH"), "turn_index": 99}]},
        {"dictation_sources": [{**dictation_source("SYNTH", "SYNTH"), "end": 999}]},
        {"dictation_sources": [{**dictation_source("SYNTH", "SYNTH"), "field": "quoted_text"}]},
        {
            "history": [{"role": "assistant", "text": "SYNTH ertaga keldim"}],
            "dictation_sources": [dictation_source("SYNTH ertaga keldim", "SYNTH ertaga keldim")],
        },
    ],
)
def test_invalid_dictation_sources_are_rejected(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        build(**change)


def test_an_owner_component_remains_distinct_from_the_quote() -> None:
    case = build(turn_third_party=True, turn_quoted_text="SYNTH contact's words")
    assert case.expect.card is not None
    assert case.expect.card.exact_text == "SYNTH ertaga keldim"


@pytest.mark.parametrize("changed", ["synth ertaga keldim", "SYNTH ertaga keldim."])
def test_expected_dictation_must_equal_the_source_characters(changed: str) -> None:
    record = with_card(origin="owner_dictated", exact_text=changed)
    with pytest.raises(ValueError):  # noqa: PT011
        OwnerDraftingCase.model_validate_json(json.dumps(record))


def test_ordered_spans_copy_separators_and_code_point_offsets() -> None:
    text = "SYNTH 📨: X1\n  Oʻgʻil, e\u0301!"
    record = case_record(
        turn=text,
        dictation_sources=[
            dictation_source(text, "X1"),
            dictation_source(text, "\n  "),
            dictation_source(text, "Oʻgʻil, e\u0301!"),
        ],
    )
    body = "X1\n  Oʻgʻil, e\u0301!"
    record["expect"]["card"]["exact_text"] = body
    case = OwnerDraftingCase.model_validate_json(json.dumps(record))
    assert case.dictation_text() == body


@pytest.mark.parametrize(
    "record",
    [
        with_card(answered="m9"),  # no such message
        with_card(answered="m2"),  # the owner's own message
        with_card(answered="m1", mode="new_message"),  # a new message answers nothing
        with_card(min_words=7, max_words=5),
        with_card(same_card=True),  # no live draft to revise
        with_card(exact_text="SYNTH words the owner never wrote", origin="owner_dictated"),
        with_card(exact_text="SYNTH never suggested", origin="agent_proposal"),
        with_card(chat="z"),
        case_record(live_draft={"chat": "z", "origin": "agent_proposal", "text": "SYNTH"}),
        case_record(live_draft={"chat": "a", "origin": "agent_proposal", "text": "SYNTH"}),
        case_record(category="nonsense"),
        case_record(variant="uz-Cyrl"),
    ],
)
def test_the_model_refuses_what_it_cannot_score(record: dict[str, Any]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011  (pydantic's ValidationError)
        OwnerDraftingCase.model_validate_json(json.dumps(record))


def test_a_tool_both_required_and_forbidden_is_refused() -> None:
    record = case_record()
    record["expect"]["tools_not_called"] = ["propose_message"]
    with pytest.raises(ValueError):  # noqa: PT011
        OwnerDraftingCase.model_validate_json(json.dumps(record))


def test_messages_must_be_oldest_first() -> None:
    record = case_record()
    record["chats"][0]["messages"].reverse()
    with pytest.raises(ValueError):  # noqa: PT011
        OwnerDraftingCase.model_validate_json(json.dumps(record))


def test_ids_must_run_from_od01_and_errors_never_echo_text(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(json.dumps(case_record(id="od02")) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="should be od01"):
        load_cases(path)
    bad = case_record()
    bad["turn"] = "SYNTH-SECRET-TEXT"
    bad["expect"]["card"]["exact_text"] = "SYNTH-SECRET-TEXT-NOT-IN-TURN"
    path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1") as raised:
        load_cases(path)
    assert "SYNTH" not in str(raised.value)


def test_the_dataset_file_is_the_frozen_one() -> None:
    assert DATASET_PATH.name == "cases.jsonl"
    assert DATASET_PATH.parent.name == "owner_drafting"
