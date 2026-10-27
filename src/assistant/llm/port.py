"""The `LLMClient` port: provider-neutral types (ARCHITECTURE §7.2, §7.4, ADR-07a).

The agent depends on this module only. Nothing here knows which provider is
behind it, so swapping OpenAI for another adapter changes `bootstrap.py`, not
the agent loop.

Two properties are carried on every request because the wrappers around the
adapter need them, not the provider:

- **`account_id`** — consent is per account (§6 invariant 9) and so is metering.
- **`provenance` on every content item** — the consent wrapper decides from it
  whether the request contains managed-conversation content (ARCHITECTURE
  §14.3). A content item without provenance cannot be built.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

from pydantic import BaseModel


class Provenance(StrEnum):
    """Where a piece of request content came from."""

    SYSTEM = "system"
    """Our own instructions and tool schemas."""

    OWNER_CONTROL = "owner_control"
    """The owner's control-chat text, or assistant replies to it."""

    MANAGED = "managed"
    """Anything derived from a managed (business) conversation: contact
    messages, contact names, summaries. Needs `ai_managed` consent."""

    TOOL_RESULT = "tool_result"
    """A tool result with no managed content (e.g. `get_status`)."""


@dataclass(frozen=True, slots=True)
class TextItem:
    """A user or assistant text turn rebuilt from domain data."""

    role: Literal["user", "assistant"]
    text: str
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class ToolResultItem:
    """The result of one tool call, sent back to the model."""

    call_id: str
    output: str
    provenance: Provenance


@dataclass(frozen=True, slots=True)
class ProviderItems:
    """Model output passed back unchanged within a run (append-only, §7.2).

    Opaque on purpose: the agent never looks inside, it only appends. The
    `provider` tag stops one adapter's items reaching another adapter.
    """

    provider: str
    payload: tuple[Mapping[str, Any], ...]
    provenance: Provenance = Provenance.OWNER_CONTROL


InputItem = TextItem | ToolResultItem | ProviderItems


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A function the model may call. `parameters` is a strict JSON schema."""

    name: str
    description: str
    parameters: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LLMRequest:
    """One provider call."""

    account_id: int
    #: The run type or other purpose label, for metering (`llm_calls.purpose`).
    purpose: str
    model: str
    instructions: str
    items: tuple[InputItem, ...]
    max_output_tokens: int
    timeout_seconds: float
    tools: tuple[ToolSpec, ...] = ()
    #: Reasoning effort, or None to send no effort at all.
    effort: str | None = None
    #: A stable, content-free key that groups requests sharing a prefix.
    prompt_cache_key: str | None = None
    run_id: int | None = None

    def contains_managed(self) -> bool:
        return any(item.provenance is Provenance.MANAGED for item in self.items)


class StopReason(StrEnum):
    """Why the model stopped. Always checked before reading content (AGENTS §8)."""

    COMPLETED = "completed"
    TOOL_CALLS = "tool_calls"
    REFUSAL = "refusal"
    MAX_TOKENS = "max_tokens"
    CONTENT_FILTER = "content_filter"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    #: Raw JSON from the model. Untrusted until the tool's model validates it.
    arguments: str


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class LLMResponse:
    stop_reason: StopReason
    model: str
    usage: Usage
    text: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    #: The model output to append, unchanged, if the run continues.
    output_items: ProviderItems | None = None
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class ParsedResponse[T: BaseModel]:
    """A structured-output result. `parsed` is None unless `stop_reason` is COMPLETED."""

    stop_reason: StopReason
    model: str
    usage: Usage
    parsed: T | None = None
    request_id: str | None = None


class LLMErrorKind(StrEnum):
    UNAVAILABLE = "unavailable"
    """Connection failure, timeout or a provider 5xx: say so, try later."""

    RATE_LIMITED = "rate_limited"
    AUTH = "auth"
    """Bad or missing key, or no access to the model: an operator problem."""

    BAD_REQUEST = "bad_request"
    """The provider rejected what we sent: our bug, not the owner's."""

    INVALID_OUTPUT = "invalid_output"
    """Structured output did not validate against our own model."""


class LLMError(Exception):
    """A classified provider failure. Carries no request or response content."""

    def __init__(
        self, kind: LLMErrorKind, *, code: str | None = None, exc_type: str | None = None
    ) -> None:
        super().__init__(kind.value if code is None else f"{kind.value}:{code}")
        self.kind = kind
        self.code = code
        #: The provider SDK's exception class name (e.g. `APITimeoutError`), when
        #: an adapter classified one: content-free, for diagnosis only.
        self.exc_type = exc_type


@dataclass(frozen=True, slots=True)
class CallRecord:
    """What metering writes to `llm_calls` — ids and numbers, no content."""

    account_id: int
    run_id: int | None
    purpose: str
    model: str
    usage: Usage
    request_id: str | None


class LLMClient(Protocol):
    """The port the agent calls. Implemented by adapters and wrappers."""

    #: The processor name consent must list (SEC-02), e.g. "OpenAI".
    processor: str

    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def parse[T: BaseModel](
        self, request: LLMRequest, schema: type[T]
    ) -> ParsedResponse[T]: ...
