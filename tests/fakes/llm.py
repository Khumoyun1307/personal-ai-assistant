"""A scripted `LLMClient` for tests (AGENTS §11: tests never call a real LLM).

Records every request so tests can assert on exactly what *would* have been
sent — in particular that nothing reached it without consent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from assistant.llm.port import (
    LLMError,
    LLMRequest,
    LLMResponse,
    ParsedResponse,
    ProviderItems,
    StopReason,
    ToolCall,
    Usage,
)

Script = LLMResponse | LLMError | Callable[[LLMRequest], LLMResponse]


def text_response(text: str, *, tokens: int = 30) -> LLMResponse:
    return LLMResponse(
        StopReason.COMPLETED,
        "gpt-6-luna",
        Usage(input_tokens=tokens, output_tokens=tokens // 3),
        text=text,
        output_items=ProviderItems("fake", ({"type": "message", "text": text},)),
        request_id="req_fake",
    )


def tool_response(*calls: tuple[str, str, str], tokens: int = 30) -> LLMResponse:
    """`calls` are `(call_id, name, arguments_json)`."""
    return LLMResponse(
        StopReason.TOOL_CALLS,
        "gpt-6-luna",
        Usage(input_tokens=tokens, output_tokens=tokens // 3),
        tool_calls=tuple(ToolCall(call_id, name, arguments) for call_id, name, arguments in calls),
        output_items=ProviderItems(
            "fake", tuple({"type": "function_call", "call_id": c[0]} for c in calls)
        ),
        request_id="req_fake",
    )


def stop_response(reason: StopReason) -> LLMResponse:
    return LLMResponse(reason, "gpt-6-luna", Usage(input_tokens=10, output_tokens=5))


class FakeLLM:
    processor = "OpenAI"

    def __init__(self, *script: Script) -> None:
        self._script: list[Script] = list(script)
        self.requests: list[LLMRequest] = []
        self.parsed: list[Any] = []

    def queue(self, *script: Script) -> None:
        self._script.extend(script)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("FakeLLM called more times than scripted")
        step = self._script.pop(0)
        if isinstance(step, LLMError):
            raise step
        if callable(step) and not isinstance(step, LLMResponse):
            return step(request)
        return step

    async def parse[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> ParsedResponse[T]:
        self.requests.append(request)
        if not self.parsed:
            raise AssertionError("FakeLLM.parse called more times than scripted")
        value = self.parsed.pop(0)
        if isinstance(value, LLMError):
            raise value
        return ParsedResponse(
            StopReason.COMPLETED, "gpt-6-luna", Usage(10, 5), schema.model_validate(value)
        )
