"""PF-13 units: `revise_draft` and `get_draft` in the registry, and their arguments.

AGENTS §8 tool checklist: a capability class, `owner_request` only, a release
flag that defaults off, registry filtering, a blocked stub without
`ai_managed`, strict schemas and schema-valid but semantically invalid
arguments. The handlers' behaviour is in
`tests/scenarios/test_m04_revise_from_chat.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from assistant.agent.draft_owner_schemas import (
    OwnerDraftResult,
    OwnerRevisionResult,
    char_count,
    output_model_for,
    word_count,
)
from assistant.agent.tools.drafts import (
    APPEND_AND_LENGTH_RULE,
    GET_DRAFT_FLAG,
    ONE_CARD_RULE,
    REVISE_DRAFT_FLAG,
    GetDraftArgs,
    ReviseDraftArgs,
)
from assistant.agent.tools.registry import (
    CapabilityClass,
    CapabilityTier,
    default_registry,
    strict_schema,
)

NAMES = ("revise_draft", "get_draft")


def offered(flags: frozenset[str], *, run_type: str = "owner_request", consent: bool = True) -> Any:
    return default_registry().for_run(
        run_type,
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=flags,
        managed_consent=consent,
        stub_managed_tools=True,
    )


def revise(**values: Any) -> ReviseDraftArgs:
    base: dict[str, Any] = {
        "conversation_id": 1,
        "action_id": None,
        "append_text": None,
        "min_words": None,
        "max_words": None,
    }
    return ReviseDraftArgs.model_validate(base | values)


def test_both_tools_are_registered_owner_request_only_with_their_own_flags() -> None:
    tools = {tool.name: tool for tool in default_registry().all()}
    revise_tool, read_tool = tools["revise_draft"], tools["get_draft"]
    # Propose: the result is still a card needing Approve, so a tainted run
    # may use it without a confirmation card (§7.1); it never sends.
    assert revise_tool.capability is CapabilityClass.PROPOSE
    assert not revise_tool.changes_state
    assert not revise_tool.needs_confirmation(tainted=True, args=revise())
    assert read_tool.capability is CapabilityClass.READ
    assert (
        (revise_tool.release_flag, read_tool.release_flag)
        == (
            REVISE_DRAFT_FLAG,
            GET_DRAFT_FLAG,
        )
        == ("tool.revise_draft", "tool.get_draft")
    )
    for tool in (revise_tool, read_tool):
        assert tool.allowed_run_types == frozenset({"owner_request"})
        assert tool.returns_managed is True


def test_registry_filtering_flags_off_on_and_contact_runs() -> None:
    assert not set(NAMES) & set(offered(frozenset()))
    only_read = offered(frozenset({GET_DRAFT_FLAG}))
    assert "get_draft" in only_read
    assert "revise_draft" not in only_read
    both = offered(frozenset({GET_DRAFT_FLAG, REVISE_DRAFT_FLAG}))
    assert set(NAMES) <= set(both)
    for run_type in ("draft_reply", "triage", "summarize"):
        assert offered(frozenset({GET_DRAFT_FLAG, REVISE_DRAFT_FLAG}), run_type=run_type) == {}


def test_without_managed_consent_both_are_blocked_stubs() -> None:
    real = {tool.name: tool for tool in default_registry().all()}
    stubs = offered(frozenset({GET_DRAFT_FLAG, REVISE_DRAFT_FLAG}), consent=False)
    for name in NAMES:
        assert stubs[name].handler is not real[name].handler
        assert stubs[name].spec() == real[name].spec()


def test_the_schemas_are_strict() -> None:
    for model in (ReviseDraftArgs, GetDraftArgs):
        schema = strict_schema(model)
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
    assert set(strict_schema(ReviseDraftArgs)["properties"]) == {
        "conversation_id",
        "action_id",
        "append_text",
        "min_words",
        "max_words",
    }


@pytest.mark.parametrize(
    ("values", "rule"),
    [
        ({"conversation_id": None}, ONE_CARD_RULE),
        ({"action_id": 5}, ONE_CARD_RULE),
        ({"append_text": "SYNTH words", "max_words": 7}, APPEND_AND_LENGTH_RULE),
        ({"min_words": 8, "max_words": 5}, "min_words must not be above max_words"),
        ({"min_words": 0}, "min_words and max_words are 1 to 1000, or null"),
        ({"append_text": "   "}, "append_text must be 1 to 4096 characters, or null"),
    ],
)
def test_semantically_invalid_arguments_are_refused_in_fixed_words(
    values: dict[str, Any], rule: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        revise(**values)
    assert rule in str(caught.value)


def test_unknown_fields_are_refused_and_get_draft_needs_one_card() -> None:
    with pytest.raises(ValidationError):
        revise(text="SYNTH")
    with pytest.raises(ValidationError):
        GetDraftArgs.model_validate({"conversation_id": 1, "action_id": 2})
    assert GetDraftArgs.model_validate({"conversation_id": None, "action_id": 2}).action_id == 2


def test_append_text_is_stripped_and_bounds_are_built() -> None:
    assert revise(append_text="  SYNTH  ").append_text == "SYNTH"
    bounds = revise(min_words=5, max_words=7).bounds
    assert bounds is not None
    assert bounds.describe() == "between 5 and 7 words"
    assert revise().bounds is None


def test_a_revision_has_its_own_output_schema_with_revision_kind() -> None:
    assert output_model_for("revise") is OwnerRevisionResult
    assert output_model_for("reply") is OwnerDraftResult
    assert output_model_for("new_message") is OwnerDraftResult
    schema = strict_schema(OwnerRevisionResult)
    assert schema["properties"]["revision_kind"]["enum"] == ["replacement", "instruction"]
    assert "revision_kind" not in OwnerDraftResult.model_json_schema()["properties"]


def test_counts_are_whitespace_words_and_code_points() -> None:
    sentence = "Ertaga ertalab o‘nda ofisda ko‘rishamiz, iltimos kechikmang do‘stim."
    assert word_count(sentence) == 8
    assert char_count(sentence) == len(sentence)
    assert word_count("  one\ttwo\nthree  ") == 3
