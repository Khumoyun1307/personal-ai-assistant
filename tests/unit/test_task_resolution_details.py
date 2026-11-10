"""Resolution references use text storage without interpreting legacy details."""

import json

import pytest

from assistant.inbox.tasks import resolution_ids


@pytest.mark.parametrize(
    "details",
    [
        None,
        "",
        "SYNTH: existing task details",
        "[]",
        "null",
        "42",
        '{"resolution_evidence_message_ids": [12]}',
        '{"type": "task_governance_v1", "original_details": null}',
    ],
)
def test_legacy_details_have_no_resolution_evidence(details: str | None) -> None:
    assert resolution_ids(details) == ()


def test_resolution_details_read_only_positive_integer_references() -> None:
    details = json.dumps(
        {
            "type": "task_resolution_evidence_v1",
            "resolution_evidence_message_ids": [12, 13, True, "14", -1, 0],
            "original_details": "SYNTH: existing task details",
        }
    )
    assert resolution_ids(details) == (12, 13)


def test_malformed_resolution_references_are_ignored() -> None:
    details = json.dumps(
        {"type": "task_resolution_evidence_v1", "resolution_evidence_message_ids": 12}
    )
    assert resolution_ids(details) == ()
