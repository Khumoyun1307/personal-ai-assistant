"""I25 trace normalization: closed codes, positive ids, no content (R4-10).

Pure tests of what the trace writer would store. The integration suite
(`tests/integration/test_incident_trace.py`) covers storage and the report.
"""

from __future__ import annotations

import ast
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from assistant.actions.requests import (
    Continuation,
    OwnerContinuation,
    OwnerRequestView,
    RequestSource,
    SourceSpan,
)
from assistant.actions.states import ActionState, StateReason
from assistant.agent.incident_trace import (
    FINISH_RESERVE,
    MAX_CHILD_RUNS_PER_READ,
    MAX_OUTCOMES_TRACED,
    REGULAR_SLOTS,
    _bounded,
    event,
    start_events,
    tool_events,
)
from assistant.agent.models import (
    MAX_TRACE_EVENTS_PER_RUN,
    RUN_STATUSES,
    TRACE_CODES,
    TRACE_REASONS,
    TRACE_STAGES,
)
from assistant.agent.tools.registry import OwnerTurn, ToolContext, ToolOutcome, ToolServices
from tests.fakes.clock import FakeClock

ROOT = Path(__file__).resolve().parents[2]
CANARY = "SYNTH-I25-CANARY Kamola sk-proj-I25Secret"


def _strings(node: ast.AST | None) -> set[str]:
    """String constants a status/reason expression can evaluate to."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _strings(node.body) | _strings(node.orelse)
    return set()


def emitted_by_tools() -> tuple[set[str], set[str]]:
    """Every literal proposal status and reason the agent tools can emit:
    `source_failure(status, reason)`, `_receipt(..., status, reason=...)`,
    `"proposal_status": ...` / `"reason": ...` dict entries and
    `proposal_status = ...` assignments (constants and conditional branches)."""
    statuses: set[str] = set()
    reasons: set[str] = set()
    for path in sorted((ROOT / "src/assistant/agent/tools").glob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "source_failure":
                    statuses |= _strings(node.args[0] if node.args else None)
                    reasons |= _strings(node.args[1] if len(node.args) > 1 else None)
                if node.func.id in {"_receipt", "_literal_receipt"} and len(node.args) > 4:
                    statuses |= _strings(node.args[4])
                if node.func.id in {"_receipt", "_literal_receipt", "source_failure"}:
                    for keyword in node.keywords:
                        if keyword.arg == "reason":
                            reasons |= _strings(keyword.value)
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value == "proposal_status":
                        statuses |= _strings(value)
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "proposal_status":
                        statuses |= _strings(node.value)
    return statuses, reasons


def test_every_proposal_status_and_reason_the_tools_emit_has_a_code() -> None:
    statuses, reasons = emitted_by_tools()
    assert {"staged", "proposal_conflict", "idempotency_conflict"} <= statuses  # scan works
    assert statuses - set(TRACE_CODES["proposal"]) == set()
    assert reasons - set(TRACE_REASONS) == set()


def test_code_lists_cover_every_state_reason_and_status() -> None:
    assert set(TRACE_STAGES) == set(TRACE_CODES)
    assert {reason.value for reason in StateReason} <= set(TRACE_REASONS)
    assert {state.value for state in ActionState} <= set(TRACE_CODES["outcome"])
    assert set(RUN_STATUSES) <= set(TRACE_CODES["run"]) & set(TRACE_CODES["child_run"])
    assert all("other" in codes for codes in TRACE_CODES.values())


@pytest.mark.parametrize("value", [CANARY, "", None, 7, "awaiting_approval "])
def test_values_outside_a_closed_list_become_other(value: object) -> None:
    item = event("proposal", value, reason=CANARY if value is not None else None)
    assert item.code == "other"
    assert item.reason in {None, "other"}


@pytest.mark.parametrize("value", [True, False, -1, 0, "12", 1.5, CANARY])
def test_only_positive_int_ids_survive(value: object) -> None:
    item = event("recipient", "unbound", conversation_id=value, action_id=value, turn_id=value)
    assert (item.conversation_id, item.action_id, item.turn_id) == (None, None, None)


def test_unknown_stage_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="unknown trace stage"):
        event("free_text", "other")


def context(**changes: Any) -> ToolContext:
    values: dict[str, Any] = {
        "account_id": 1,
        "run_id": 2,
        "run_type": "owner_request",
        "tainted": False,
        "services": ToolServices(session_factory=None, clock=FakeClock(), monthly_budget_usd=None),  # type: ignore[arg-type]
        "turn_id": 30,
        "managed_ok": True,
    }
    return ToolContext(**(values | changes))


def test_tool_events_read_fixed_keys_only_and_carry_no_content() -> None:
    outcome = ToolOutcome(
        "awaiting_approval",
        {
            "proposal_status": "staged",
            "reason": CANARY,
            "request_id": 5,
            "request_version": 2,
            "action": {
                "action_id": 9,
                "conversation_id": 4,
                "version": 3,
                "presentation": "shown",
                "state": "awaiting_approval",
            },
            "contact_name": CANARY,
            "text": CANARY,
            "guidance_ignored": "the guidance was not used",
        },
        contains_managed=True,
        log_detail={"instruction_source": "dropped_tainted", "mode": "reply"},
    )
    events = tool_events(context(), outcome)
    assert CANARY not in repr(events)
    stored = {(e.stage, e.code) for e in events}
    assert stored == {
        ("taint", "tainted_by_tool"),
        ("dropped", "dropped_tainted"),
        ("dropped", "guidance_ignored"),
        ("proposal", "staged"),
        ("recipient", "proposal_target"),
        ("observed", "shown"),
    }
    [proposal] = [e for e in events if e.stage == "proposal"]
    assert asdict(proposal) == {
        "stage": "proposal",
        "code": "staged",
        "reason": "other",
        "turn_id": None,
        "request_id": 5,
        "request_version": 2,
        "child_run_id": None,
        "action_id": 9,
        "action_version": 3,
        "conversation_id": None,
    }
    [observed] = [e for e in events if e.stage == "observed"]
    assert (observed.action_id, observed.action_version, observed.conversation_id) == (9, 3, 4)


def test_a_no_action_result_traces_only_the_fixed_reason() -> None:
    outcome = ToolOutcome(
        "not_drafted",
        {"proposal_status": "invalid_arguments", "reason": "recipient_mismatch", "action": None},
    )
    events = tool_events(context(tainted=True), outcome)
    assert [(e.stage, e.code, e.reason) for e in events] == [
        ("proposal", "invalid_arguments", "recipient_mismatch")
    ]


def test_start_events_name_request_sources_recipient_and_dropped_channels() -> None:
    now = datetime(2026, 11, 17, tzinfo=UTC)
    sources = tuple(
        RequestSource(
            span=SourceSpan(turn_id=turn, field="text", start=0, end=4, field_sha256="0" * 64),
            purpose=purpose,
        )
        for turn, purpose in ((30, "literal_body"), (28, "instruction"))
    )
    view = OwnerRequestView(
        request_id=5,
        version=3,
        operation="dictate",
        state="ready",
        contact_id=7,
        conversation_id=4,
        missing_slot=None,
        action_id=None,
        action_version=None,
        target_message_id=None,
        sources=sources,
        requested_at=now,
        expires_at=now,
        selection_basis="unique_strong",
    )
    ctx = context(
        tainted=True,
        managed_ok=False,
        owner_turn=OwnerTurn(
            id=30, text=CANARY, third_party=True, contains_managed=False, quoted=False
        ),
        continuation=OwnerContinuation(
            selection=Continuation(
                status="selected", request_id=5, version=3, basis="pending_question"
            ),
            request=view,
        ),
    )
    events = start_events(ctx, opened_by_turn=False)
    assert CANARY not in repr(events)
    assert [(e.stage, e.code, e.turn_id) for e in events] == [
        ("request", "pending_question", 30),
        ("instruction", "literal_body", 30),
        ("instruction", "instruction", 28),
        ("recipient", "unique_strong", None),
        ("dropped", "forwarded_turn", 30),
        ("dropped", "managed_access_off", 30),
        ("taint", "started_tainted", 30),
    ]
    assert events[3].conversation_id == 4
    assert start_events(ctx, opened_by_turn=True)[0].code == "opened_by_turn"


def test_a_run_without_a_request_says_so() -> None:
    assert [(e.stage, e.code) for e in start_events(context(), opened_by_turn=False)] == [
        ("request", "none")
    ]


# ------------------------------------------------------------ S2: bounds keep the outcome
def _many(count: int, stage: str = "proposal", code: str = "staged") -> list[Any]:
    return [event(stage, code)] * count


def test_finish_reserve_holds_the_largest_finish_batch() -> None:
    assert FINISH_RESERVE >= MAX_OUTCOMES_TRACED + MAX_CHILD_RUNS_PER_READ + 1
    assert REGULAR_SLOTS + 1 + FINISH_RESERVE == MAX_TRACE_EVENTS_PER_RUN


def test_regular_overflow_ends_with_one_truncation_marker() -> None:
    batch = _bounded(_many(REGULAR_SLOTS + 5), 0, final=False)
    assert len(batch) == REGULAR_SLOTS + 1
    assert (batch[-1].stage, batch[-1].code) == ("trace", "truncated")
    # Later regular events are dropped; the marker is not repeated.
    assert _bounded(_many(2), REGULAR_SLOTS + 1, final=False) == []


def test_a_batch_that_exactly_reaches_the_regular_limit_is_kept_whole() -> None:
    """The reviewer's edge: a full regular area never eats the finish slots."""
    batch = _bounded(_many(2), REGULAR_SLOTS - 2, final=False)
    assert [item.stage for item in batch] == ["proposal", "proposal"]
    after = _bounded(_many(1), REGULAR_SLOTS, final=False)
    assert [(item.stage, item.code) for item in after] == [("trace", "truncated")]


@pytest.mark.parametrize("used", [0, REGULAR_SLOTS, REGULAR_SLOTS + 1])
def test_the_largest_finish_batch_always_fits_after_regular_events(used: int) -> None:
    finish = [
        *_many(MAX_OUTCOMES_TRACED, "outcome", "awaiting_approval"),
        *_many(MAX_CHILD_RUNS_PER_READ, "child_run", "succeeded"),
        event("run", "succeeded"),
    ]
    assert _bounded(finish, used, final=True) == finish


def test_an_oversized_final_batch_keeps_the_run_status_last() -> None:
    finish = [*_many(10, "outcome", "awaiting_approval"), event("run", "failed")]
    kept = _bounded(finish, MAX_TRACE_EVENTS_PER_RUN - 3, final=True)
    assert len(kept) == 3
    assert (kept[-1].stage, kept[-1].code) == ("run", "failed")
    assert _bounded(finish, MAX_TRACE_EVENTS_PER_RUN, final=True) == []


def test_unhashable_or_odd_values_are_normalized_not_raised() -> None:
    outcome = ToolOutcome(
        "awaiting_approval",
        {"proposal_status": ["staged"], "reason": {"x": CANARY}, "action": [1]},
        log_detail={"instruction_source": ["dropped_quoted"]},
    )
    events = tool_events(context(), outcome)
    assert [(e.stage, e.code, e.reason) for e in events] == [("proposal", "other", "other")]
