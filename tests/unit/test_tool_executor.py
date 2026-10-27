"""Tool execution contract (ARCHITECTURE §8.3, AGENTS §8).

Covers: unknown or unoffered tools, invalid JSON, schema-invalid arguments
(reported by field, never echoed), our own argument rules told to the model
in fixed words (`problems`), schema-valid but semantically invalid arguments
(a foreign-account id), a lookup bug that is not "not found", an idempotent
retry of the same call id, managed results being marked for the consent
wrapper, and one content-free log line for every call that does not succeed
(M03 fix round C2: failures used to leave no trace).
"""

from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, model_validator
from structlog.testing import capture_logs

from assistant.agent.tools.common import ArgumentRuleError
from assistant.agent.tools.executor import ToolExecutor
from assistant.agent.tools.registry import (
    CHAT_ACCESS_OFF,
    CapabilityClass,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolServices,
)
from assistant.core.errors import NotInAccountError
from assistant.llm.port import Provenance, ToolCall
from tests.fakes.clock import FakeClock

MY_CONVERSATION = 11
SOMEONE_ELSES_CONVERSATION = 99
#: Ids that make the test handler misbehave.
LOOKUP_BUG = 13
BLOCKED = 14
HANDLER_ERROR = 15
CANARY = "CANARY-SYNTH-c2"
#: Ten characters, so it passes the length check and reaches
#: `date.fromisoformat`, whose message quotes its input.
DAY_CANARY = "CANARY-c2x"
RULE = "give first or second, not both; set the other to null"
TOOL_EVENTS = {"agent_tool_call_failed", "agent_tool_call_not_run"}


class ConversationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: int


def _day(value: str) -> str:
    date.fromisoformat(value)  # a stdlib message that quotes the input
    return value


class RuleArgs(BaseModel):
    """One of our rules (fixed words) and a stdlib error that echoes input."""

    model_config = ConfigDict(extra="forbid")
    day: Annotated[str, AfterValidator(_day)]
    first: int | None
    second: int | None

    @model_validator(mode="after")
    def _one(self) -> Self:
        if self.first is not None and self.second is not None:
            raise ArgumentRuleError(RULE)
        return self


class Handler:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, _ctx: ToolContext, args: ConversationArgs) -> ToolOutcome:
        self.calls += 1
        if args.conversation_id == LOOKUP_BUG:
            # A programming error, not a missing id: must not read "not found".
            missing: dict[str, Any] = {}
            return ToolOutcome("ok", missing[CANARY])
        if args.conversation_id == BLOCKED:
            return ToolOutcome(CHAT_ACCESS_OFF, {"reason": "synthetic"})
        if args.conversation_id == HANDLER_ERROR:
            return ToolOutcome("error", {"reason": f"synthetic {CANARY}"})
        if args.conversation_id != MY_CONVERSATION:
            # What an account-scoped repository does for a foreign id.
            raise NotInAccountError
        return ToolOutcome("ok", {"title": "synthetic"}, contains_managed=True)


async def _rules(_ctx: ToolContext, _args: RuleArgs) -> ToolOutcome:
    return ToolOutcome("ok")


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
    rules = ToolDefinition(
        name="check_rules",
        description="d",
        args_model=RuleArgs,
        capability=CapabilityClass.READ,
        handler=_rules,
        release_flag="tool.check_rules",
    )
    context = ToolContext(
        account_id=1,
        run_id=5,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(session_factory=None, clock=clock, monthly_budget_usd=None),  # type: ignore[arg-type]
    )
    executor = ToolExecutor(
        context=context,
        offered={tool.name: tool, rules.name: rules},
        record_step=record,
        clock=clock,
        # Registered but not offered to this run (its flag is off).
        registered=frozenset({tool.name, rules.name, "delete_everything"}),
    )
    return executor, steps


def output(result: Any) -> dict[str, Any]:
    return dict(json.loads(result.output))


def call(conversation_id: Any, call_id: str = "c1") -> ToolCall:
    return ToolCall(call_id, "get_conversation", json.dumps({"conversation_id": conversation_id}))


def tool_lines(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry["event"] in TOOL_EVENTS]


def assert_content_free(logs: list[dict[str, Any]], *secrets: str) -> None:
    rendered = json.dumps(logs, default=str)
    for secret in (CANARY, DAY_CANARY, *secrets):
        assert secret not in rendered


async def test_a_tool_that_was_not_offered_is_refused() -> None:
    handler = Handler()
    executor, steps = make(handler)

    with capture_logs() as logs:
        result = await executor.execute(ToolCall("c1", "delete_everything", "{}"))

    assert output(result) == {"status": "error", "reason": "this tool is not available"}
    assert handler.calls == 0
    assert (steps[0][0], steps[0][2]) == ("delete_everything", "error")
    [line] = tool_lines(logs)
    assert line["event"] == "agent_tool_call_failed"
    assert line["log_level"] == "warning"
    assert (line["tool"], line["status"], line["code"]) == (
        "delete_everything",
        "error",
        "not_offered",
    )
    assert (line["account_id"], line["run_id"]) == (1, 5)


async def test_a_tool_name_that_is_not_registered_is_never_logged_or_stored() -> None:
    """The name is model output: it is recorded as "(unknown)"."""
    executor, steps = make(Handler())

    with capture_logs() as logs:
        result = await executor.execute(ToolCall("c1", f"{CANARY}_tool", "{}"))

    assert output(result)["reason"] == "this tool is not available"
    assert steps[0][0] == "(unknown)"
    [line] = tool_lines(logs)
    assert (line["tool"], line["code"]) == ("(unknown)", "not_offered")
    assert_content_free(logs)


async def test_invalid_json_is_an_error_not_a_crash() -> None:
    executor, _ = make(Handler())

    with capture_logs() as logs:
        result = await executor.execute(ToolCall("c1", "get_conversation", f"{{not json {CANARY}"))

    assert output(result)["reason"] == "arguments are not valid JSON"
    [line] = tool_lines(logs)
    assert (line["tool"], line["code"]) == ("get_conversation", "args_not_json")
    assert_content_free(logs)


async def test_schema_invalid_arguments_name_the_field_without_echoing_input() -> None:
    handler = Handler()
    executor, _ = make(handler)

    with capture_logs() as logs:
        result = await executor.execute(call(f"synthetic secret {CANARY}"))

    body = output(result)
    assert body["reason"] == "invalid arguments"
    assert body["fields"] == ["conversation_id"]
    assert "problems" not in body  # not one of our rules: the field only
    assert CANARY not in result.output
    assert handler.calls == 0
    [line] = tool_lines(logs)
    assert line["code"] == "args_invalid"
    assert (line["fields"], line["error_types"]) == (["conversation_id"], ["int_parsing"])
    assert_content_free(logs)


async def test_extra_arguments_are_rejected() -> None:
    executor, _ = make(Handler())

    with capture_logs() as logs:
        result = await executor.execute(
            ToolCall("c1", "get_conversation", json.dumps({"conversation_id": 11, CANARY: 2}))
        )

    assert output(result)["fields"] == [CANARY]  # the model's own key, back to the model
    [line] = tool_lines(logs)
    # An unknown key is model output: logged only as "(extra)".
    assert (line["fields"], line["error_types"]) == (["(extra)"], ["extra_forbidden"])
    assert_content_free(logs)


async def test_a_rule_violation_tells_the_model_which_rule_without_echoing_input() -> None:
    executor, _ = make(Handler())

    with capture_logs() as logs:
        broken_rule = await executor.execute(
            ToolCall(
                "c1", "check_rules", json.dumps({"day": "2026-10-27", "first": 1, "second": 2})
            )
        )
        bad_day = await executor.execute(
            ToolCall(
                "c2", "check_rules", json.dumps({"day": DAY_CANARY, "first": 1, "second": None})
            )
        )

    body = output(broken_rule)
    assert (body["reason"], body["fields"]) == ("invalid arguments", ["(root)"])
    assert body["problems"] == [{"field": "(root)", "rule": RULE}]
    # A stdlib message quotes the input: never shown, only the field.
    other = output(bad_day)
    assert (other["fields"], "problems" in other) == (["day"], False)
    assert DAY_CANARY not in bad_day.output
    rule_line, day_line = tool_lines(logs)
    assert (rule_line["fields"], rule_line["error_types"]) == (["(root)"], ["value_error"])
    assert (day_line["fields"], day_line["error_types"]) == (["day"], ["value_error"])
    assert_content_free(logs, RULE)  # not even our own rule text is logged


async def test_a_foreign_account_id_reads_as_not_found() -> None:
    executor, steps = make(Handler())

    with capture_logs() as logs:
        result = await executor.execute(call(SOMEONE_ELSES_CONVERSATION))

    assert output(result) == {"status": "error", "reason": "not found"}
    assert steps[0][2] == "error"
    [line] = tool_lines(logs)
    assert line["log_level"] == "warning"
    assert (line["code"], line["exc_type"]) == ("not_found", "NotInAccountError")


async def test_a_lookup_bug_is_an_internal_error_not_a_missing_id() -> None:
    """A `KeyError` (any `LookupError` but `NotInAccountError`) is a bug: it is
    neither disguised as "not found" nor allowed to look like the owner's
    data is missing (§6 invariant 11)."""
    executor, steps = make(Handler())

    with capture_logs() as logs:
        result = await executor.execute(call(LOOKUP_BUG))

    assert output(result) == {"status": "error", "reason": "internal error"}
    assert steps[0][2] == "error"
    [line] = tool_lines(logs)
    assert line["log_level"] == "warning"
    assert (line["code"], line["exc_type"]) == ("internal_lookup", "KeyError")
    assert_content_free(logs)


async def test_a_handler_status_that_is_not_ok_is_logged_by_its_status() -> None:
    executor, _ = make(Handler())

    with capture_logs() as logs:
        await executor.execute(call(BLOCKED, "c1"))
        await executor.execute(call(HANDLER_ERROR, "c2"))

    blocked, failed = tool_lines(logs)
    assert (blocked["event"], blocked["log_level"]) == ("agent_tool_call_not_run", "info")
    assert blocked["code"] == f"handler:{CHAT_ACCESS_OFF}"
    assert (failed["event"], failed["log_level"]) == ("agent_tool_call_failed", "warning")
    assert failed["code"] == "handler:error"
    assert_content_free(logs)  # a reason text is never logged


async def test_a_successful_call_logs_no_failure_line() -> None:
    executor, _ = make(Handler())

    with capture_logs() as logs:
        await executor.execute(call(MY_CONVERSATION))

    assert tool_lines(logs) == []


async def test_a_repeated_call_id_executes_once() -> None:
    handler = Handler()
    executor, steps = make(handler)
    repeated = call(MY_CONVERSATION)

    first = await executor.execute(repeated)
    second = await executor.execute(repeated)

    assert first == second
    assert handler.calls == 1
    assert len(steps) == 1


async def test_steps_are_content_free_and_managed_results_are_marked() -> None:
    executor, steps = make(Handler())

    result = await executor.execute(call(MY_CONVERSATION))

    assert result.provenance is Provenance.MANAGED
    name, digest, status, _latency = steps[0]
    assert (name, status) == ("get_conversation", "ok")
    assert digest is not None
    assert str(MY_CONVERSATION) not in digest


async def test_all_results_of_one_turn_are_returned_together_in_order() -> None:
    executor, _ = make(Handler())
    calls = (call(MY_CONVERSATION, "a"), ToolCall("b", "nope", "{}"))

    results = await executor.execute_all(calls)

    assert [r.call_id for r in results] == ["a", "b"]


def test_no_code_raises_a_bare_lookup_error() -> None:
    """The executor answers "not found" only for `NotInAccountError`; every other
    `LookupError` is an internal error. So every "not this account's" raise
    in the code base is `NotInAccountError` (M03 fix round C2)."""
    root = Path(__file__).resolve().parents[2] / "src" / "assistant"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(raised, ast.Name) and raised.id == "LookupError":
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []
