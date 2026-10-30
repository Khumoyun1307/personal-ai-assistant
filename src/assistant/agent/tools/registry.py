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
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.agent.citations import CitableMessage
from assistant.core.channel import ControlChannel
from assistant.core.clock import Clock
from assistant.llm.port import ToolSpec

if TYPE_CHECKING:
    from assistant.actions.config import ActionsConfig
    from assistant.actions.ports import ApprovalCardPresenter
    from assistant.agent.drafting import Drafter  # type: ignore[import-untyped, unused-ignore]
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
    drafter: Drafter | None = None
    approval_cards: ApprovalCardPresenter | None = None
    actions_config: ActionsConfig | None = None
    notifications_enabled: bool = False
    #: The run's enabled feature flags, for a tool that must mirror another
    #: tool's release flag (ICR-2: attention v2 offers `draft_reply` only
    #: with `tool.propose_reply` on). Empty by default, so it fails closed.
    enabled_flags: frozenset[str] = frozenset()


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
class OwnerTurn:
    """The owner's control-chat turn this run answers, as the runtime loaded
    it (PF-11, owner decision D2 of 2026-10-30). The application, not the
    model, supplies the owner's own words to a draft: a model-written
    paraphrase from a tainted run may restate other people's text (R2), the
    owner's own message cannot. In memory only; never logged or persisted."""

    id: int
    text: str | None
    #: The turn carries third-party text: forwarded (then `text` is someone
    #: else's words) or quoting (`quoted`) — `control_turns.third_party`.
    third_party: bool
    #: The turn forwards or quotes managed-chat content.
    contains_managed: bool
    #: The owner quoted or replied to someone else's message alongside their
    #: own words (`control_turns.quoted_text` is set).
    quoted: bool

    @property
    def forwarded(self) -> bool:
        """`text` itself is someone else's (read as `render_turn` does)."""
        return self.third_party and not self.quoted

    @property
    def own_words(self) -> str | None:
        """What the owner typed themselves: never a forwarded text. A quote
        sits apart (`quoted_text`), so the turn's own text is still theirs."""
        if self.forwarded or self.text is None:
            return None
        return self.text.strip() or None

    @property
    def instruction(self) -> str | None:
        """The owner's words as an instruction for a draft, even in a tainted
        run: only from a turn that carries nothing third-party alongside — not
        forwarded, not quoting, no managed content (invariant 3b)."""
        if self.third_party or self.quoted or self.contains_managed:
            return None
        return self.own_words


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
    #: A held call the owner confirmed: the newest control turn at the
    #: Confirm tap (an id). A deletion clears the owner's own turns only up to
    #: it, so a question sent after the tap keeps its text (M03 D88). None:
    #: all of them (a job re-created by the reconciler; the runtime then asks
    #: the owner to send an emptied question again).
    turn_cutoff: int | None = None
    #: The owner turn this run answers (retried-turn proposal idempotency).
    turn_id: int | None = None
    #: That turn's words and flags (PF-11): None outside an owner run (a held
    #: call applied after the owner's tap, a unit test).
    owner_turn: OwnerTurn | None = None


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
    #: The messages this result shows, by `message_id`, for verified
    #: citations in the reply (S03.7 B3, `agent/citations.py`). Read from the
    #: database by the tool; never persisted, never sent to the model.
    citable: Mapping[int, CitableMessage] = field(default_factory=dict)
    #: For the executor's one log line when a call does not end `ok` (§8.3):
    #: a fixed code (`not_found`, `hold:args_too_large`, …) and content-free
    #: extras (real field names, pydantic error types, an exception type).
    #: Never sent to the model, never persisted.
    log_code: str | None = None
    log_detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.status == "error"

    @property
    def taints(self) -> bool:
        return self.contains_managed if self.untrusted is None else self.untrusted


ToolHandler = Callable[[ToolContext, Any], Awaitable[ToolOutcome]]

#: A chat-reading tool offered while `ai_managed` is off (S03.7 B5, D104):
#: the status its blocked stub returns, and the fixed reason the model gets.
CHAT_ACCESS_OFF = "chat_access_off"
CHAT_ACCESS_OFF_REASON = (
    "AI for the owner's connected chats is off, so their messages cannot be read. "
    "Nothing was searched or read. Do not guess what the chats say."
)


async def _chat_access_off(_ctx: ToolContext, _args: Any) -> ToolOutcome:
    """The blocked stub's handler: reads nothing, returns no message data."""
    return ToolOutcome(CHAT_ACCESS_OFF, {"reason": CHAT_ACCESS_OFF_REASON})


#: For a confirmation card: what a held call's ids refer to, in plain text
#: for the owner (the note's words, the chat's contact name). `NotInAccountError`
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
        stub_managed_tools: bool = False,
    ) -> dict[str, ToolDefinition]:
        """The tools this run may see and call, by name.

        Contact-triggered run types get nothing. A tool that returns managed
        content is omitted unless `ai_managed` is active (ARCHITECTURE §14.3)
        — or, with `stub_managed_tools`, offered under its own name and schema
        as a blocked stub that reads nothing and returns `chat_access_off`
        (S03.7 B5): the model learns the chats are off instead of answering
        blind. Release flags, run types and tiers filter stubs the same way.
        """
        if run_type not in TOOL_RUN_TYPES:
            return {}
        offered: dict[str, ToolDefinition] = {}
        for tool in self._tools.values():
            if not (
                run_type in tool.allowed_run_types
                and tier >= tool.min_tier
                and tool.release_flag in enabled_flags
            ):
                continue
            if managed_consent or not tool.returns_managed:
                offered[tool.name] = tool
            elif stub_managed_tools:
                offered[tool.name] = blocked_stub(tool)
        return offered


def blocked_stub(tool: ToolDefinition) -> ToolDefinition:
    """`tool` with nothing behind it: same name, description and argument
    schema (the model sees the tool it would see with consent), a handler
    that opens no session, and no managed content (§6 invariant 9)."""
    return replace(tool, handler=_chat_access_off, returns_managed=False, describe=None)


def default_registry() -> ToolRegistry:
    """Every tool the product has. The catalogue is ARCHITECTURE §8.2."""
    from assistant.agent.tools.activity import GET_ACTIVITY
    from assistant.agent.tools.autonomy import PAUSE_AUTONOMY, RESUME_AUTONOMY
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
    from assistant.agent.tools.monitors import CREATE_MONITOR, LIST_MONITORS, STOP_MONITOR
    from assistant.agent.tools.priority import SET_CONTACT_PRIORITY
    from assistant.agent.tools.replies import PROPOSE_MESSAGE, PROPOSE_REPLY
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
            GET_ACTIVITY,
            REMEMBER,
            FORGET,
            LIST_MEMORIES,
            UPDATE_PREFERENCES,
            SET_CONVERSATION_MODE,
            DELETE_CONVERSATION_DATA,
            DELETE_CONTACT_DATA,
            DELETE_EVERYTHING,
            SET_CONTACT_PRIORITY,
            CREATE_MONITOR,
            LIST_MONITORS,
            STOP_MONITOR,
            PROPOSE_REPLY,
            PROPOSE_MESSAGE,
            PAUSE_AUTONOMY,
            RESUME_AUTONOMY,
        ]
    )


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A JSON schema that satisfies OpenAI strict mode.

    Strict mode needs `additionalProperties: false` on every object and every
    property listed as required (optional values are expressed as nullable).
    """
    schema = model.model_json_schema()
    _make_strict(schema)
    definitions = schema.get("$defs", {})
    for definition in definitions.values():
        _make_strict(definition)
    _bare_refs(schema, definitions)
    return schema


def _bare_refs(node: Any, definitions: dict[str, Any]) -> None:
    """Strict mode rejects a `$ref` with sibling keywords (400
    `invalid_function_parameters`), which Pydantic emits for a described
    nested model; the description moves to the definition when it has none."""
    if isinstance(node, list):
        for item in node:
            _bare_refs(item, definitions)
        return
    if not isinstance(node, dict):
        return
    if "$ref" in node:
        siblings = {key: node.pop(key) for key in list(node) if key != "$ref"}
        target = definitions.get(node["$ref"].rsplit("/", 1)[-1])
        if target is not None and "description" in siblings:
            target.setdefault("description", siblings["description"])
    for value in node.values():
        _bare_refs(value, definitions)


def _make_strict(node: dict[str, Any]) -> None:
    node.pop("title", None)
    if node.get("type") == "object":
        properties = node.setdefault("properties", {})
        node["additionalProperties"] = False
        node["required"] = list(properties)
        for child in properties.values():
            if isinstance(child, dict):
                _make_strict(child)
