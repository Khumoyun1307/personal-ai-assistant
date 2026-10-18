"""Tool execution contract (ARCHITECTURE §8.3, AGENTS §8).

Covers: unknown or unoffered tools, invalid JSON, schema-invalid arguments
(reported by field, never echoed), schema-valid but semantically invalid
arguments (a foreign-account id), an idempotent retry of the same call id, and
managed results being marked for the consent wrapper.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from assistant.agent.tools.executor import ToolExecutor
from assistant.agent.tools.registry import (
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolServices,
)
from assistant.llm.port import Provenance, ToolCall
from tests.fakes.clock import FakeClock

MY_CONVERSATION = 11
SOMEONE_ELSES_CONVERSATION = 99


class ConversationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: int


class Handler:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, _ctx: ToolContext, args: ConversationArgs) -> ToolOutcome:
        self.calls += 1
        if args.conversation_id != MY_CONVERSATION:
            # What an account-scoped repository does for a foreign id.
            raise LookupError
        return ToolOutcome("ok", {"title": "synthetic"}, contains_managed=True)


def make(handler: Handler) -> tuple[ToolExecutor, list[tuple[str, str | None, str, int]]]:
    steps: list[tuple[str, str | None, str, int]] = []

    async def record(name: str, digest: str | None, status: str, latency: int) -> None:
        steps.append((name, digest, status, latency))

    clock = FakeClock()
    tool = ToolDefinition(
        name="get_conversation",
        description="d",
        args_model=ConversationArgs,
        capability=CapabilityClass.READ,
        handler=handler,
        release_flag="tool.get_conversation",
        returns_managed=True,
    )
    context = ToolContext(
        account_id=1,
        run_id=5,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(session_factory=None, clock=clock, monthly_budget_usd=None),  # type: ignore[arg-type]
    )
    executor = ToolExecutor(
        context=context, offered={tool.name: tool}, record_step=record, clock=clock
    )
    return executor, steps


def output(result: Any) -> dict[str, Any]:
    return dict(json.loads(result.output))


async def test_a_tool_that_was_not_offered_is_refused() -> None:
    handler = Handler()
    executor, steps = make(handler)

    result = await executor.execute(ToolCall("c1", "delete_everything", "{}"))

    assert output(result) == {"status": "error", "reason": "this tool is not available"}
    assert handler.calls == 0
    assert (steps[0][0], steps[0][2]) == ("delete_everything", "error")


async def test_invalid_json_is_an_error_not_a_crash() -> None:
    executor, _ = make(Handler())

    result = await executor.execute(ToolCall("c1", "get_conversation", "{not json"))

    assert output(result)["reason"] == "arguments are not valid JSON"


async def test_schema_invalid_arguments_name_the_field_without_echoing_input() -> None:
    handler = Handler()
    executor, _ = make(handler)

    result = await executor.execute(
        ToolCall("c1", "get_conversation", json.dumps({"conversation_id": "synthetic secret"}))
    )

    body = output(result)
    assert body["reason"] == "invalid arguments"
    assert body["fields"] == ["conversation_id"]
    assert "synthetic secret" not in result.output
    assert handler.calls == 0


async def test_extra_arguments_are_rejected() -> None:
    executor, _ = make(Handler())

    result = await executor.execute(
        ToolCall("c1", "get_conversation", '{"conversation_id": 11, "account_id": 2}')
    )

    assert output(result)["fields"] == ["account_id"]


async def test_a_foreign_account_id_reads_as_not_found() -> None:
    executor, steps = make(Handler())

    result = await executor.execute(
        ToolCall(
            "c1", "get_conversation", json.dumps({"conversation_id": SOMEONE_ELSES_CONVERSATION})
        )
    )

    assert output(result) == {"status": "error", "reason": "not found"}
    assert steps[0][2] == "error"


async def test_a_repeated_call_id_executes_once() -> None:
    handler = Handler()
    executor, steps = make(handler)
    call = ToolCall("c1", "get_conversation", json.dumps({"conversation_id": MY_CONVERSATION}))

    first = await executor.execute(call)
    second = await executor.execute(call)

    assert first == second
    assert handler.calls == 1
    assert len(steps) == 1


async def test_steps_are_content_free_and_managed_results_are_marked() -> None:
    executor, steps = make(Handler())

    result = await executor.execute(
        ToolCall("c1", "get_conversation", json.dumps({"conversation_id": MY_CONVERSATION}))
    )

    assert result.provenance is Provenance.MANAGED
    name, digest, status, _latency = steps[0]
    assert (name, status) == ("get_conversation", "ok")
    assert digest is not None
    assert str(MY_CONVERSATION) not in digest


async def test_all_results_of_one_turn_are_returned_together_in_order() -> None:
    executor, _ = make(Handler())
    calls = (
        ToolCall("a", "get_conversation", json.dumps({"conversation_id": MY_CONVERSATION})),
        ToolCall("b", "nope", "{}"),
    )

    results = await executor.execute_all(calls)

    assert [r.call_id for r in results] == ["a", "b"]
