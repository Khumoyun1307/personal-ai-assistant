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
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.core.channel import ControlChannel
from assistant.core.clock import Clock
from assistant.llm.port import ToolSpec

if TYPE_CHECKING:
    from assistant.agent.summarize import Summarizer


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


#: Control calls that only reduce autonomy: they run even in a tainted run
#: (ARCHITECTURE §7.1, §8.1). Names reserved for M04 to M06 tools.
TAINT_EXEMPT_TOOLS = frozenset({"pause_autonomy", "end_delegation", "cancel_action"})
#: Never executed from a model call, tainted or not: only `/resume` or a card
#: button resumes autonomy (§7.1).
CARD_ONLY_TOOLS = frozenset({"resume_autonomy"})


@dataclass(frozen=True, slots=True)
class ToolServices:
    """Shared dependencies handed to tool handlers."""

    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    monthly_budget_usd: Decimal | None
    #: The owner's control chat, for confirmation cards (None in unit tests
    #: that never reach the confirmation gate).
    channel: ControlChannel | None = None
    #: The `summarize` run type (S03.2), for catch-up and attention tools;
    #: None where no summary can be made (those tools then say so).
    summarizer: Summarizer | None = None


@dataclass(frozen=True, slots=True)
class ToolRefs:
    """Which conversations and contacts a result drew on. Written to the
    assistant turn's `control_turns.refs`, so deleting a conversation or a
    contact can find the turns that quoted it (ARCHITECTURE §10.4)."""

    conversation_ids: frozenset[int] = frozenset()
    contact_ids: frozenset[int] = frozenset()

    def __or__(self, other: ToolRefs) -> ToolRefs:
        return ToolRefs(
            self.conversation_ids | other.conversation_ids, self.contact_ids | other.contact_ids
        )

    def as_json(self) -> dict[str, list[int]]:
        return {
            "conversation_ids": sorted(self.conversation_ids),
            "contact_ids": sorted(self.contact_ids),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any] | None) -> ToolRefs:
        """The `control_turns.refs` shape back; anything else counts as empty."""
        if not value:
            return cls()

        def ids(key: str) -> frozenset[int]:
            raw = value.get(key)
            if not isinstance(raw, list):
                return frozenset()
            return frozenset(item for item in raw if isinstance(item, int))

        return cls(ids("conversation_ids"), ids("contact_ids"))


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a tool handler may use. Ids only; the handler loads what it needs."""

    account_id: int
    run_id: int
    run_type: str
    tainted: bool
    services: ToolServices
    #: The model's id for this call: with `run_id`, the idempotency key of a
    #: state change (§8.3 step 4). Empty outside a model call.
    call_id: str = ""
    #: A held call the owner confirmed, run by the applier: it came from a
    #: tainted run whatever the run row says now (fail closed).
    held: bool = False
    #: Conversations and contacts the run's results drew on so far.
    refs: ToolRefs = field(default_factory=ToolRefs)
    #: `ai_managed` is active AND names the processor of this run's model —
    #: the runtime's check (§14.3, M03 D48). Fail closed: False unless the
    #: runtime established it.
    managed_ok: bool = False


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """A tool's factual result (§8.3 step 5). Never claims pending work is done."""

    status: str
    data: Mapping[str, Any] = field(default_factory=dict)
    #: True if `data` holds managed-conversation content: needs `ai_managed`
    #: consent to reach the model (§6 invariant 9).
    contains_managed: bool = False
    #: True if `data` holds contact-authored text or names, which taints the
    #: run (invariant 3b). Defaults to `contains_managed`.
    untrusted: bool | None = None
    refs: ToolRefs = field(default_factory=ToolRefs)

    @property
    def is_error(self) -> bool:
        return self.status == "error"

    @property
    def taints(self) -> bool:
        return self.contains_managed if self.untrusted is None else self.untrusted


ToolHandler = Callable[[ToolContext, Any], Awaitable[ToolOutcome]]
#: For a confirmation card: what a held call's ids refer to, in plain text
#: for the owner (the note's words, the chat's contact name). LookupError
#: for an id that is not the account's. Owner-facing only, never sent to a
#: model (S03.3 safety review S1).
ToolDescriber = Callable[[AsyncSession, int, Mapping[str, Any]], Awaitable[str | None]]


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
    describe: ToolDescriber | None = None
    #: Always a confirmation card, tainted or not: the deletions (S03.5,
    #: §8.1 "deletions require a confirmation card").
    always_confirm: bool = False
    #: A card, tainted or not, for arguments this matches (S03.5: a
    #: retention change, which deletes data at the next sweep).
    confirm_if: Callable[[Any], bool] | None = None

    @property
    def changes_state(self) -> bool:
        return self.capability in (CapabilityClass.CONFIGURE, CapabilityClass.CONTROL)

    def needs_confirmation(self, *, tainted: bool, args: BaseModel | None = None) -> bool:
        """The confirmation gate (§7.1): a model-initiated state change in a
        tainted run becomes a card, except calls that only reduce autonomy;
        `resume_autonomy` and the `always_confirm` tools are a card whatever
        the taint, and so is a call whose arguments `confirm_if` matches."""
        if self.name in CARD_ONLY_TOOLS or self.always_confirm:
            return True
        if self.confirm_if is not None and args is not None and self.confirm_if(args):
            return True
        return tainted and self.changes_state and self.name not in TAINT_EXEMPT_TOOLS

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
        if (tool.always_confirm or tool.confirm_if is not None) and not tool.changes_state:
            raise ToolRegistrationError(f"{tool.name}: only state changes need a card")
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
    from assistant.agent.tools.conversations import (
        GET_CONVERSATION,
        LIST_CONVERSATIONS,
        RESOLVE_CONTACT,
    )
    from assistant.agent.tools.deletion import (
        DELETE_CONTACT_DATA,
        DELETE_CONVERSATION_DATA,
        DELETE_EVERYTHING,
    )
    from assistant.agent.tools.memory import FORGET, LIST_MEMORIES, REMEMBER
    from assistant.agent.tools.modes import SET_CONVERSATION_MODE, UPDATE_PREFERENCES
    from assistant.agent.tools.search import SEARCH_MESSAGES
    from assistant.agent.tools.status import GET_STATUS
    from assistant.agent.tools.summaries import GET_ATTENTION_LIST, SUMMARIZE_PERIOD

    return ToolRegistry(
        [
            GET_STATUS,
            SEARCH_MESSAGES,
            RESOLVE_CONTACT,
            GET_CONVERSATION,
            LIST_CONVERSATIONS,
            SUMMARIZE_PERIOD,
            GET_ATTENTION_LIST,
            REMEMBER,
            FORGET,
            LIST_MEMORIES,
            UPDATE_PREFERENCES,
            SET_CONVERSATION_MODE,
            DELETE_CONVERSATION_DATA,
            DELETE_CONTACT_DATA,
            DELETE_EVERYTHING,
        ]
    )


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
