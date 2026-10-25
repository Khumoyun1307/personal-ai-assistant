"""S03.3 tool arguments: schema-valid but semantically invalid input is
refused by our own validators (AGENTS §8, M03 D52), and every schema stays a
plain strict schema."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from assistant.agent.tools.memory import ForgetArgs, ListMemoriesArgs, RememberArgs
from assistant.agent.tools.modes import SetConversationModeArgs, UpdatePreferencesArgs

REMEMBER: dict[str, Any] = {
    "content": "SYNTH note",
    "kind": "fact",
    "contact_id": None,
    "conversation_id": None,
    "applies_to_replies": None,
    "replaces_memory_id": None,
}


def _with(base: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {**base, **changes}


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (RememberArgs, _with(REMEMBER, content="")),
        (RememberArgs, _with(REMEMBER, content="   ")),
        (RememberArgs, _with(REMEMBER, content="x" * 501)),
        (RememberArgs, _with(REMEMBER, contact_id=3, conversation_id=4)),
        (RememberArgs, _with(REMEMBER, contact_id=0)),
        (RememberArgs, _with(REMEMBER, conversation_id=-1)),
        (RememberArgs, _with(REMEMBER, contact_id=2**63)),
        (RememberArgs, _with(REMEMBER, replaces_memory_id=0)),
        (RememberArgs, _with(REMEMBER, applies_to_replies=True)),  # a fact, not an instruction
        (RememberArgs, _with(REMEMBER, kind="shareable")),
        (ForgetArgs, {"memory_id": 0}),
        (ForgetArgs, {"memory_id": 2**63}),
        (
            ListMemoriesArgs,
            {"contact_id": 1, "conversation_id": 2, "scope": None, "limit": None},
        ),
        (
            ListMemoriesArgs,
            {"contact_id": 1, "conversation_id": None, "scope": "owner", "limit": None},
        ),
        (
            ListMemoriesArgs,
            {"contact_id": None, "conversation_id": None, "scope": None, "limit": 0},
        ),
        (
            ListMemoriesArgs,
            {"contact_id": None, "conversation_id": None, "scope": None, "limit": 51},
        ),
        (
            UpdatePreferencesArgs,
            {"preferred_language": None, "timezone": None, "default_mode": None},
        ),
        (
            UpdatePreferencesArgs,
            {"preferred_language": "uz;DROP", "timezone": None, "default_mode": None},
        ),
        (
            UpdatePreferencesArgs,
            {"preferred_language": "x" * 9, "timezone": None, "default_mode": None},
        ),
        (
            UpdatePreferencesArgs,
            {"preferred_language": None, "timezone": "x" * 65, "default_mode": None},
        ),
        (
            UpdatePreferencesArgs,
            {"preferred_language": None, "timezone": None, "default_mode": "assist"},
        ),
        (SetConversationModeArgs, {"conversation_id": None, "contact_id": None, "mode": "ignore"}),
        (SetConversationModeArgs, {"conversation_id": 1, "contact_id": 2, "mode": "ignore"}),
        (SetConversationModeArgs, {"conversation_id": 1, "contact_id": None, "mode": "assist"}),
        (SetConversationModeArgs, {"conversation_id": 0, "contact_id": None, "mode": "ignore"}),
    ],
)
def test_schema_valid_but_semantically_invalid_memory_arguments_are_refused(
    model: type[BaseModel], values: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(values)


def test_valid_arguments_are_accepted_and_trimmed() -> None:
    parsed = RememberArgs.model_validate(_with(REMEMBER, content="  SYNTH note  "))
    assert parsed.content == "SYNTH note"
    instruction = RememberArgs.model_validate(
        _with(REMEMBER, kind="instruction", applies_to_replies=True, contact_id=5)
    )
    assert instruction.applies_to_replies is True
    prefs = UpdatePreferencesArgs.model_validate(
        {"preferred_language": "uz-Latn", "timezone": "Asia/Tashkent", "default_mode": "ignore"}
    )
    assert prefs.preferred_language == "uz-Latn"
