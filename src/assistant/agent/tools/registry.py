"""Agent tool registry (ARCHITECTURE §8.1 to §8.3, AGENTS §8).

Each tool is registered with: a strict Pydantic argument model, a capability
class, the run types allowed to use it (`owner_request` only), a minimum
capability tier, whether its results carry managed-conversation content, and
a release flag that defaults off.

Two invariants are enforced here, at registration, so a mistake fails at import
rather than in production:

- **No tool sends, edits, deletes or marks read** (§6 invariant 2). There is no
  capability class for it, and names that look like one are refused.
- **Only `owner_request` runs get tools** (§6 invariant 3a). Contact-triggered
  run types can never be listed, and `for_run` returns nothing for them.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.core.clock import Clock
from assistant.llm.port import ToolSpec


class CapabilityClass(StrEnum):
    """What a tool may do. Deliberately has no "send" member (§8.1)."""

    READ = "read"
    PROPOSE = "propose"
    CONFIGURE = "configure"
    CONTROL = "control"


class CapabilityTier(IntEnum):
    """How much of the owner's account the assistant is connected to.

    M01 has only the control chat. Higher tiers (business connection, rights)
    arrive with M02; tools needing them declare a higher minimum.
    """

    CONTROL_ONLY = 0


#: The only run type that may have tools (ARCHITECTURE §7.1).
TOOL_RUN_TYPES = frozenset({"owner_request"})

#: Names that would describe an outbound effect. Outbound effects are
#: `propose_*` tools that create an action for approval, never direct calls.
_FORBIDDEN_NAME = re.compile(r"^(send|edit|delete_message|mark_read|read_message|reply)(_|$)")


@dataclass(frozen=True, slots=True)
class ToolServices:
    """Shared dependencies handed to tool handlers."""

    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    monthly_budget_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a tool handler may use. Ids only; the handler loads what it needs."""

    account_id: int
    run_id: int
    run_type: str
    tainted: bool
    services: ToolServices


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """A tool's factual result (§8.3 step 5). Never claims pending work is done."""

    status: str
    data: Mapping[str, Any] = field(default_factory=dict)
    #: True if `data` holds contact-authored text or names (invariant 3b).
    contains_managed: bool = False

    @property
    def is_error(self) -> bool:
        return self.status == "error"


ToolHandler = Callable[[ToolContext, Any], Awaitable[ToolOutcome]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    args_model: type[BaseModel]
    capability: CapabilityClass
    handler: ToolHandler
    release_flag: str
    allowed_run_types: frozenset[str] = TOOL_RUN_TYPES
    min_tier: CapabilityTier = CapabilityTier.CONTROL_ONLY
    returns_managed: bool = False

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=strict_schema(self.args_model),
        )


class ToolRegistrationError(ValueError):
    """A tool definition breaks a registry invariant."""


class ToolRegistry:
    def __init__(self, tools: Iterable[ToolDefinition] = ()) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ToolRegistrationError(f"duplicate tool {tool.name}")
        if _FORBIDDEN_NAME.match(tool.name):
            raise ToolRegistrationError(
                f"{tool.name}: tools may not send, edit, delete or mark read; use propose_*"
            )
        if not isinstance(tool.capability, CapabilityClass):
            raise ToolRegistrationError(f"{tool.name}: unknown capability class")
        if not tool.allowed_run_types or not tool.allowed_run_types <= TOOL_RUN_TYPES:
            raise ToolRegistrationError(f"{tool.name}: only owner_request runs may have tools")
        if not tool.release_flag:
            raise ToolRegistrationError(f"{tool.name}: a release flag is required")
        self._tools[tool.name] = tool

    def all(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._tools.values())

    def for_run(
        self,
        run_type: str,
        *,
        tier: CapabilityTier,
        enabled_flags: frozenset[str],
        managed_consent: bool,
    ) -> dict[str, ToolDefinition]:
        """The tools this run may see and call, by name.

        Contact-triggered run types get nothing. A tool that returns managed
        content is omitted unless `ai_managed` is active (ARCHITECTURE §14.3).
        """
        if run_type not in TOOL_RUN_TYPES:
            return {}
        return {
            tool.name: tool
            for tool in self._tools.values()
            if run_type in tool.allowed_run_types
            and tier >= tool.min_tier
            and tool.release_flag in enabled_flags
            and (managed_consent or not tool.returns_managed)
        }


def default_registry() -> ToolRegistry:
    """Every tool the product has. The catalogue is ARCHITECTURE §8.2."""
    from assistant.agent.tools.status import GET_STATUS

    return ToolRegistry([GET_STATUS])


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A JSON schema that satisfies OpenAI strict mode.

    Strict mode needs `additionalProperties: false` on every object and every
    property listed as required (optional values are expressed as nullable).
    """
    schema = model.model_json_schema()
    _make_strict(schema)
    for definition in schema.get("$defs", {}).values():
        _make_strict(definition)
    return schema


def _make_strict(node: dict[str, Any]) -> None:
    node.pop("title", None)
    if node.get("type") == "object":
        properties = node.setdefault("properties", {})
        node["additionalProperties"] = False
        node["required"] = list(properties)
        for child in properties.values():
            if isinstance(child, dict):
                _make_strict(child)
