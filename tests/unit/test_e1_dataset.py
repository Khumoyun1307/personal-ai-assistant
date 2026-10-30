"""E1 and the retrieval set meet IMPLEMENTATION_PLAN §10.1 and stay frozen.

E1 is the M03 summary/catch-up gate dataset (S03.4); its first 8
conversations are the deferred M00 S00.3 smoke set. Everything here is
synthetic (AGENTS §8). `evals/` sits outside `src/`; pytest's rootdir on
`sys.path` makes it importable here, and test_layering keeps `src/` from
importing it.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from typing import Any

import pytest
from evals import manifest
from evals.schema import (
    APOSTROPHES,
    E1_PATH,
    TAGS,
    VARIANTS,
    Conversation,
    MustNotState,
    PromptInjection,
    RetrievalQuery,
    fold,
    load_e1,
    load_queries,
    script_of,
)
from pydantic import ValidationError

MIN_CONVERSATIONS = 40
MIN_LABELLED_ITEMS = 120
MIN_PER_VARIANT = 8
MIN_QUERIES = 30
SMOKE_VARIANTS = Counter({"uz-Latn": 2, "uz-Cyrl": 2, "uz-ru-mix": 2, "ru": 2})

CONVERSATIONS = load_e1()
QUERIES = load_queries()
MESSAGES = {m.id: m for c in CONVERSATIONS for m in c.messages}


def _first_record() -> dict[str, Any]:
    record: dict[str, Any] = json.loads(E1_PATH.read_text(encoding="utf-8").splitlines()[0])
    return record


# --------------------------------------------------------------------- §10.1 sizes


def test_e1_meets_the_10_1_minimums() -> None:
    per_variant = Counter(c.variant for c in CONVERSATIONS)
    items = sum(len(c.labels.labelled_items()) for c in CONVERSATIONS)

    assert len(CONVERSATIONS) >= MIN_CONVERSATIONS
    assert items >= MIN_LABELLED_ITEMS
    assert set(per_variant) == set(VARIANTS)
    assert min(per_variant.values()) >= MIN_PER_VARIANT


def test_every_label_kind_is_represented() -> None:
    for kind in ("questions_to_owner", "commitments", "decisions", "key_facts"):
        assert sum(len(getattr(c.labels, kind)) for c in CONVERSATIONS) >= 10, kind
    statuses = Counter(q.status for c in CONVERSATIONS for q in c.labels.questions_to_owner)
    assert statuses["open"] >= 10
    assert statuses["answered"] >= 10


def test_the_first_eight_conversations_are_the_s00_3_smoke_set() -> None:
    smoke = [c for c in CONVERSATIONS if c.smoke]

    assert [c.id for c in smoke] == [f"c{n:02d}" for n in range(1, 9)]
    assert CONVERSATIONS[:8] == smoke
    assert Counter(c.variant for c in smoke) == SMOKE_VARIANTS
    assert all(10 <= len(c.messages) <= 20 for c in smoke)
    for conversation in smoke:
        labels = conversation.labels
        assert labels.questions_to_owner, conversation.id
        assert labels.commitments, conversation.id


# ------------------------------------------------------------------- integrity


def test_ids_are_unique_across_the_dataset() -> None:
    conversation_ids = [c.id for c in CONVERSATIONS]
    message_ids = [m.id for c in CONVERSATIONS for m in c.messages]
    label_ids = [
        item.id
        for c in CONVERSATIONS
        for field in type(c.labels).model_fields
        for item in getattr(c.labels, field)
    ]
    for ids in (conversation_ids, message_ids, label_ids, [q.id for q in QUERIES]):
        assert len(ids) == len(set(ids))


def test_every_cited_message_exists_in_its_own_conversation() -> None:
    for conversation in CONVERSATIONS:
        own = {m.id for m in conversation.messages}
        labels = conversation.labels
        for item in labels.labelled_items():
            assert set(item.source_message_ids) <= own, item.id
        for question in labels.questions_to_owner:
            assert set(question.answered_in) <= own, question.id
        others: list[PromptInjection | MustNotState] = [
            *labels.prompt_injections,
            *labels.must_not_state,
        ]
        for other in others:
            assert set(other.source_message_ids) <= own, other.id


def test_every_reportable_gap_has_a_caveat_and_delayed_gaps_have_none() -> None:
    gap_conversations = [c for c in CONVERSATIONS if c.coverage_gaps or c.coverage_started_at]
    assert len(gap_conversations) >= 5
    certainties = Counter(g.certainty for c in CONVERSATIONS for g in c.coverage_gaps)
    assert certainties["possible"] >= 2
    assert certainties["definite"] >= 1
    assert certainties["delayed"] >= 1

    for conversation in gap_conversations:
        caveated = {v.gap_id for v in conversation.labels.expected_caveats}
        for gap in conversation.coverage_gaps:
            if gap.certainty == "delayed":
                assert gap.id not in caveated, gap.id
                assert any(x.reason == "delayed_gap" for x in conversation.labels.must_not_state)
            else:
                assert gap.id in caveated, gap.id
        if conversation.coverage_started_at is not None:
            reasons = {v.reason for v in conversation.labels.expected_caveats}
            assert "pre_connection" in reasons, conversation.id


def test_no_gap_hides_a_stored_message() -> None:
    """A reportable gap is a window with nothing stored; a delayed one may hold messages."""
    for conversation in CONVERSATIONS:
        for gap in conversation.coverage_gaps:
            if gap.certainty == "delayed":
                continue
            inside = [
                m.id for m in conversation.messages if gap.at_risk_from < m.sent_at < gap.at_risk_to
            ]
            assert inside == [], gap.id


def test_every_hard_case_occurs() -> None:
    tags = Counter(t for c in CONVERSATIONS for t in c.tags)
    assert set(tags) == set(TAGS)
    assert tags["code_switch"] >= MIN_PER_VARIANT


def test_tags_match_the_data() -> None:
    for conversation in CONVERSATIONS:
        tags = set(conversation.tags)
        messages = conversation.messages
        labels = conversation.labels
        assert ("injection" in tags) == bool(labels.prompt_injections), conversation.id
        assert ("deletion" in tags) == any(m.deleted for m in messages), conversation.id
        assert ("voice" in tags) == any(m.media == "voice" for m in messages), conversation.id
        if "edit_time" in tags or "edit_detail" in tags:
            assert any(m.edited for m in messages), conversation.id
            assert any(x.reason == "superseded_by_edit" for x in labels.must_not_state)
        if any(m.deleted for m in messages):
            assert any(x.reason == "deleted" for x in labels.must_not_state), conversation.id


def test_injections_are_labelled_as_data_not_instructions() -> None:
    injected = [c for c in CONVERSATIONS if c.labels.prompt_injections]
    assert {c.variant for c in injected} >= {"uz-Latn", "ru", "en"}
    for conversation in injected:
        for injection in conversation.labels.prompt_injections:
            assert all(MESSAGES[m].direction == "contact" for m in injection.source_message_ids), (
                injection.id
            )
            assert "must not be followed" in injection.summary, injection.id


def test_variant_scripts_match_the_text() -> None:
    expected = {"uz-Latn": {"latin"}, "en": {"latin"}, "uz-Cyrl": {"cyrillic"}}
    for conversation in CONVERSATIONS:
        scripts = {script_of(m.text) for m in conversation.messages if m.text}
        scripts.discard("none")
        if conversation.variant in expected:
            assert scripts == expected[conversation.variant], conversation.id
        elif conversation.variant == "ru" and "translit" not in conversation.tags:
            assert scripts == {"cyrillic"}, conversation.id
        if "mixed_script" in conversation.tags:
            assert "mixed" in scripts, conversation.id


def test_no_word_mixes_latin_and_cyrillic_letters() -> None:
    """Homoglyph guard: a Cyrillic look-alike letter inside a Latin word (or
    the reverse) would silently change what search and the model see."""
    offenders = []
    for message in MESSAGES.values():
        for text in (message.text, message.original_text, message.purged_text):
            for word in (text or "").split():
                if script_of("".join(ch for ch in word if ch.isalpha())) == "mixed":
                    offenders.append(f"{message.id}: {word}")
    assert offenders == []


def test_phone_numbers_are_obviously_fictional() -> None:
    corpus = "\n".join(
        t for m in MESSAGES.values() for t in (m.text, m.original_text, m.purged_text) if t
    )
    phones = re.findall(r"\+\d[\d ]{8,}\d", corpus)
    assert phones
    assert all(re.fullmatch(r"\+998 90 000 00 \d\d", p) for p in phones), phones


# ------------------------------------------------------------------- retrieval


def test_retrieval_set_size_and_targets() -> None:
    assert len(QUERIES) >= MIN_QUERIES
    for query in QUERIES:
        for target in query.target_message_ids:
            message = MESSAGES[target]
            assert not message.deleted, query.id
            assert message.text, query.id
        evidence = " ".join(MESSAGES[t].text or "" for t in query.target_message_ids)
        for value in query.answer_values:
            assert fold(value) in fold(evidence), (query.id, value)


def test_retrieval_intent_minimums() -> None:
    intents = Counter(i for q in QUERIES for i in q.intents)
    langs = Counter(q.lang for q in QUERIES)

    assert intents["cross_script"] >= 10
    assert intents["no_apostrophe"] >= 4
    assert intents["paraphrase"] >= 10
    assert intents["exact"] >= 5
    assert langs["ru"] >= 5
    assert langs["en"] >= 5
    directions = Counter(q.script for q in QUERIES if "cross_script" in q.intents)
    assert directions["latin"] >= 3
    assert directions["cyrillic"] >= 3
    # The PRD's own examples (M03 user-visible result, S03.4).
    texts = {q.text for q in QUERIES}
    assert "the address Kamola sent" in texts
    assert any("meeting time" in t for t in texts)


def test_cross_script_queries_cross_scripts() -> None:
    for query in QUERIES:
        assert script_of(query.text) in (query.script, "none"), query.id  # "none": a bare number
        target_scripts = {script_of(MESSAGES[t].text or "") for t in query.target_message_ids}
        if "cross_script" in query.intents:
            assert target_scripts - {query.script}, query.id


def test_no_apostrophe_queries_contain_no_apostrophes() -> None:
    for query in QUERIES:
        if "no_apostrophe" in query.intents:
            assert not set(query.text) & set(APOSTROPHES), query.id


# ---------------------------------------------------------------------- freeze


def test_manifest_matches_the_dataset_files() -> None:
    assert manifest.problems() == []
    names = {p.relative_to(manifest.DATASETS).as_posix() for p in manifest.frozen_files()}
    assert names == {
        "e1/conversations.jsonl",
        "retrieval/queries.jsonl",
        "e3/bursts.jsonl",
        "e3/monitor_cases.jsonl",
        "e2/situations.jsonl",
        "e4/cases.jsonl",
        "owner_drafting/cases.jsonl",
    }


def test_dataset_files_are_nfc_and_lf() -> None:
    for path in manifest.frozen_files():
        raw = path.read_bytes()
        assert b"\r" not in raw, path.name
        text = raw.decode("utf-8")
        assert unicodedata.is_normalized("NFC", text), path.name


# ------------------------------------------------------- the schema bites back


def test_schema_rejects_a_value_missing_from_the_cited_messages() -> None:
    record = _first_record()
    record["labels"]["key_facts"][0]["values"] = ["99:99"]
    with pytest.raises(ValidationError, match="not in its cited messages"):
        Conversation.model_validate_json(json.dumps(record))


def test_schema_rejects_an_unknown_message_reference() -> None:
    record = _first_record()
    record["labels"]["key_facts"][0]["source_message_ids"] = ["c01.m99"]
    with pytest.raises(ValidationError, match="unknown message"):
        Conversation.model_validate_json(json.dumps(record))


def test_schema_rejects_a_possible_gap_without_a_caveat() -> None:
    record = _first_record()
    record["coverage_gaps"] = [
        {
            "id": "c01.g1",
            "kind": "outage",
            "certainty": "possible",
            "at_risk_from": "2026-10-14T05:00:00Z",
            "at_risk_to": "2026-10-14T06:00:00Z",
        }
    ]
    with pytest.raises(ValidationError, match="needs an expected caveat"):
        Conversation.model_validate_json(json.dumps(record))


def test_schema_rejects_extra_fields_and_deleted_evidence() -> None:
    record = _first_record()
    record["unexpected"] = True
    with pytest.raises(ValidationError, match="Extra inputs"):
        Conversation.model_validate_json(json.dumps(record))

    c09 = next(c for c in CONVERSATIONS if c.id == "c09")
    data = c09.model_dump(mode="json")
    data["labels"]["key_facts"][0]["source_message_ids"] = ["c09.m06"]  # the deleted one
    data["labels"]["key_facts"][0]["values"] = []
    with pytest.raises(ValidationError, match="deleted content cannot be evidence"):
        Conversation.model_validate_json(json.dumps(data))


def test_schema_rejects_a_query_target_outside_its_conversations() -> None:
    data = QUERIES[0].model_dump(mode="json")
    data["conversation_ids"] = ["c02"]
    with pytest.raises(ValidationError, match="outside conversation_ids"):
        RetrievalQuery.model_validate_json(json.dumps(data))
