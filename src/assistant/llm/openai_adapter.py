"""OpenAI adapter for the `LLMClient` port (ADR-07a).

Uses the official `openai` SDK's Responses API (AGENTS §8). Every SDK call
shape used here was checked against the installed SDK (3.35.1) and the
official docs on 2026-10-18; see M01.md for the list.

Provider rules this module enforces, so nothing above it has to know them:

- **`store=False` on every request.** The API stores responses for 30 days
  when `store` is omitted (TF §7.2). `_request_kwargs` is the only place that
  builds a request, and a unit test asserts the flag on its output.
- **Stateless multi-turn.** With `store=False` the model's previous output
  items are sent back in `input`. Reasoning items need
  `include=["reasoning.encrypted_content"]` to be passed back (SDK docstring).
  Never `previous_response_id`, which only works with stored responses.
- **Stop reason before content.** `status`/`incomplete_details` and refusals
  are checked before any text or tool call is read (AGENTS §8).
- **Errors are classified here** into `LLMErrorKind`. The exception message is
  never logged or forwarded, because it can echo request content.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import openai
import pydantic
from openai import AsyncOpenAI
from pydantic import BaseModel

from assistant.llm.port import (
    InputItem,
    LLMError,
    LLMErrorKind,
    LLMRequest,
    LLMResponse,
    ParsedResponse,
    ProviderItems,
    StopReason,
    TextItem,
    ToolCall,
    ToolResultItem,
    ToolSpec,
    Usage,
)
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

PROVIDER = "openai"
#: The processor name the consent text uses (handlers.AI_PROCESSORS).
PROCESSOR = "OpenAI"


class OpenAIClient:
    """`LLMClient` over `AsyncOpenAI`. The SDK client is injected by bootstrap."""

    processor = PROCESSOR

    def __init__(self, client: AsyncOpenAI) -> None:
        self._client = client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        try:
            response = await self._client.responses.create(**_request_kwargs(request))
        except openai.OpenAIError as error:
            raise _classify(error) from None
        return _to_response(response)

    async def parse[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> ParsedResponse[T]:
        if request.tools:
            raise ValueError("structured-output runs have no tools (§6 invariant 3a)")
        try:
            response = await self._client.responses.parse(
                text_format=schema, **_request_kwargs(request)
            )
        except pydantic.ValidationError:
            # The SDK parses inside the call; truncated or malformed JSON lands
            # here. Invalid output never triggers anything (AGENTS §8).
            raise LLMError(LLMErrorKind.INVALID_OUTPUT) from None
        except openai.OpenAIError as error:
            raise _classify(error) from None

        stop = _stop_reason(response)
        usage = _usage(response)
        request_id = getattr(response, "_request_id", None)
        if stop is not StopReason.COMPLETED:
            return ParsedResponse(stop, response.model, usage, request_id=request_id)
        parsed = response.output_parsed
        if parsed is None:
            raise LLMError(LLMErrorKind.INVALID_OUTPUT)
        try:
            # Re-validate with our own model: the SDK's parse is a convenience,
            # not the contract (AGENTS §8).
            validated = schema.model_validate(parsed.model_dump())
        except pydantic.ValidationError:
            raise LLMError(LLMErrorKind.INVALID_OUTPUT) from None
        return ParsedResponse(stop, response.model, usage, validated, request_id)


# ------------------------------------------------------------------ requests
def _request_kwargs(request: LLMRequest) -> dict[str, Any]:
    """The one place a request body is built. `store` is always False."""
    kwargs: dict[str, Any] = {
        "model": request.model,
        "instructions": request.instructions,
        "input": [entry for item in request.items for entry in _input_entries(item)],
        "max_output_tokens": request.max_output_tokens,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "timeout": request.timeout_seconds,
    }
    if request.tools:
        kwargs["tools"] = [_tool_param(tool) for tool in request.tools]
    if request.effort is not None:
        kwargs["reasoning"] = {"effort": request.effort}
    if request.prompt_cache_key is not None:
        kwargs["prompt_cache_key"] = request.prompt_cache_key
    return kwargs


def _input_entries(item: InputItem) -> list[Mapping[str, Any]]:
    match item:
        case TextItem(role=role, text=text):
            return [{"role": role, "content": text}]
        case ToolResultItem(call_id=call_id, output=output):
            return [{"type": "function_call_output", "call_id": call_id, "output": output}]
        case ProviderItems(provider=provider, payload=payload):
            if provider != PROVIDER:
                raise ValueError(f"cannot replay {provider} items through the OpenAI adapter")
            return list(payload)
    raise TypeError(f"unknown input item {type(item).__name__}")


def _tool_param(tool: ToolSpec) -> dict[str, Any]:
    # Strict mode: the schema must set additionalProperties=false and list
    # every property as required (function-calling guide). The registry
    # produces schemas that satisfy this; strict makes the API enforce it.
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": dict(tool.parameters),
        "strict": True,
    }


# ----------------------------------------------------------------- responses
def _to_response(response: Any) -> LLMResponse:
    stop = _stop_reason(response)
    usage = _usage(response)
    request_id = getattr(response, "_request_id", None)
    items = ProviderItems(
        provider=PROVIDER,
        payload=tuple(
            item.model_dump(mode="json", exclude_none=True, by_alias=True)
            for item in response.output
        ),
    )
    if stop is not StopReason.COMPLETED:
        return LLMResponse(stop, response.model, usage, request_id=request_id)

    calls = tuple(
        ToolCall(call_id=item.call_id, name=item.name, arguments=item.arguments)
        for item in response.output
        if item.type == "function_call"
    )
    if calls:
        return LLMResponse(
            StopReason.TOOL_CALLS,
            response.model,
            usage,
            tool_calls=calls,
            output_items=items,
            request_id=request_id,
        )
    return LLMResponse(
        StopReason.COMPLETED,
        response.model,
        usage,
        text=response.output_text,
        output_items=items,
        request_id=request_id,
    )


def _stop_reason(response: Any) -> StopReason:
    """Status first, then refusals; only a clean completion exposes content."""
    if response.status == "incomplete":
        reason = getattr(response.incomplete_details, "reason", None)
        if reason == "max_output_tokens":
            return StopReason.MAX_TOKENS
        if reason == "content_filter":
            return StopReason.CONTENT_FILTER
        return StopReason.OTHER
    if response.status != "completed":
        return StopReason.OTHER
    for item in response.output:
        if item.type == "message" and any(part.type == "refusal" for part in item.content):
            return StopReason.REFUSAL
    return StopReason.COMPLETED


def _usage(response: Any) -> Usage:
    usage = response.usage
    if usage is None:
        return Usage()
    details = usage.input_tokens_details
    return Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=getattr(details, "cached_tokens", 0) or 0,
        cache_write_tokens=getattr(details, "cache_write_tokens", 0) or 0,
    )


# -------------------------------------------------------------------- errors
def _classify(error: openai.OpenAIError) -> LLMError:
    """Typed SDK errors → our kinds. Never `str(error)`: it can echo content."""
    code = getattr(error, "code", None)
    code = code if isinstance(code, str) else None
    # APITimeoutError is an APIConnectionError; 5xx statuses are the provider's
    # problem, other statuses (400, 404, 422) are ours.
    if isinstance(error, openai.RateLimitError):
        kind = LLMErrorKind.RATE_LIMITED
    elif isinstance(error, (openai.AuthenticationError, openai.PermissionDeniedError)):
        kind = LLMErrorKind.AUTH
    elif isinstance(error, openai.APIStatusError) and error.status_code < 500:
        kind = LLMErrorKind.BAD_REQUEST
    else:
        kind = LLMErrorKind.UNAVAILABLE
    logger.warning("llm_call_failed", exc_type=type(error).__name__, kind=kind.value, code=code)
    return LLMError(kind, code=code)
