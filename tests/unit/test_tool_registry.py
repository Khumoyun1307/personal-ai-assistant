"""Tool registry invariants (ARCHITECTURE §8, AGENTS §8).

RG-17 (§6 invariants 2 and 3a): contact-triggered runs get no tools, and no
registered tool sends, edits, deletes or marks read.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from assistant.agent import prompt_registry
from assistant.agent.models import RUN_TYPES
from assistant.agent.tools.registry import (
    CHAT_ACCESS_OFF,
    CHAT_ACCESS_OFF_REASON,
    TOOL_RUN_TYPES,
    CapabilityClass,
    CapabilityTier,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRefs,
    ToolRegistrationError,
    ToolRegistry,
    ToolServices,
    default_registry,
    strict_schema,
)
from tests.fakes.clock import FakeClock

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


class _NoDatabase:
    """A session factory that fails the test if anything opens a session."""

    def __call__(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the blocked stub opened a database session")


def _stub_context() -> ToolContext:
    return ToolContext(
        account_id=1,
        run_id=1,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(
            session_factory=_NoDatabase(),  # type: ignore[arg-type]
            clock=FakeClock(),
            monthly_budget_usd=None,
        ),
    )


async def test_the_blocked_stub_returns_no_message_data_and_makes_no_db_read_of_content() -> None:
    """S03.7 B5 (invariant 9): without `ai_managed`, a chat-reading tool is
    offered as a stub with the same name and schema; its handler opens no
    session and returns only the fixed `chat_access_off` status."""
    registry = default_registry()
    flags = every_flag(registry)
    stubs = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=flags,
        managed_consent=False,
        stub_managed_tools=True,
    )
    real = {tool.name: tool for tool in registry.all()}
    managed = {name for name, tool in real.items() if tool.returns_managed}
    assert managed == {
        "search_messages",
        "resolve_contact",
        "get_conversation",
        "list_conversations",
        "summarize_period",
        "get_attention_list",
    }
    assert managed <= set(stubs)
    for name in managed:
        stub = stubs[name]
        assert stub.spec() == real[name].spec()  # the model sees the same tool
        assert stub.handler is not real[name].handler
        assert stub.returns_managed is False
        outcome = await stub.handler(_stub_context(), None)
        assert outcome.status == CHAT_ACCESS_OFF
        assert dict(outcome.data) == {"reason": CHAT_ACCESS_OFF_REASON}
        assert outcome.contains_managed is False
        assert outcome.taints is False
        assert outcome.refs == ToolRefs()
        assert not outcome.citable
    # Tools that do not read the chats are offered as they are.
    for name in set(stubs) - managed:
        assert stubs[name] is real[name]


def test_release_flags_still_hide_blocked_stubs() -> None:
    registry = ToolRegistry([tool(returns_managed=True)])

    flagged_off = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=frozenset(),
        managed_consent=False,
        stub_managed_tools=True,
    )
    with_consent = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=frozenset({"tool.list_things"}),
        managed_consent=True,
        stub_managed_tools=True,
    )
    contact_run = registry.for_run(
        "summarize",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=frozenset({"tool.list_things"}),
        managed_consent=False,
        stub_managed_tools=True,
    )

    assert flagged_off == {}
    assert with_consent["list_things"].handler is noop  # consent: the real tool
    assert contact_run == {}


def test_owner_request_v7_is_active_and_v1_to_v6_remain() -> None:
    assert prompt_registry.ACTIVE["owner_request"] == "v7"
    for version in ("v1", "v2", "v3", "v4", "v5", "v6", "v7"):
        assert prompt_registry.load("owner_request", version).text
    v6 = prompt_registry.load("owner_request", "v6").text
    assert "[msg:" in v6
    assert "chat_access_off" in v6
    assert "widened" in v6


def _rule(prompt: str, number: int) -> str:
    """Rule `number` of a prompt (up to the next numbered rule)."""
    start = prompt.index(f"\n{number}. ")
    end = prompt.find(f"\n{number + 1}. ", start)
    return prompt[start : end if end != -1 else None]


def test_owner_request_v7_is_v6_plus_the_search_and_language_rules() -> None:
    """S03.7 fix round (owner decisions 1-3, 2026-10-26): a bare phrase is a
    search of the owner's chats; a name that is no contact is searched for; an
    empty narrowed search reads the chat before saying "not there"; the whole
    reply in the owner's language and script, values copied as written."""
    v6 = prompt_registry.load("owner_request", "v6").text
    v7 = prompt_registry.load("owner_request", "v7").text
    assert v7.split("\n1. ")[0] == v6.split("\n1. ")[0]
    for number in range(1, 18):
        if number == 16:  # rewritten
            continue
        if number in (5, 6):  # v6 word for word, with additions
            v7_lines = _rule(v7, number).splitlines()
            for line in _rule(v6, number).splitlines():
                assert any(new.startswith(line) for new in v7_lines), (number, line)
            continue
        assert _rule(v7, number) == _rule(v6, number), number
    rule5 = _rule(v7, 5)
    assert "only a name, a number, an address, a phrase or a few keywords" in rule5
    assert "the owner's own words, exactly as written, as the first phrase" in rule5
    assert "something to remember, a message to draft or send, or a general-knowledge" in rule5
    assert "If `resolve_contact` finds no contact" in rule5
    assert "read that chat with `get_conversation` before saying it is not there" in rule5
    assert "exactly as the message writes them" in _rule(v7, 6)
    rule16 = _rule(v7, 16)
    assert "whole reply" in rule16
    assert "script" in rule16


def test_duplicate_names_are_refused() -> None:
    with pytest.raises(ToolRegistrationError, match="duplicate"):
        ToolRegistry([tool(), tool()])


class Nested(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int | None
    query: str


#: Keywords kept out of tool schemas: constraints are validated by our own
#: Pydantic validators, so the schema stays within plain strict mode; no
#: `default` either (D52: every field required and nullable).
_UNUSED_KEYWORDS = {
    "format",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "pattern",
    "default",
}


def _keywords(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for value in node.values() for k in _keywords(value)}
    if isinstance(node, list):
        return {k for value in node for k in _keywords(value)}
    return set()


def test_every_registered_tool_schema_is_strict() -> None:
    for definition in default_registry().all():
        schema = definition.spec().parameters
        assert schema["additionalProperties"] is False, definition.name
        assert sorted(schema["required"]) == sorted(schema["properties"]), definition.name
        assert not _keywords(schema) & _UNUSED_KEYWORDS, definition.name


def test_the_confirmation_gate_covers_state_changes_in_tainted_runs_only() -> None:
    configure = tool("set_thing", capability=CapabilityClass.CONFIGURE)
    control = tool("stop_thing", capability=CapabilityClass.CONTROL)
    read = tool()
    assert configure.needs_confirmation(tainted=True)
    assert control.needs_confirmation(tainted=True)
    assert not configure.needs_confirmation(tainted=False)
    assert not read.needs_confirmation(tainted=True)
    # Calls that only reduce autonomy run even when tainted (§7.1)...
    for name in ("pause_autonomy", "end_delegation", "cancel_action"):
        assert not tool(name, capability=CapabilityClass.CONTROL).needs_confirmation(tainted=True)
    # ...and resume_autonomy never runs from a model call.
    resume = tool("resume_autonomy", capability=CapabilityClass.CONTROL)
    assert resume.needs_confirmation(tainted=False)


def test_schemas_satisfy_openai_strict_mode() -> None:
    schema = strict_schema(Nested)

    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == ["limit", "query"]
    status_schema = default_registry().all()[0].spec().parameters
    assert status_schema["additionalProperties"] is False
    assert status_schema["required"] == []
