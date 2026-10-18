"""Tool registry invariants (ARCHITECTURE §8, AGENTS §8).

RG-17 (§6 invariants 2 and 3a): contact-triggered runs get no tools, and no
registered tool sends, edits, deletes or marks read.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from assistant.agent.models import RUN_TYPES
from assistant.agent.tools.registry import (
    TOOL_RUN_TYPES,
    CapabilityClass,
    CapabilityTier,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRegistrationError,
    ToolRegistry,
    default_registry,
    strict_schema,
)

CONTACT_TRIGGERED = ("triage", "draft_reply", "summarize", "extract_memory", "evaluate_monitor")


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def noop(_ctx: ToolContext, _args: Any) -> ToolOutcome:
    return ToolOutcome("ok")


def tool(name: str = "list_things", **overrides: Any) -> ToolDefinition:
    values: dict[str, Any] = {
        "name": name,
        "description": "d",
        "args_model": NoArgs,
        "capability": CapabilityClass.READ,
        "handler": noop,
        "release_flag": f"tool.{name}",
    }
    values.update(overrides)
    return ToolDefinition(**values)


def every_flag(registry: ToolRegistry) -> frozenset[str]:
    return frozenset(t.release_flag for t in registry.all())


def test_rg17_contact_triggered_runs_get_no_tools() -> None:
    registry = default_registry()

    for run_type in CONTACT_TRIGGERED:
        offered = registry.for_run(
            run_type,
            tier=CapabilityTier.CONTROL_ONLY,
            enabled_flags=every_flag(registry),
            managed_consent=True,
        )
        assert offered == {}, run_type
    assert set(CONTACT_TRIGGERED) <= set(RUN_TYPES)


def test_rg17_no_registered_tool_sends_edits_deletes_or_marks_read() -> None:
    for definition in default_registry().all():
        assert definition.capability in set(CapabilityClass)
        assert definition.allowed_run_types <= TOOL_RUN_TYPES
        for verb in ("send", "edit", "delete_message", "mark_read", "reply"):
            assert not definition.name.startswith(verb), definition.name
    assert {member.value for member in CapabilityClass} == {
        "read",
        "propose",
        "configure",
        "control",
    }


@pytest.mark.parametrize(
    "name", ["send_message", "edit_message", "delete_message", "mark_read", "reply_to"]
)
def test_rg17_a_send_like_tool_cannot_be_registered(name: str) -> None:
    with pytest.raises(ToolRegistrationError):
        ToolRegistry([tool(name)])


def test_tools_cannot_be_given_to_contact_triggered_run_types() -> None:
    with pytest.raises(ToolRegistrationError, match="owner_request"):
        ToolRegistry([tool(allowed_run_types=frozenset({"owner_request", "triage"}))])


def test_a_release_flag_is_mandatory_and_off_means_not_offered() -> None:
    with pytest.raises(ToolRegistrationError, match="release flag"):
        ToolRegistry([tool(release_flag="")])

    registry = ToolRegistry([tool()])
    off = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=frozenset(),
        managed_consent=True,
    )
    on = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=frozenset({"tool.list_things"}),
        managed_consent=True,
    )
    assert off == {}
    assert set(on) == {"list_things"}


def test_tools_returning_managed_content_need_managed_consent() -> None:
    registry = ToolRegistry([tool(returns_managed=True)])
    flags = frozenset({"tool.list_things"})

    without = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=flags,
        managed_consent=False,
    )
    with_consent = registry.for_run(
        "owner_request", tier=CapabilityTier.CONTROL_ONLY, enabled_flags=flags, managed_consent=True
    )

    assert without == {}
    assert set(with_consent) == {"list_things"}


def test_duplicate_names_are_refused() -> None:
    with pytest.raises(ToolRegistrationError, match="duplicate"):
        ToolRegistry([tool(), tool()])


class Nested(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int | None
    query: str


def test_schemas_satisfy_openai_strict_mode() -> None:
    schema = strict_schema(Nested)

    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == ["limit", "query"]
    status_schema = default_registry().all()[0].spec().parameters
    assert status_schema["additionalProperties"] is False
    assert status_schema["required"] == []
