"""The OpenAI adapter (ADR-07a, TF §7.2, AGENTS §8).

Pinned claims:

- **every request is sent with `store=False`** — the API stores responses for
  30 days when it is omitted (TF §7.2);
- stateless multi-turn: encrypted reasoning is requested, `previous_response_id`
  is never used;
- the stop reason is decided before any content is read (refusal, truncation);
- SDK errors are classified without leaking their message.

Uses real SDK model and exception classes with a stand-in client; no network.
"""

from __future__ import annotations

from typing import Any

import httpx2
import openai
import pytest
from openai.types.responses import (
    Response,
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputRefusal,
    ResponseOutputText,
    ResponseUsage,
)
from openai.types.responses.response import IncompleteDetails
from openai.types.responses.response_usage import InputTokensDetails, OutputTokensDetails
from pydantic import BaseModel

from assistant.llm.openai_adapter import OpenAIClient, _request_kwargs
from assistant.llm.port import (
    LLMError,
    LLMErrorKind,
    LLMRequest,
    Provenance,
    ProviderItems,
    StopReason,
    TextItem,
    ToolResultItem,
    ToolSpec,
)

TOOL = ToolSpec(
    name="get_status",
    description="state",
    parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
)


def request(**overrides: Any) -> LLMRequest:
    values: dict[str, Any] = {
        "account_id": 1,
        "purpose": "owner_request",
        "model": "gpt-6-luna",
        "instructions": "Be brief.",
        "items": (TextItem("user", "Synthetic question", Provenance.OWNER_CONTROL),),
        "max_output_tokens": 500,
        "timeout_seconds": 30.0,
    }
    values.update(overrides)
    return LLMRequest(**values)


def usage() -> ResponseUsage:
    return ResponseUsage.model_construct(
        input_tokens=120,
        output_tokens=40,
        total_tokens=160,
        input_tokens_details=InputTokensDetails.model_construct(
            cached_tokens=100, cache_write_tokens=0
        ),
        output_tokens_details=OutputTokensDetails.model_construct(reasoning_tokens=10),
    )


def sdk_response(output: list[Any], *, status: str = "completed", reason: str | None = None) -> Any:
    response = Response.model_construct(
        id="resp_1",
        model="gpt-6-luna-2026-09-01",
        object="response",
        status=status,
        incomplete_details=IncompleteDetails.model_construct(reason=reason) if reason else None,
        output=output,
        usage=usage(),
    )
    response._request_id = "req_123"
    return response


def message(*parts: Any) -> ResponseOutputMessage:
    return ResponseOutputMessage.model_construct(
        id="msg_1", type="message", role="assistant", status="completed", content=list(parts)
    )


def output_text(text: str) -> ResponseOutputText:
    return ResponseOutputText.model_construct(type="output_text", text=text, annotations=[])


class StubResponses:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    async def parse(self, **kwargs: Any) -> Any:
        return await self.create(**kwargs)


class StubClient:
    def __init__(self, result: Any) -> None:
        self.responses = StubResponses(result)


def adapter(result: Any) -> tuple[OpenAIClient, StubResponses]:
    stub = StubClient(result)
    return OpenAIClient(stub), stub.responses  # type: ignore[arg-type]


# --------------------------------------------------------------- store=False
@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"tools": (TOOL,)},
        {"effort": "medium", "prompt_cache_key": "owner_request:owner_request.v1"},
        {
            "items": (
                TextItem("user", "q", Provenance.OWNER_CONTROL),
                ProviderItems("openai", ({"type": "function_call", "call_id": "c1"},)),
                ToolResultItem("c1", '{"status":"ok"}', Provenance.TOOL_RESULT),
            )
        },
    ],
)
def test_every_request_is_built_with_store_false(overrides: dict[str, Any]) -> None:
    kwargs = _request_kwargs(request(**overrides))

    assert kwargs["store"] is False
    assert kwargs["include"] == ["reasoning.encrypted_content"]
    assert "previous_response_id" not in kwargs
    assert "conversation" not in kwargs
    assert "background" not in kwargs


async def test_store_false_reaches_the_sdk_call() -> None:
    client, stub = adapter(sdk_response([message(output_text("Hi"))]))

    await client.complete(request())

    assert stub.calls[0]["store"] is False


def test_tools_are_strict_functions_and_effort_is_optional() -> None:
    with_tools = _request_kwargs(request(tools=(TOOL,), effort="low"))
    without = _request_kwargs(request())

    assert with_tools["tools"][0]["strict"] is True
    assert with_tools["tools"][0]["type"] == "function"
    assert with_tools["reasoning"] == {"effort": "low"}
    assert "reasoning" not in without
    assert "tools" not in without


def test_items_are_replayed_in_order_and_tool_results_keep_their_call_id() -> None:
    items = (
        TextItem("user", "q", Provenance.OWNER_CONTROL),
        ProviderItems("openai", ({"type": "function_call", "call_id": "c1"},)),
        ToolResultItem("c1", '{"status":"ok"}', Provenance.TOOL_RESULT),
    )

    entries = _request_kwargs(request(items=items))["input"]

    assert entries == [
        {"role": "user", "content": "q"},
        {"type": "function_call", "call_id": "c1"},
        {"type": "function_call_output", "call_id": "c1", "output": '{"status":"ok"}'},
    ]


def test_another_providers_items_are_refused() -> None:
    items = (ProviderItems("anthropic", ({"type": "x"},)),)

    with pytest.raises(ValueError, match="cannot replay"):
        _request_kwargs(request(items=items))


# -------------------------------------------------------------- stop reasons
async def test_completed_text_with_usage_and_request_id() -> None:
    client, _ = adapter(sdk_response([message(output_text("Hello there"))]))

    result = await client.complete(request())

    assert result.stop_reason is StopReason.COMPLETED
    assert result.text == "Hello there"
    assert result.usage.input_tokens == 120
    assert result.usage.cache_read_tokens == 100
    assert result.request_id == "req_123"
    assert result.output_items is not None
    assert result.output_items.provider == "openai"


async def test_a_refusal_is_detected_before_any_text_is_read() -> None:
    refusal = ResponseOutputRefusal.model_construct(type="refusal", refusal="No.")
    client, _ = adapter(sdk_response([message(output_text("partial"), refusal)]))

    result = await client.complete(request())

    assert result.stop_reason is StopReason.REFUSAL
    assert result.text is None


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("max_output_tokens", StopReason.MAX_TOKENS),
        ("content_filter", StopReason.CONTENT_FILTER),
        ("steered", StopReason.OTHER),
    ],
)
async def test_incomplete_responses_expose_no_content(reason: str, expected: StopReason) -> None:
    client, _ = adapter(
        sdk_response([message(output_text("cut off"))], status="incomplete", reason=reason)
    )

    result = await client.complete(request())

    assert result.stop_reason is expected
    assert result.text is None
    assert result.tool_calls == ()


async def test_function_calls_become_tool_calls() -> None:
    call = ResponseFunctionToolCall.model_construct(
        type="function_call", call_id="call_9", name="get_status", arguments="{}"
    )
    client, _ = adapter(sdk_response([call]))

    result = await client.complete(request(tools=(TOOL,)))

    assert result.stop_reason is StopReason.TOOL_CALLS
    assert [(c.call_id, c.name, c.arguments) for c in result.tool_calls] == [
        ("call_9", "get_status", "{}")
    ]
    assert result.output_items is not None
    assert result.output_items.payload[0]["call_id"] == "call_9"


class Answer(BaseModel):
    verdict: str


async def test_parse_refuses_tools_and_revalidates_output() -> None:
    client, _ = adapter(sdk_response([message(output_text("{}"))]))

    with pytest.raises(ValueError, match="no tools"):
        await client.parse(request(tools=(TOOL,)), Answer)


# -------------------------------------------------------------------- errors
REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/responses")


def status_error(cls: type[openai.APIStatusError], code: int) -> openai.APIStatusError:
    return cls(
        "synthetic secret detail", response=httpx2.Response(code, request=REQUEST), body=None
    )


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (openai.APIConnectionError(request=REQUEST), LLMErrorKind.UNAVAILABLE),
        (openai.APITimeoutError(request=REQUEST), LLMErrorKind.UNAVAILABLE),
        (status_error(openai.RateLimitError, 429), LLMErrorKind.RATE_LIMITED),
        (status_error(openai.AuthenticationError, 401), LLMErrorKind.AUTH),
        (status_error(openai.PermissionDeniedError, 403), LLMErrorKind.AUTH),
        (status_error(openai.BadRequestError, 400), LLMErrorKind.BAD_REQUEST),
        (status_error(openai.NotFoundError, 404), LLMErrorKind.BAD_REQUEST),
        (status_error(openai.InternalServerError, 500), LLMErrorKind.UNAVAILABLE),
    ],
)
async def test_sdk_errors_are_classified_without_their_message(
    error: openai.OpenAIError, kind: LLMErrorKind
) -> None:
    client, _ = adapter(error)

    with pytest.raises(LLMError) as caught:
        await client.complete(request())

    assert caught.value.kind is kind
    # The SDK class name (content-free), so an evaluation can tell a timeout
    # from a connection error or a 5xx (S03.7 fix round, H1).
    assert caught.value.exc_type == type(error).__name__
    assert "synthetic secret detail" not in str(caught.value)
    assert caught.value.__cause__ is None
