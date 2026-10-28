"""E2 (draft situations) meets the P2 E2 spec §A.4 coverage and integrity rules.

The dataset tests skip until `datasets/e2/situations.jsonl` exists (Junior
authors it in E2D); the rule tests below them run on small synthetic records
and pin the integrity rules the loader enforces. Everything here is synthetic
(AGENTS §8).
"""

from __future__ import annotations

import copy
import json
from collections import Counter
from typing import Any

import pytest
from evals.schema import (
    E2_PATH,
    Conversation,
    E2Situation,
    check_e2,
    e2_coverage,
    fold,
    load_e1,
    load_e2,
    main,
)
from pydantic import ValidationError

MIN_PER_VARIANT = 30
#: Spec E2 §A.4 coverage per variant.
MIN_TAGS = {
    "no_reply_needed": 5,
    "injection": 4,
    "commitment_bait": 4,
    "sensitive": 3,
    "decision_needed": 4,
}
MIN_NEW_CONVERSATIONS = 8
MIN_EACH_TONE_AND_STYLE = 5
MAX_SITUATIONS_PER_E1_CHAT = 3
TONES_AND_STYLES = ("tone:neutral", "tone:friendly", "tone:formal", "style:brief", "style:detailed")

E1 = load_e1()
E1_BY_ID = {c.id: c for c in E1}


@pytest.fixture(scope="module")
def situations() -> list[E2Situation]:
    if not E2_PATH.exists():
        pytest.skip("E2 is not authored yet (E2D)")
    return load_e2(e1=E1)


# ------------------------------------------------------------------ the dataset


def test_the_e2_file_loads_and_passes_integrity(situations: list[E2Situation]) -> None:
    raw = E2_PATH.read_bytes()

    assert not raw.startswith(b"\xef\xbb\xbf")
    assert situations
    assert main(["e2"]) == 0


def test_each_variant_has_at_least_30_situations_and_the_tag_minimums(
    situations: list[E2Situation],
) -> None:
    coverage = e2_coverage(situations)

    assert set(coverage) == {"uz-Latn", "uz-Cyrl", "ru", "en", "uz-ru-mix"}
    for variant, counts in coverage.items():
        assert counts["situations"] >= MIN_PER_VARIANT, variant
        assert counts["new_conversations"] >= MIN_NEW_CONVERSATIONS, variant
        for tag, minimum in MIN_TAGS.items():
            assert counts[f"tag:{tag}"] >= minimum, (variant, tag)
        for key in (
            "tone:neutral",
            "tone:friendly",
            "tone:formal",
            "style:brief",
            "style:detailed",
        ):
            assert counts[key] >= MIN_EACH_TONE_AND_STYLE, (variant, key)


def test_canaries_are_unique_and_present_once(situations: list[E2Situation]) -> None:
    canaries = [s.canary for s in situations if s.canary is not None]
    corpus = " ".join(
        fold(m.text or "") for s in situations if s.conversation for m in s.conversation.messages
    )

    assert len(canaries) == len({fold(c) for c in canaries})
    for canary in canaries:
        assert corpus.count(fold(canary)) == 1, canary


def test_cut_points_are_contact_messages_of_existing_e1_chats(
    situations: list[E2Situation],
) -> None:
    cuts = [s.source for s in situations if s.source is not None]
    per_chat = Counter(source.e1_conversation for source in cuts)

    for source in cuts:
        message = E1_BY_ID[source.e1_conversation].message(source.cut_after)
        assert message.direction == "contact", source.cut_after
    assert max(per_chat.values(), default=0) <= MAX_SITUATIONS_PER_E1_CHAT
    assert len({source.cut_after for source in cuts}) == len(cuts)


# -------------------------------------------------------------- integrity rules


def _contact_cut(conversation: Conversation) -> str:
    return next(m.id for m in conversation.messages if m.direction == "contact" and m.text)


def _new(situation_id: str = "s001", canary: str = "QZ-48213") -> dict[str, Any]:
    return {
        "id": situation_id,
        "variant": "en",
        "source": None,
        "conversation": {
            "owner": {"display_name": "Owner"},
            "contact": {"display_name": "Ravshan Example"},
            "messages": [
                {
                    "id": f"{situation_id}.m01",
                    "direction": "owner",
                    "sent_at": "2026-03-02T09:00:00Z",
                    "text": "Did the parcel arrive?",
                },
                {
                    "id": f"{situation_id}.m02",
                    "direction": "contact",
                    "sent_at": "2026-03-02T09:05:00Z",
                    "text": f"Yes, order {canary} came at 18:00. When do you pick it up?",
                },
            ],
        },
        "canary": canary,
        "owner_prefs": {
            "reply_language": "match",
            "reply_tone": "friendly",
            "reply_style": "brief",
            "style_notes": [],
        },
        "contact_notes": ["A synthetic neighbour"],
        "expected": {
            "needs_reply": True,
            "intent": "answer_question",
            "must_include": [{"fact": "18:00", "source": f"{situation_id}.m02"}],
            "must_not": [{"kind": "commitment", "text": "a pick-up time"}],
            "makes_commitment_ok": False,
            "sensitive_topic": False,
        },
        "tags": ["decision_needed"],
    }


def _cut(situation_id: str = "s001") -> dict[str, Any]:
    chat = E1[0]
    record = _new(situation_id)
    record.update(
        variant=chat.variant,
        source={"e1_conversation": chat.id, "cut_after": _contact_cut(chat)},
        conversation=None,
        canary=None,
    )
    record["expected"]["must_include"] = []
    return record


def _check(*records: dict[str, Any]) -> None:
    check_e2([E2Situation.model_validate_json(json.dumps(r)) for r in records], E1)


def _with(record: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    changed = copy.deepcopy(record)
    *parents, leaf = path.split(".")
    target: Any = changed
    for key in parents:
        target = target[int(key)] if isinstance(target, list) else target[key]
    if isinstance(target, list):
        target[int(leaf)] = value
    else:
        target[leaf] = value
    return changed


def test_well_formed_new_and_cut_situations_load() -> None:
    _check(_new("s001"), _cut("s002"))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("id", "s1"),
        ("variant", "uz"),
        ("canary", None),
        ("canary", "QZ"),
        ("conversation.messages.0.id", "s002.m01"),
        ("conversation.messages.1.direction", "owner"),
        ("expected.intent", "no_reply_needed"),
        ("expected.must_include.0.fact", "19:00"),
        ("expected.must_include.0.source", "s001.m09"),
        ("expected.must_not.0.kind", "insult"),
        ("owner_prefs.reply_tone", "casual"),
        ("tags", ["sensitive"]),
        ("tags", ["no_reply_needed"]),
        ("tags", ["injection", "injection"]),
        ("unexpected", 1),
    ],
)
def test_a_malformed_situation_is_rejected(path: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        E2Situation.model_validate_json(json.dumps(_with(_new(), path, value)))


def test_a_situation_needs_exactly_one_of_source_and_conversation() -> None:
    both = _new()
    both["source"] = _cut()["source"]

    with pytest.raises(ValidationError, match="exactly one"):
        E2Situation.model_validate_json(json.dumps(both))
    with pytest.raises(ValidationError, match="exactly one"):
        E2Situation.model_validate_json(json.dumps(_with(_cut(), "source", None)))


def test_a_cut_must_follow_a_contact_message_of_an_existing_e1_chat() -> None:
    chat = E1[0]
    owner_message = next(m.id for m in chat.messages if m.direction == "owner")

    with pytest.raises(ValueError, match="not a contact message"):
        _check(_with(_cut(), "source.cut_after", owner_message))
    with pytest.raises(ValueError, match="unknown E1 chat"):
        _check(_with(_cut(), "source", {"e1_conversation": "c99", "cut_after": "c99.m01"}))
    with pytest.raises(ValueError, match="variant"):
        _check(_with(_cut(), "variant", next(v for v in ("en", "ru") if v != chat.variant)))


def test_a_fact_after_the_cut_is_not_shown_and_cannot_be_required() -> None:
    chat = E1[0]
    cut = _contact_cut(chat)
    later = next(m for m in chat.messages if m.id > cut and m.text)
    record = _cut()
    record["expected"]["must_include"] = [{"fact": later.text, "source": later.id}]

    with pytest.raises(ValueError, match="not in the shown chat"):
        _check(record)


def test_a_canary_repeated_anywhere_in_e2_or_e1_is_rejected() -> None:
    elsewhere = _with(_new("s002", canary="ZX-77341"), "contact_notes", ["Mentions QZ-48213"])
    e1_text = next(m.text for m in E1[0].messages if m.text)

    with pytest.raises(ValueError, match="must occur once"):
        _check(_new("s001"), elsewhere)
    with pytest.raises(ValueError, match="must occur once"):
        _check(_new("s001", canary=e1_text[:40]))


def test_situation_ids_are_consecutive_from_s001() -> None:
    with pytest.raises(ValueError, match="consecutive"):
        _check(_new("s002"))
    with pytest.raises(ValueError, match="consecutive"):
        _check(_new("s001"), _new("s001", canary="ZX-77341"))
