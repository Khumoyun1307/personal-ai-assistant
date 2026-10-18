"""Tool execution contract (ARCHITECTURE §8.3).

For every tool call the executor:

1. refuses a tool that was not offered to this run (unknown, flagged off, wrong
   run type or tier) — the model can name anything, so the offer is the check;
2. parses and validates the arguments with the tool's Pydantic model — errors
   are reported by field path, never by echoing the input;
3. runs the handler once per `(run_id, call_id)`: a repeated call id returns
   the first result instead of executing again;
4. returns a compact, factual JSON result;
5. records a content-free `agent_run_steps` row (name, argument digest,
   status, latency).

Account ownership of referenced ids is checked inside each handler, through
account-scoped repositories that cannot return another account's rows.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from pydantic import ValidationError

from assistant.agent.persistence import args_digest
from assistant.agent.tools.registry import ToolContext, ToolDefinition, ToolOutcome
from assistant.core.clock import Clock
from assistant.llm.port import Provenance, ToolCall, ToolResultItem
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

#: Persists one step: (tool_name, args_digest, status, latency_ms).
StepRecorder = Callable[[str, str | None, str, int], Awaitable[None]]


@dataclass
class ToolExecutor:
    """Executes one run's tool calls. One instance per run."""

    context: ToolContext
    offered: Mapping[str, ToolDefinition]
    record_step: StepRecorder
    clock: Clock
    _done: dict[str, ToolResultItem] = field(default_factory=dict)

    async def execute_all(self, calls: tuple[ToolCall, ...]) -> list[ToolResultItem]:
        """Run every call of one model turn; results are returned together."""
        return [await self.execute(call) for call in calls]

    async def execute(self, call: ToolCall) -> ToolResultItem:
        cached = self._done.get(call.call_id)
        if cached is not None:
            return cached
        started = self.clock.now()
        outcome, digest = await self._run(call)
        latency_ms = int((self.clock.now() - started).total_seconds() * 1000)
        await self.record_step(call.name, digest, outcome.status, latency_ms)
        result = ToolResultItem(
            call_id=call.call_id,
            output=json.dumps({"status": outcome.status, **outcome.data}, default=str),
            provenance=Provenance.MANAGED if outcome.contains_managed else Provenance.TOOL_RESULT,
        )
        self._done[call.call_id] = result
        return result

    async def _run(self, call: ToolCall) -> tuple[ToolOutcome, str | None]:
        tool = self.offered.get(call.name)
        if tool is None:
            return ToolOutcome("error", {"reason": "this tool is not available"}), None
        try:
            raw = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            return ToolOutcome("error", {"reason": "arguments are not valid JSON"}), None
        digest = args_digest(raw)
        try:
            args = tool.args_model.model_validate(raw)
        except ValidationError as error:
            fields = sorted(
                {
                    ".".join(str(part) for part in issue["loc"]) or "(root)"
                    for issue in error.errors(include_input=False, include_url=False)
                }
            )
            return ToolOutcome("error", {"reason": "invalid arguments", "fields": fields}), digest
        try:
            return await tool.handler(self.context, args), digest
        except LookupError:
            # A referenced id that does not exist in this account. Same answer
            # whether it is missing or someone else's (AGENTS §6.12).
            return ToolOutcome("error", {"reason": "not found"}), digest
