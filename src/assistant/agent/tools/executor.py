"""Tool execution contract (ARCHITECTURE §8.3) and the taint gate (§7.1).

For every tool call the executor:

1. refuses a tool that was not offered to this run (unknown, flagged off, wrong
   run type or tier) — the model can name anything, so the offer is the check;
2. parses and validates the arguments with the tool's Pydantic model — errors
   are reported by field path, never by echoing the input;
3. **the single taint gate**: in a tainted run a `configure`/`control` call
   (other than the calls that only reduce autonomy) is held for the owner's
   tap (`agent/confirmations.py`) instead of executed; reads always run;
4. runs the handler once per `(run_id, call_id)`: a repeated call id returns
   the first result instead of executing again;
5. returns a compact, factual JSON result;
6. records a content-free `agent_run_steps` row (name, argument digest,
   status, latency).

Calls of one model turn run in order, and taint is sequential: a result
carrying contact-authored text taints the run at once, so a later call in
the same batch is gated too. The run's taint is persisted immediately
(`agent_runs.tainted`) through `on_taint`.

Account ownership of referenced ids is checked inside each handler, through
account-scoped queries that cannot return another account's rows.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace

from pydantic import ValidationError

from assistant.agent import confirmations
from assistant.agent.citations import CitableMessage
from assistant.agent.persistence import args_digest
from assistant.agent.tools.registry import (
    CHAT_ACCESS_OFF,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRefs,
)
from assistant.core.clock import Clock
from assistant.llm.port import Provenance, ToolCall, ToolResultItem
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

#: Persists one step: (tool_name, args_digest, status, latency_ms).
StepRecorder = Callable[[str, str | None, str, int], Awaitable[None]]
#: Persists that the run became tainted.
TaintRecorder = Callable[[], Awaitable[None]]


async def _no_taint_recorder() -> None:
    return None


@dataclass
class ToolExecutor:
    """Executes one run's tool calls. One instance per run."""

    context: ToolContext
    offered: Mapping[str, ToolDefinition]
    record_step: StepRecorder
    clock: Clock
    on_taint: TaintRecorder = _no_taint_recorder
    #: Conversations and contacts the results drew on (for `control_turns.refs`).
    refs: ToolRefs = field(default_factory=ToolRefs)
    #: A result in this run carried contact-authored text.
    saw_untrusted: bool = False
    #: Every message the results showed, by id: what the reply may cite (B3).
    citable: dict[int, CitableMessage] = field(default_factory=dict)
    #: A chat-reading tool was called while chat access is off (B5).
    saw_chat_access_off: bool = False
    _done: dict[str, ToolResultItem] = field(default_factory=dict)

    @property
    def tainted(self) -> bool:
        return self.context.tainted

    async def execute_all(self, calls: tuple[ToolCall, ...]) -> list[ToolResultItem]:
        """Run every call of one model turn, in order; results are returned together."""
        return [await self.execute(call) for call in calls]

    async def execute(self, call: ToolCall) -> ToolResultItem:
        cached = self._done.get(call.call_id)
        if cached is not None:
            return cached
        started = self.clock.now()
        outcome, digest = await self._run(call)
        latency_ms = int((self.clock.now() - started).total_seconds() * 1000)
        await self.record_step(call.name, digest, outcome.status, latency_ms)
        self.refs = self.refs | outcome.refs
        self.citable.update(outcome.citable)
        if outcome.status == CHAT_ACCESS_OFF:
            self.saw_chat_access_off = True
        if outcome.taints:
            # This run's OWN input carried third-party text (D56): its reply
            # will taint later runs, even if the run was already tainted.
            self.saw_untrusted = True
            if not self.context.tainted:
                self.context = replace(self.context, tainted=True)
                await self.on_taint()
                logger.info("agent_run_tainted", run_id=self.context.run_id, tool=call.name)
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
        if tool.needs_confirmation(tainted=self.context.tainted, args=args):
            try:
                held = await confirmations.hold(
                    self.context, tool, call.call_id, args.model_dump(mode="json")
                )
            except LookupError:
                # The card's description could not resolve an id: it is not
                # this account's (AGENTS §6.12).
                return ToolOutcome("error", {"reason": "not found"}), digest
            return held, digest
        try:
            context = replace(self.context, call_id=call.call_id, refs=self.refs)
            return await tool.handler(context, args), digest
        except LookupError:
            # A referenced id that does not exist in this account. Same answer
            # whether it is missing or someone else's (AGENTS §6.12).
            return ToolOutcome("error", {"reason": "not found"}), digest
