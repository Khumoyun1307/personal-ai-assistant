"""The settings read tool is strict, content-free and opt-in."""

import pytest
from pydantic import ValidationError

from assistant.agent.tools.preferences import GET_PREFERENCES, GetPreferencesArgs
from assistant.agent.tools.registry import CapabilityClass, default_registry
from assistant.agent.tools.status import GET_STATUS


def test_get_preferences_has_no_arguments_or_account_selector() -> None:
    assert GetPreferencesArgs.model_validate({}).model_dump() == {}
    for invented in ({"account_id": 2}, {"digest_time": "25:00"}):
        with pytest.raises(ValidationError):
            GetPreferencesArgs.model_validate(invented)
    schema = GET_PREFERENCES.spec().parameters
    assert schema["additionalProperties"] is False
    assert schema["properties"] == {}
    assert schema["required"] == []


def test_get_preferences_registry_filters_flags_and_run_types() -> None:
    registry = default_registry()
    assert GET_PREFERENCES in registry.all()
    assert GET_PREFERENCES.capability is CapabilityClass.READ
    assert GET_PREFERENCES.min_tier == GET_STATUS.min_tier
    assert GET_PREFERENCES.allowed_run_types == frozenset({"owner_request"})
    assert not GET_PREFERENCES.returns_managed
    assert not GET_PREFERENCES.needs_confirmation(tainted=True)
    for run_type, flags, expected in (
        ("owner_request", frozenset(), False),
        ("owner_request", frozenset({"tool.get_preferences"}), True),
        ("draft_reply", frozenset({"tool.get_preferences"}), False),
        ("triage", frozenset({"tool.get_preferences"}), False),
    ):
        offered = registry.for_run(
            run_type,
            tier=GET_STATUS.min_tier,
            enabled_flags=flags,
            managed_consent=False,
        )
        assert ("get_preferences" in offered) is expected
