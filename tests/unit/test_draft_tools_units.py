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

from assistant.actions.requests import RequestSource, SourceSpan, copy_source_text
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
    LiteralEdit,
    ReviseDraftArgs,
)
from assistant.agent.tools.registry import (
    CapabilityClass,
    CapabilityTier,
    default_registry,
    strict_schema,
)
from assistant.agent.tools.replies import ProposeMessageArgs, source_failure


@pytest.mark.parametrize(
    "proposal_status",
    ["needs_input", "invalid_arguments", "stale", "not_available"],
)
def test_source_validation_keeps_v10_no_action_status(proposal_status: str) -> None:
    outcome = source_failure(proposal_status, "source_unavailable")
    assert outcome.status == "not_drafted"
    assert outcome.data["proposal_status"] == proposal_status
    assert outcome.data["reason"] == "source_unavailable"
    assert outcome.data["action"] is None
    assert outcome.data["effect_version"] is None
    assert outcome.data["requested_change_applied"] is False


def test_source_copy_preserves_unicode_code_points_and_explicit_separator() -> None:
    import hashlib

    field = "Tell them:  X1 x/h, O‘g‘il! e\u0301\n👍  "
    source = RequestSource(
        span=SourceSpan(
            turn_id=1,
            field="text",
            start=len("Tell them:"),
            end=len(field),
            field_sha256=hashlib.sha256(field.encode("utf-8")).hexdigest(),
        ),
        purpose="literal_body",
    )
    assert copy_source_text(field, source) == "  X1 x/h, O‘g‘il! e\u0301\n👍  "
    assert copy_source_text(field.casefold(), source) is None
    assert copy_source_text(field.replace("x/h", "h/h"), source) is None
    assert copy_source_text(field[:-1], source) is None


def test_source_proposal_schema_accepts_ids_and_never_model_text() -> None:
    schema = strict_schema(ProposeMessageArgs)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert set(schema["properties"]) == {
        "request_id",
        "request_version",
        "conversation_id",
        "answered_message_id",
        "source_ids",
    }
    values = {
        "request_id": 1,
        "request_version": 1,
        "conversation_id": 1,
        "answered_message_id": None,
        "source_ids": [1],
    }
    assert ProposeMessageArgs.model_validate(values).source_ids == [1]
    for invalid in (
        {"text": "X1"},
        {"source_ids": [True]},
        {"source_ids": []},
        {"request_id": True},
        {"request_version": 0},
        {"answered_message_id": True},
        {"contact_id": 1},
    ):
        with pytest.raises(ValidationError):
            ProposeMessageArgs.model_validate(values | invalid)


NAMES = ("revise_draft", "get_draft")


def offered(flags: frozenset[str], *, run_type: str = "owner_request", consent: bool = True) -> Any:
    return default_registry().for_run(
        run_type,
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=flags,
        managed_consent=consent,
        stub_managed_tools=True,
    )


def literal_args() -> dict[str, Any]:
    return {
        "request_id": 1,
        "request_version": 1,
        "expected_version": 1,
        "edit": "literal_append",
        "source_ids": [1],
    }


@pytest.mark.parametrize("model", [ProposeMessageArgs, LiteralEdit])
@pytest.mark.parametrize("count", [0, 1, 64, 65])
def test_literal_source_limits_are_local_validation(
    model: type[ProposeMessageArgs] | type[LiteralEdit], count: int
) -> None:
    source_schema = strict_schema(model)["properties"]["source_ids"]
    assert "minItems" not in source_schema
    assert "maxItems" not in source_schema
    values: dict[str, Any] = (
        literal_args()
        if model is LiteralEdit
        else {
            "request_id": 1,
            "request_version": 1,
            "conversation_id": 1,
            "answered_message_id": None,
        }
    )
    values["source_ids"] = list(range(1, count + 1))
    if count in (1, 64):
        assert model.model_validate(values).source_ids == values["source_ids"]
    else:
        with pytest.raises(ValidationError):
            model.model_validate(values)


def revise(**values: Any) -> ReviseDraftArgs:
    base: dict[str, Any] = {
        "conversation_id": 1,
        "action_id": None,
        "literal": None,
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
        "literal",
        "min_words",
        "max_words",
    }


@pytest.mark.parametrize(
    ("values", "rule"),
    [
        ({"conversation_id": None}, ONE_CARD_RULE),
        ({"action_id": 5}, ONE_CARD_RULE),
        ({"literal": literal_args(), "max_words": 7}, APPEND_AND_LENGTH_RULE),
        ({"min_words": 8, "max_words": 5}, "min_words must not be above max_words"),
        ({"min_words": 0}, "min_words and max_words are 1 to 1000, or null"),
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


def test_source_edit_has_no_model_supplied_text_and_bounds_are_built() -> None:
    assert revise(literal=literal_args()).literal is not None
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
