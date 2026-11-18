"""I25 trace normalization: closed codes, positive ids, no content (R4-10).

Pure tests of what the trace writer would store. The integration suite
(`tests/integration/test_incident_trace.py`) covers storage and the report.
"""

from __future__ import annotations

import importlib.util
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
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
from assistant.agent.incident_trace import event, start_events, tool_events
from assistant.agent.models import (
    MAX_TRACE_EVENTS_PER_RUN,
    RUN_STATUSES,
    TRACE_CODES,
    TRACE_REASONS,
    TRACE_STAGES,
    trace_code_check,
)
from assistant.agent.tools.registry import OwnerTurn, ToolContext, ToolOutcome, ToolServices
from tests.fakes.clock import FakeClock

ROOT = Path(__file__).resolve().parents[2]
CANARY = "SYNTH-I25-CANARY Kamola sk-proj-I25Secret"


def migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "i25_migration", ROOT / "migrations/versions/0024_incident_trace.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_freezes_exactly_the_model_code_lists() -> None:
    frozen = migration()
    assert frozen.STAGES == TRACE_STAGES
    assert frozen.CODES == TRACE_CODES
    assert frozen.REASONS == TRACE_REASONS
    assert frozen.MAX_EVENTS == MAX_TRACE_EVENTS_PER_RUN
    assert frozen._code_check() == trace_code_check()


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
