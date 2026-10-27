"""S05.1 §3.4: deterministic validation of a sanitized triage verdict against
the messages shown and this conversation's open tasks. Pure; synthetic
text only."""

from __future__ import annotations

from typing import Any

from assistant.agent.triage import validate_triage
from assistant.agent.triage_schemas import SanitizedTriage, TriageResult, sanitize_triage

#: Shown messages → direction (classification), as the run loads them.
DIRECTIONS = {
    10: "owner_manual",
    11: "contact",
    12: "assistant_sent",
    13: "owner_automated",
    14: "contact",
}


def verdict(**overrides: Any) -> SanitizedTriage:
    values: dict[str, Any] = {
        "language": "en",
        "needs_reply": True,
        "contains_question_to_owner": False,
        "requests_decision": False,
        "urgency": "normal",
        "deadline": None,
        "commitments": [],
        "open_questions": [],
        "resolved_task_ids": [],
        "escalation_category": "needs_owner",
        "reasons": ["Waiting for an answer"],
        "script": "latin",
    }
    values.update(overrides)
    return sanitize_triage(TriageResult.model_validate(values), timezone="UTC")


def commitment(by: str, source: int, existing: int | None = None) -> dict[str, Any]:
    return {
        "by": by,
        "text": "Send the documents",
        "due": None,
        "source_message_id": source,
        "existing_task_id": existing,
    }


def question(source: int, *, is_decision: bool = False) -> dict[str, Any]:
    return {"text": "Which day?", "source_message_id": source, "is_decision": is_decision}


def test_a_commitment_citing_the_other_side_is_dropped() -> None:
    """A mis-sided task is worse than none: dropped, not demoted (TL decision)."""
    checked, counts = validate_triage(
        verdict(
            commitments=[
                commitment("owner", 11),  # the owner "promised" in the contact's message
                commitment("contact", 10),  # the contact "promised" in the owner's message
                commitment("owner", 10),
                commitment("owner", 12),  # sent by the assistant for the owner: owner side
                commitment("contact", 14),
            ]
        ),
        directions=DIRECTIONS,
        open_tasks={},
    )
    assert [(c.by, c.source_message_id) for c in checked.commitments] == [
        ("owner", 10),
        ("owner", 12),
        ("contact", 14),
    ]
    assert counts.commitments_misattributed == 2
    assert counts.commitments_kept == 3


def test_a_question_must_cite_a_contact_message() -> None:
    checked, counts = validate_triage(
        verdict(open_questions=[question(10), question(12), question(11, is_decision=True)]),
        directions=DIRECTIONS,
        open_tasks={},
    )
    assert [(q.source_message_id, q.is_decision) for q in checked.open_questions] == [(11, True)]
    assert counts.questions_misattributed == 2
    assert counts.questions_kept == 1


def test_automatic_messages_support_no_commitment_or_question() -> None:
    """An away/greeting message is neutral: never a promise or a question."""
    checked, counts = validate_triage(
        verdict(
            commitments=[commitment("owner", 13), commitment("contact", 13)],
            open_questions=[question(13)],
        ),
        directions=DIRECTIONS,
        open_tasks={},
    )
    assert checked.commitments == ()
    assert checked.open_questions == ()
    assert (counts.commitments_misattributed, counts.questions_misattributed) == (2, 1)


def test_unknown_or_foreign_task_ids_are_removed() -> None:
    """Only this conversation's open tasks may be updated or closed; anything
    else is removed quietly — never an error, never another chat's task."""
    checked, counts = validate_triage(
        verdict(
            commitments=[commitment("contact", 11, existing=7), commitment("contact", 14, 99)],
            resolved_task_ids=[7, 8, 99, 1000],
        ),
        directions=DIRECTIONS,
        open_tasks={7: "contact_commitment", 8: "open_question"},
    )
    assert [c.existing_task_id for c in checked.commitments] == [7, None]
    assert checked.resolved_task_ids == (7, 8)
    assert counts.foreign_task_ids == 3


def test_an_existing_task_of_the_other_side_is_never_updated() -> None:
    """Viro on #79: a contact commitment naming the owner's open task must not
    retitle it (§3.5 step 4); the id is removed, the commitment kept as new."""
    checked, counts = validate_triage(
        verdict(
            commitments=[
                commitment("contact", 11, existing=7),  # 7 is the owner's
                commitment("owner", 10, existing=8),  # 8 is a question
                commitment("owner", 12, existing=7),  # the right side
            ]
        ),
        directions=DIRECTIONS,
        open_tasks={7: "owner_commitment", 8: "open_question"},
    )
    assert [c.existing_task_id for c in checked.commitments] == [None, None, 7]
    assert counts.mismatched_task_ids == 2
    assert counts.foreign_task_ids == 0


def test_ids_not_shown_are_dropped() -> None:
    checked, counts = validate_triage(
        verdict(
            commitments=[commitment("contact", 11), commitment("contact", 555)],
            open_questions=[question(556), question(14)],
        ),
        directions=DIRECTIONS,
        open_tasks={},
    )
    assert [c.source_message_id for c in checked.commitments] == [11]
    assert [q.source_message_id for q in checked.open_questions] == [14]
    assert (counts.commitments_unshown, counts.questions_unshown) == (1, 1)


def test_flags_and_the_suspicious_category_are_kept_as_given() -> None:
    given = verdict(
        needs_reply=False,
        requests_decision=True,
        urgency="urgent",
        escalation_category="suspicious",
    )
    checked, _ = validate_triage(given, directions=DIRECTIONS, open_tasks={})
    assert (checked.needs_reply, checked.requests_decision) == (False, True)
    assert (checked.urgency, checked.escalation_category) == ("urgent", "suspicious")
