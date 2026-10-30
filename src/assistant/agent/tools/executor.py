"""Tool execution contract (ARCHITECTURE §8.3) and the taint gate (§7.1).

For every tool call the executor:

1. refuses a tool that was not offered to this run (unknown, flagged off, wrong
   run type or tier) — the model can name anything, so the offer is the check;
2. parses and validates the arguments with the tool's Pydantic model — errors
   are reported by field path, never by echoing the input; a broken rule of
   our own (`ArgumentRuleError`) is also told in its fixed words
   (`problems`), so the model can correct the call;
3. **the single taint gate**: in a tainted run a `configure`/`control` call
   (other than the calls that only reduce autonomy) is held for the owner's
   tap (`agent/confirmations.py`) instead of executed; reads always run;
4. runs the handler once per `(run_id, call_id)`: a repeated call id returns
   the first result instead of executing again;
5. returns a compact, factual JSON result: `NotInAccountError` reads "not found"
   (the same for a missing id and another account's); any other
   `LookupError` is a bug and reads "internal error";
6. records a content-free `agent_run_steps` row (name, argument digest,
   status, latency); a name that is not a registered tool is model output,
   recorded as "(unknown)";
7. logs one content-free line for every call that does not end `ok` (M03 fix
   round C2): a warning `agent_tool_call_failed`, or `agent_tool_call_not_run`
   (info) for an expected state — held for a card, already decided on one,
   chat access off. Ids, the tool name, the status and a fixed code only;
   never arguments, reasons or messages.

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
from assistant.agent.tools.common import ArgumentRuleError
from assistant.agent.tools.registry import (
    CHAT_ACCESS_OFF,
    ToolContext,
    ToolDefinition,
    ToolOutcome,
    ToolRefs,
)
from assistant.core.clock import Clock
from assistant.core.errors import NotInAccountError
from assistant.llm.port import Provenance, ToolCall, ToolResultItem
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

#: Persists one step: (tool_name, args_digest, status, latency_ms).
StepRecorder = Callable[[str, str | None, str, int], Awaitable[None]]
#: Persists that the run became tainted.
TaintRecorder = Callable[[], Awaitable[None]]

#: What a call naming no registered tool is recorded and logged as.
UNKNOWN_TOOL = "(unknown)"
#: In the log: a model-level rule (no field), and a key that is no field.
ROOT_FIELD = "(root)"
EXTRA_FIELD = "(extra)"
#: Statuses that are expected states, not failures: logged at info. Held
#: for the owner's tap or already decided on its card (a repeated call), a
#: chat tool's blocked stub while chat access is off, or a propose tool's
#: normal result — a card awaiting approval, blocked, not drafted (ICR-3).
NOT_RUN_STATUSES = frozenset(
    {
        "awaiting_confirmation",
        "not_executed",
        "confirmed",
        "applied",
        "rejected",
        CHAT_ACCESS_OFF,
        "awaiting_approval",
        "blocked",
        "not_drafted",
        # PF-10: the provider refused to write an owner-requested draft.
        "declined_by_provider",
    }
)


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
    #: Every tool name the registry has, offered or not: a call naming one is
    #: recorded and logged by it; any other name is model output.
    registered: frozenset[str] = frozenset()
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
        known = call.name in self.offered or call.name in self.registered
        tool = call.name if known else UNKNOWN_TOOL
        await self.record_step(tool, digest, outcome.status, latency_ms)
        if outcome.status != "ok":
            self._log(tool, outcome)
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
                logger.info("agent_run_tainted", run_id=self.context.run_id, tool=tool)
        result = ToolResultItem(
            call_id=call.call_id,
            output=json.dumps({"status": outcome.status, **outcome.data}, default=str),
            provenance=Provenance.MANAGED if outcome.contains_managed else Provenance.TOOL_RESULT,
        )
        self._done[call.call_id] = result
        return result

    def _log(self, tool: str, outcome: ToolOutcome) -> None:
        """The one line for a call that did not end `ok` (§8.3 step 7)."""
        fields: dict[str, object] = {
            "account_id": self.context.account_id,
            "run_id": self.context.run_id,
            "tool": tool,
            "status": outcome.status,
            "code": outcome.log_code or f"handler:{outcome.status}",
            "tainted": self.context.tainted,
            **outcome.log_detail,
        }
        if outcome.status in NOT_RUN_STATUSES:
            logger.info("agent_tool_call_not_run", **fields)
        else:
            logger.warning("agent_tool_call_failed", **fields)

    async def _run(self, call: ToolCall) -> tuple[ToolOutcome, str | None]:
        tool = self.offered.get(call.name)
        if tool is None:
            return _failed("not_offered", "this tool is not available"), None
        try:
            raw = json.loads(call.arguments or "{}")
        except json.JSONDecodeError:
            return _failed("args_not_json", "arguments are not valid JSON"), None
        digest = args_digest(raw)
        try:
            args = tool.args_model.model_validate(raw)
        except ValidationError as error:
            return _invalid(tool, error), digest
        if tool.needs_confirmation(tainted=self.context.tainted, args=args):
            try:
                held = await confirmations.hold(
                    self.context, tool, call.call_id, args.model_dump(mode="json")
                )
            except NotInAccountError as error:
                # The card's description could not resolve an id: it is not
                # this account's (AGENTS §6.12).
                return _not_found(error), digest
            except LookupError as error:
                return _internal(error), digest
            return _coded("hold", held), digest
        try:
            context = replace(self.context, call_id=call.call_id, refs=self.refs)
            outcome = await tool.handler(context, args)
        except NotInAccountError as error:
            # A referenced id that does not exist in this account. Same answer
            # whether it is missing or someone else's (AGENTS §6.12).
            return _not_found(error), digest
        except LookupError as error:
            # A bug (a KeyError, an IndexError, a missing time zone), never a
            # missing id: said so, not "not found" (§6 invariant 11).
            return _internal(error), digest
        return _coded("handler", outcome), digest


def _failed(code: str, reason: str) -> ToolOutcome:
    return ToolOutcome("error", {"reason": reason}, log_code=code)


def _not_found(error: NotInAccountError) -> ToolOutcome:
    return ToolOutcome(
        "error",
        {"reason": "not found"},
        log_code="not_found",
        log_detail={"exc_type": type(error).__name__},
    )


def _internal(error: LookupError) -> ToolOutcome:
    return ToolOutcome(
        "error",
        {"reason": "internal error"},
        log_code="internal_lookup",
        log_detail={"exc_type": type(error).__name__},
    )


def _coded(stage: str, outcome: ToolOutcome) -> ToolOutcome:
    """`stage:<code>` for the log line: the outcome's own code, else its status."""
    return replace(outcome, log_code=f"{stage}:{outcome.log_code or outcome.status}")


def _invalid(tool: ToolDefinition, error: ValidationError) -> ToolOutcome:
    """For the model: every field path, and the fixed text of each of our own
    rules it broke. For the log: only real field names and pydantic's error
    types — never input, messages, or an unknown key (model output)."""
    fields: set[str] = set()
    problems: list[dict[str, str]] = []
    logged_fields: set[str] = set()
    error_types: set[str] = set()
    known = tool.args_model.model_fields
    for issue in error.errors(include_input=False, include_url=False):
        location = issue["loc"]
        path = ".".join(str(part) for part in location) or ROOT_FIELD
        fields.add(path)
        rule = issue.get("ctx", {}).get("error")
        if isinstance(rule, ArgumentRuleError):
            problems.append({"field": path, "rule": str(rule)})
        if not location:
            logged_fields.add(ROOT_FIELD)
        else:
            top = location[0]
            logged_fields.add(top if isinstance(top, str) and top in known else EXTRA_FIELD)
        error_types.add(issue["type"])
    data: dict[str, object] = {"reason": "invalid arguments", "fields": sorted(fields)}
    if problems:
        data["problems"] = problems
    return ToolOutcome(
        "error",
        data,
        log_code="args_invalid",
        log_detail={"fields": sorted(logged_fields), "error_types": sorted(error_types)},
    )
