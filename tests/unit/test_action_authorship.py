"""H9: current proof, never the original trigger, determines authorship."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from assistant.actions.authorship import VersionAuthorship, current_authorship, is_ai_generated


def proof(**fields: Any) -> dict[str, Any]:
    return {
        "version": 2,
        "kind": "owner_literal_edit",
        "source_ids": [],
        "parent_version": 1,
        "retains_generated_text": False,
        **fields,
    }


@pytest.mark.parametrize(
    ("kind", "residue", "generated"),
    [
        ("owner_dictated", False, False),
        ("owner_literal_edit", False, False),
        ("owner_literal_edit", True, True),
        ("assistant_written", True, True),
        ("agent_proposal_selected", True, True),
    ],
)
def test_current_version_authorship_matrix(kind: str, residue: bool, generated: bool) -> None:
    value = proof(kind=kind, retains_generated_text=residue)
    assert is_ai_generated(value, version=2) is generated
    assert current_authorship(value, version=2) is not None


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        proof(version=1, parent_version=None),  # proof for another version
        proof(source_ids=[True]),
        proof(source_ids=[0]),
        proof(source_ids=[2**63]),
        proof(source_ids=[1, 1]),
        proof(version=True),
        proof(retains_generated_text="false"),
        proof(parent_version=None),
        proof(kind="assistant_written"),
        proof(text="Synthetic text must never be stored here"),
    ],
)
def test_missing_legacy_or_invalid_proof_is_conservatively_generated(value: object) -> None:
    assert is_ai_generated(value, version=2)
    assert current_authorship(value, version=2) is None


def test_authorship_round_trips_without_a_duplicate_body() -> None:
    value = VersionAuthorship.model_validate(proof(source_ids=[7, 9]))
    assert VersionAuthorship.model_validate_json(value.model_dump_json()) == value
    with pytest.raises(ValidationError):
        VersionAuthorship.model_validate(proof(source_ids=list(range(1, 66))))
