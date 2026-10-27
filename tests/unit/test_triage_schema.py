"""S05.1 triage output: the plain strict schema and `sanitize_triage` (caps,
trimming, owner-local dates into UTC). Synthetic text only."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from assistant.agent.triage_schemas import (
    MAX_ITEMS,
    MAX_REASONS,
    MAX_TEXT,
    TriageResult,
    parse_owner_local,
    sanitize_triage,
)


def _schema_keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _schema_keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _schema_keys(v)}
    return set()


def result(**overrides: Any) -> TriageResult:
    values: dict[str, Any] = {
        "language": "uz-Latn",
        "needs_reply": True,
        "contains_question_to_owner": True,
        "requests_decision": False,
        "urgency": "normal",
        "deadline": None,
        "commitments": [],
        "open_questions": [],
        "resolved_task_ids": [],
        "escalation_category": "needs_owner",
        "reasons": ["Savol bor"],
        "script": "latin",
    }
    values.update(overrides)
    return TriageResult.model_validate(values)


def commitment(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "by": "contact",
        "text": "Hujjatlarni yuborish",
        "due": None,
        "source_message_id": 11,
        "existing_task_id": None,
    }
    values.update(overrides)
    return values


def question(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "text": "Uchrashuv vaqti?",
        "source_message_id": 11,
        "is_decision": False,
    }
    values.update(overrides)
    return values


def test_the_triage_schema_is_plain_strict_with_language_first() -> None:
    """Every field required, nullable where optional, no defaults and no
    length/format keywords (M03 D52), `language` first (S03.6)."""
    schema = TriageResult.model_json_schema()
    assert not _schema_keys(schema) & {"format", "minLength", "maxLength", "pattern", "default"}
    for node in [schema, *schema.get("$defs", {}).values()]:
        assert node["additionalProperties"] is False
        assert sorted(node["required"]) == sorted(node["properties"])
    assert next(iter(schema["properties"])) == "language"
    assert list(schema["$defs"]["TriageQuestion"]["properties"]) == [
        "text",
        "source_message_id",
        "is_decision",
    ]
    assert schema["properties"]["urgency"]["enum"] == ["low", "normal", "high", "urgent"]


def test_due_and_deadline_parse_in_the_owner_timezone_or_become_null() -> None:
    tashkent = "Asia/Tashkent"  # UTC+5
    assert parse_owner_local("2026-11-03 18:00", tashkent) == datetime(
        2026, 11, 3, 13, 0, tzinfo=UTC
    )
    # A date alone is due by the end of that local day.
    assert parse_owner_local("2026-11-03", tashkent) == datetime(2026, 11, 3, 18, 59, tzinfo=UTC)
    # Berlin after the October change (UTC+1), before it (UTC+2).
    assert parse_owner_local("2026-10-26 09:00", "Europe/Berlin") == datetime(
        2026, 10, 26, 8, 0, tzinfo=UTC
    )
    assert parse_owner_local("2026-10-24 09:00", "Europe/Berlin") == datetime(
        2026, 10, 24, 7, 0, tzinfo=UTC
    )
    for unparseable in ("tomorrow", "Friday 17:00", "2026-13-01", "2026-11-03 25:00", "", "  "):
        assert parse_owner_local(unparseable, tashkent) is None, unparseable
    assert parse_owner_local(None, tashkent) is None
    # An unknown zone reads as UTC rather than failing.
    assert parse_owner_local("2026-11-03 18:00", "Nowhere/City") == datetime(
        2026, 11, 3, 18, 0, tzinfo=UTC
    )

    clean = sanitize_triage(
        result(
            deadline="2026-11-03 18:00",
            commitments=[
                commitment(due="2026-11-04"),
                commitment(due="ertaga", source_message_id=12),
            ],
        ),
        timezone=tashkent,
    )
    assert clean.deadline_at == datetime(2026, 11, 3, 13, 0, tzinfo=UTC)
    assert [c.due_at for c in clean.commitments] == [
        datetime(2026, 11, 4, 18, 59, tzinfo=UTC),
        None,
    ]


def test_sanitize_caps_lists_and_lengths() -> None:
    long_words = " ".join(["soz"] * 200)  # 799 characters
    clean = sanitize_triage(
        result(
            commitments=[commitment(source_message_id=i, text=long_words) for i in range(14)],
            open_questions=[question(source_message_id=i) for i in range(12)],
            reasons=["Birinchi", "  Ikkinchi  ", "Uchinchi", "Tortinchi", "Beshinchi"],
            resolved_task_ids=[3, 3, 4],
        ),
        timezone="UTC",
    )
    assert len(clean.commitments) == MAX_ITEMS == 10
    assert len(clean.open_questions) == MAX_ITEMS
    assert clean.reasons == ("Birinchi", "Ikkinchi", "Uchinchi")
    assert len(clean.reasons) == MAX_REASONS == 3
    for item in clean.commitments:
        assert len(item.text) <= MAX_TEXT == 300
        # Trimmed at a word, marked as cut.
        assert item.text.endswith("soz…")
    assert clean.capped == (14 - 10) + (12 - 10) + (5 - 3)
    assert clean.resolved_task_ids == (3, 4)


def test_sanitize_drops_items_left_empty() -> None:
    clean = sanitize_triage(
        result(
            commitments=[commitment(text="   "), commitment(source_message_id=12)],
            open_questions=[question(text="")],
            reasons=["", "Haqiqiy sabab"],
        ),
        timezone="UTC",
    )
    assert [c.source_message_id for c in clean.commitments] == [12]
    assert clean.open_questions == ()
    assert clean.reasons == ("Haqiqiy sabab",)
    assert clean.empty == 3


@pytest.mark.parametrize("field", ["urgency", "escalation_category", "script"])
def test_values_outside_the_enums_are_invalid_output(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        result(**{field: "made_up"})
