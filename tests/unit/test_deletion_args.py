"""S03.5 deletion and settings tool arguments: schema-valid but semantically
invalid input is refused by our own validators (AGENTS §8, M03 D52)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from assistant.agent.tools.deletion import (
    DeleteContactDataArgs,
    DeleteConversationDataArgs,
    DeleteEverythingArgs,
)
from assistant.agent.tools.modes import UpdatePreferencesArgs
from assistant.agent.tools.registry import strict_schema

PREFS: dict[str, Any] = {
    "preferred_language": None,
    "timezone": None,
    "default_mode": None,
    "reply_language": None,
    "reply_tone": None,
    "reply_style": None,
    "message_retention_days": None,
    "control_chat_retention_days": None,
    "raw_update_retention_days": None,
}


def _prefs(**changes: Any) -> dict[str, Any]:
    return {**PREFS, **changes}


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (DeleteConversationDataArgs, {"conversation_id": None, "contact_id": None}),
        (DeleteConversationDataArgs, {"conversation_id": 1, "contact_id": 2}),
        (DeleteConversationDataArgs, {"conversation_id": 0, "contact_id": None}),
        (DeleteConversationDataArgs, {"conversation_id": -4, "contact_id": None}),
        (DeleteConversationDataArgs, {"conversation_id": 2**63, "contact_id": None}),
        (DeleteConversationDataArgs, {"conversation_id": 1, "contact_id": None, "all": True}),
        (DeleteContactDataArgs, {"contact_id": 0}),
        (DeleteContactDataArgs, {"contact_id": 2**63}),
        (DeleteContactDataArgs, {"contact_id": None}),
        (DeleteEverythingArgs, {"scope": "chat"}),
        (DeleteEverythingArgs, {}),
        (UpdatePreferencesArgs, _prefs(reply_language="uz;DROP")),
        (UpdatePreferencesArgs, _prefs(reply_tone="rude")),
        (UpdatePreferencesArgs, _prefs(reply_style="poem")),
        (UpdatePreferencesArgs, _prefs(message_retention_days=0)),
        (UpdatePreferencesArgs, _prefs(message_retention_days=13)),  # below the range
        (UpdatePreferencesArgs, _prefs(message_retention_days=91)),  # above the policy
        (UpdatePreferencesArgs, _prefs(control_chat_retention_days=365)),
        (UpdatePreferencesArgs, _prefs(raw_update_retention_days=8)),
        (UpdatePreferencesArgs, _prefs(raw_update_retention_days=0)),
        (UpdatePreferencesArgs, _prefs(message_retention_days=2**63)),
    ],
)
def test_schema_valid_but_semantically_invalid_deletion_and_settings_arguments_are_refused(
    model: type[BaseModel], values: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(values)


def test_valid_arguments_parse() -> None:
    assert (
        DeleteConversationDataArgs.model_validate(
            {"conversation_id": None, "contact_id": 3}
        ).contact_id
        == 3
    )
    assert DeleteEverythingArgs.model_validate({"scope": "everything"}).scope == "everything"
    parsed = UpdatePreferencesArgs.model_validate(
        _prefs(reply_language="match", reply_tone="formal", message_retention_days=30)
    )
    assert (parsed.reply_language, parsed.message_retention_days) == ("match", 30)


@pytest.mark.parametrize(
    "model",
    [
        DeleteConversationDataArgs,
        DeleteContactDataArgs,
        DeleteEverythingArgs,
        UpdatePreferencesArgs,
    ],
)
def test_the_schemas_are_plain_strict(model: type[BaseModel]) -> None:
    schema = strict_schema(model)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    dumped = str(schema)
    for keyword in ("minimum", "maximum", "pattern", "format", "minLength", "maxLength"):
        assert f"'{keyword}'" not in dumped
