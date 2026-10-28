"""`decide`, the execution-failure mapping and explanations (S04.1 spec §3.2-§3.3)."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from assistant.actions.states import (
    TRANSITIONS,
    ActionKind,
    ActionOrigin,
    ActionState,
    StateReason,
)
from assistant.core.reply_window import REPLY_WINDOW
from assistant.policy import rules
from assistant.policy.context import (
    AccountSnapshot,
    ActionFacts,
    CheckResult,
    ConnectionSnapshot,
    ConversationSnapshot,
    Decision,
    Freshness,
    IngestionState,
    Outcome,
    Phase,
    PolicyContext,
)
from assistant.policy.engine import decide, execution_target
from assistant.policy.explanations import EXPLANATIONS, explain
from tests.unit.test_policy_rules import NOW, ctx

ALL_REASON_CODES = {code for codes in rules.REASON_CODES.values() for code in codes}

# One override per failure, each making exactly one rule fail.
FAILURES: dict[str, dict[str, Any]] = {
    "account_inactive": {"account": {"status": "blocked"}},
    "no_connection": {"connection": {"exists": False}},
    "connection_disabled": {"connection": {"is_enabled": False}},
    "connection_stale": {"connection": {"status": "stale"}},
    "no_reply_right": {"connection": {"can_reply": False}},
    "not_owned": {"conversation": {"account_id": 2}},
    "bot_chat": {"conversation": {"is_bot": True}},
    "chat_ignored": {"conversation": {"mode": "ignore"}},
    "chat_possibly_paused": {"conversation": {"availability": "possibly_paused"}},
    "window_closed": {"conversation": {"last_contact_msg_at": NOW - REPLY_WINDOW}},
    "takeover": {"freshness": {"newest_owner_manual_sent_at": NOW - timedelta(minutes=1)}},
    "consent_missing": {"account": {"managed_consent_ok": False}},
    "empty_text": {"action": {"text_length": 0}},
    "too_long": {"action": {"text_length": 4097}},
    "not_assistant_message": {
        "action": {"kind": ActionKind.EDIT_MESSAGE},
        "freshness": {"target_is_assistant_sent": False},
    },
}

EXECUTION_FAILURES: dict[str, dict[str, Any]] = {
    **FAILURES,
    "source_deleted": {"freshness": {"base_message_present": False}},
    "new_contact_message": {"freshness": {"newest_contact_message_id": 101}},
    "already_claimed": {"action": {"state": ActionState.EXECUTING}},
    "max_age": {"action": {"expires_at": NOW}},
    "inbox_pending": {"ingestion": {"pending_inbound": 1}},
    "barrier_pending": {"ingestion": {"barrier_passed": False}},
    "sending_disabled": {"sending_enabled": False},
}


def failed_reasons(decision: Decision) -> list[str]:
    return [c.reason for c in decision.checks if not c.passed]


def test_proposal_requires_approval_when_every_precondition_passes() -> None:
    for origin in (ActionOrigin.OWNER_DICTATED, ActionOrigin.AGENT_PROPOSAL):
        decision = decide(
            ctx(phase=Phase.PROPOSAL, action={"origin": origin, "state": ActionState.PROPOSED})
        )
        assert decision.outcome is Outcome.REQUIRE_APPROVAL
        assert all(c.passed for c in decision.checks)


def test_proposal_never_allows_auto_in_p2() -> None:
    # Even an autonomous origin cannot pass: there are no delegations in P2.
    autonomous = decide(ctx(phase=Phase.PROPOSAL, action={"origin": ActionOrigin.DELEGATION_AUTO}))
    assert autonomous.outcome is Outcome.DENY
    assert failed_reasons(autonomous) == ["no_delegation"]
    # Sending off and an undrained inbox are not proposal-phase facts.
    off = decide(ctx(phase=Phase.PROPOSAL, sending_enabled=False, ingestion={"pending_inbound": 5}))
    assert off.outcome is Outcome.REQUIRE_APPROVAL


@pytest.mark.parametrize("reason", sorted(FAILURES))
def test_proposal_denies_on_any_failed_precondition(reason: str) -> None:
    decision = decide(ctx(phase=Phase.PROPOSAL, **FAILURES[reason]))
    assert decision.outcome is Outcome.DENY
    assert failed_reasons(decision) == [reason]


def test_execution_allows_sending_only_when_every_rule_passes() -> None:
    for origin in (ActionOrigin.OWNER_DICTATED, ActionOrigin.AGENT_PROPOSAL):
        allowed = decide(ctx(action={"origin": origin}))
        assert allowed.outcome is Outcome.ALLOW_AUTO
        assert execution_target(allowed) is None
    for reason, overrides in EXECUTION_FAILURES.items():
        decision = decide(ctx(**overrides))
        assert decision.outcome is not Outcome.ALLOW_AUTO, reason
        assert failed_reasons(decision) == [reason]


OUTDATED = ActionState.OUTDATED
EXPIRED = ActionState.EXPIRED
BLOCKED = ActionState.BLOCKED
AUTONOMOUS: dict[str, Any] = {"action": {"origin": ActionOrigin.DELEGATION_AUTO}}


def merged(*overrides: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for override in overrides:
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(out.get(key), dict):
                out[key] = {**out[key], **value}
            else:
                out[key] = value
    return out


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        # Every row of the spec §3.2 table.
        (EXECUTION_FAILURES["already_claimed"], None),
        (EXECUTION_FAILURES["takeover"], (OUTDATED, StateReason.TAKEOVER)),
        (EXECUTION_FAILURES["new_contact_message"], (OUTDATED, StateReason.NEW_CONTACT_MESSAGE)),
        (EXECUTION_FAILURES["source_deleted"], (OUTDATED, StateReason.SOURCE_DELETED)),
        (EXECUTION_FAILURES["window_closed"], (EXPIRED, StateReason.WINDOW_CLOSED)),
        (EXECUTION_FAILURES["max_age"], (EXPIRED, StateReason.MAX_AGE)),
        (EXECUTION_FAILURES["consent_missing"], (BLOCKED, StateReason.CONSENT_REVOKED)),
        (EXECUTION_FAILURES["sending_disabled"], (BLOCKED, StateReason.SENDING_DISABLED)),
        (EXECUTION_FAILURES["too_long"], (BLOCKED, StateReason.TOO_LONG)),
        (EXECUTION_FAILURES["empty_text"], (BLOCKED, StateReason.TOO_LONG)),
        (
            merged(AUTONOMOUS, {"account": {"autonomy_paused": True}}),
            (ActionState.CANCELLED, StateReason.AUTONOMY_PAUSED),
        ),
        # "Any other" failure.
        (EXECUTION_FAILURES["account_inactive"], (BLOCKED, StateReason.POLICY_DENIED)),
        (EXECUTION_FAILURES["no_connection"], (BLOCKED, StateReason.POLICY_DENIED)),
        (EXECUTION_FAILURES["connection_disabled"], (BLOCKED, StateReason.POLICY_DENIED)),
        (EXECUTION_FAILURES["connection_stale"], (BLOCKED, StateReason.POLICY_DENIED)),
        (EXECUTION_FAILURES["no_reply_right"], (BLOCKED, StateReason.POLICY_DENIED)),
        (EXECUTION_FAILURES["not_owned"], (BLOCKED, StateReason.POLICY_DENIED)),
        (EXECUTION_FAILURES["bot_chat"], (BLOCKED, StateReason.POLICY_DENIED)),
        (EXECUTION_FAILURES["chat_ignored"], (BLOCKED, StateReason.POLICY_DENIED)),
        (EXECUTION_FAILURES["chat_possibly_paused"], (BLOCKED, StateReason.POLICY_DENIED)),
        (EXECUTION_FAILURES["not_assistant_message"], (BLOCKED, StateReason.POLICY_DENIED)),
        (AUTONOMOUS, (BLOCKED, StateReason.POLICY_DENIED)),  # no_delegation
        # The first matching row wins, whatever the rule order.
        (
            merged(EXECUTION_FAILURES["already_claimed"], EXECUTION_FAILURES["takeover"]),
            None,
        ),
        (
            merged(EXECUTION_FAILURES["window_closed"], EXECUTION_FAILURES["takeover"]),
            (OUTDATED, StateReason.TAKEOVER),
        ),
        (
            merged(EXECUTION_FAILURES["takeover"], EXECUTION_FAILURES["new_contact_message"]),
            (OUTDATED, StateReason.TAKEOVER),
        ),
        (
            merged(EXECUTION_FAILURES["max_age"], EXECUTION_FAILURES["window_closed"]),
            (EXPIRED, StateReason.WINDOW_CLOSED),
        ),
        (
            merged(EXECUTION_FAILURES["no_reply_right"], EXECUTION_FAILURES["sending_disabled"]),
            (BLOCKED, StateReason.SENDING_DISABLED),
        ),
        (
            merged(EXECUTION_FAILURES["chat_ignored"], EXECUTION_FAILURES["max_age"]),
            (EXPIRED, StateReason.MAX_AGE),
        ),
        # A pending inbox alongside another failure denies; alone it waits.
        (EXECUTION_FAILURES["inbox_pending"], None),
        (EXECUTION_FAILURES["barrier_pending"], None),
        (
            merged(EXECUTION_FAILURES["inbox_pending"], EXECUTION_FAILURES["no_reply_right"]),
            (BLOCKED, StateReason.POLICY_DENIED),
        ),
        (
            merged(EXECUTION_FAILURES["barrier_pending"], EXECUTION_FAILURES["takeover"]),
            (OUTDATED, StateReason.TAKEOVER),
        ),
    ],
)
def test_execution_failure_maps_to_its_state(
    overrides: dict[str, Any], expected: tuple[ActionState, StateReason] | None
) -> None:
    decision = decide(ctx(**overrides))
    assert decision.outcome is not Outcome.ALLOW_AUTO
    assert execution_target(decision) == expected


def test_execution_targets_are_legal_transitions_from_approved() -> None:
    for overrides in EXECUTION_FAILURES.values():
        target = execution_target(decide(ctx(**overrides)))
        if target is not None:
            assert (ActionState.APPROVED, target[0]) in TRANSITIONS
    paused = execution_target(
        decide(ctx(**merged(AUTONOMOUS, {"account": {"autonomy_paused": True}})))
    )
    assert paused is not None
    assert (ActionState.APPROVED, paused[0]) in TRANSITIONS


def test_wait_only_when_inbox_drained_is_the_sole_failure() -> None:
    for reason in ("inbox_pending", "barrier_pending"):
        alone = decide(ctx(**EXECUTION_FAILURES[reason]))
        assert alone.outcome is Outcome.WAIT
        assert failed_reasons(alone) == [reason]
        for other, overrides in EXECUTION_FAILURES.items():
            if other in {"inbox_pending", "barrier_pending"}:
                continue
            together = decide(ctx(**merged(EXECUTION_FAILURES[reason], overrides)))
            assert together.outcome is Outcome.DENY, other
    # At proposal the inbox is not consulted at all.
    proposal = decide(ctx(phase=Phase.PROPOSAL, **EXECUTION_FAILURES["inbox_pending"]))
    assert proposal.outcome is Outcome.REQUIRE_APPROVAL


def test_checks_list_every_evaluated_rule_in_order() -> None:
    names = [rule.__name__ for rule in rules.RULES]
    full = decide(
        ctx(action={"kind": ActionKind.EDIT_MESSAGE, "origin": ActionOrigin.DELEGATION_AUTO})
    )
    assert [c.name for c in full.checks] == names

    send = decide(ctx())
    assert [c.name for c in send.checks] == [
        "account_active",
        "connection_active",
        "conversation_owned",
        "conversation_available",
        "window_open",
        "no_takeover_since",
        "not_stale",
        "not_already_executed",
        "not_expired",
        "inbox_drained",
        "length_ok",
        "consent_valid",
        "sending_enabled",
    ]
    proposal = decide(ctx(phase=Phase.PROPOSAL, action={"kind": ActionKind.DELETE_OWN}))
    assert [c.name for c in proposal.checks] == [
        "account_active",
        "connection_active",
        "conversation_owned",
        "conversation_available",
        "window_open",
        "no_takeover_since",
        "target_is_assistant_sent",
        "consent_valid",
    ]
    # Every failure is reported, not just the first.
    many = decide(ctx(account={"status": "blocked"}, sending_enabled=False))
    assert failed_reasons(many) == ["account_inactive", "sending_disabled"]


def test_every_reason_code_has_an_explanation() -> None:
    assert set(EXPLANATIONS) == ALL_REASON_CODES
    for code, sentence in EXPLANATIONS.items():
        assert sentence.strip() == sentence, code
        assert sentence[0].isupper(), code
        assert sentence.endswith("."), code
    with pytest.raises(TypeError):
        EXPLANATIONS["window_closed"] = "changed"  # type: ignore[index]


def test_spec_example_sentences_are_used_verbatim() -> None:
    assert EXPLANATIONS["window_closed"] == (
        "Telegram's 24-hour reply window for this chat has closed, so a reply cannot be "
        "sent. Copy the text and send it yourself if you still want to."
    )
    assert EXPLANATIONS["no_reply_right"] == (
        'Alviro is connected without the right to reply in your chats. Turn on "Reply to '
        "messages\" in Telegram's Chat Automation settings, or copy the text and send it "
        "yourself."
    )
    assert EXPLANATIONS["takeover"] == (
        "You replied in this chat yourself after this draft was made."
    )


def test_explanations_render_for_each_failed_check() -> None:
    assert explain(decide(ctx())) == ()
    for reason, overrides in EXECUTION_FAILURES.items():
        assert explain(decide(ctx(**overrides))) == (EXPLANATIONS[reason],)
    many = decide(
        ctx(
            connection={"can_reply": False},
            conversation={"last_contact_msg_at": None},
            freshness={"newest_owner_manual_sent_at": NOW},
        )
    )
    assert explain(many) == (
        EXPLANATIONS["no_reply_right"],
        EXPLANATIONS["window_closed"],
        EXPLANATIONS["takeover"],
    )


def test_explain_is_total_for_unknown_codes() -> None:
    # Decisions stored by an older build may carry a code this build lacks.
    stored = Decision(Outcome.DENY, (CheckResult("future_rule", False, "future_code"),))
    (sentence,) = explain(stored)
    assert sentence


def test_explanations_never_contain_placeholders_for_content() -> None:
    placeholder = re.compile(r"[{}<>%$]|\bTODO\b|\bXXX\b")
    for code, sentence in EXPLANATIONS.items():
        assert not placeholder.search(sentence), code
        # One or two plain sentences, not a template.
        assert 1 <= sentence.count(". ") + 1 <= 2, code


# --- Totality and purity --------------------------------------------------------

_times = st.datetimes(
    min_value=datetime(2026, 1, 1),  # noqa: DTZ001 - hypothesis bounds are naive
    max_value=datetime(2027, 12, 31),  # noqa: DTZ001
    timezones=st.just(UTC),
)
_maybe_time = st.none() | _times
_maybe_id = st.none() | st.integers(min_value=0, max_value=1_000)
_free = st.sampled_from(["", "unknown"])  # values outside the database enums

_contexts = st.builds(
    PolicyContext,
    now=_times,
    phase=st.sampled_from(Phase),
    account=st.builds(
        AccountSnapshot,
        account_id=st.integers(1, 3),
        status=st.sampled_from(["onboarding", "active", "blocked", "deleted"]) | _free,
        autonomy_paused=st.booleans(),
        managed_consent_ok=st.booleans(),
    ),
    connection=st.builds(
        ConnectionSnapshot,
        exists=st.booleans(),
        status=st.none() | st.sampled_from(["pending_owner", "active", "disabled", "stale"]),
        is_enabled=st.booleans(),
        can_reply=st.booleans(),
    ),
    conversation=st.builds(
        ConversationSnapshot,
        conversation_id=st.integers(1, 3),
        account_id=st.integers(1, 3),
        mode=st.sampled_from(["ignore", "observe", "assist"]) | _free,
        availability=st.sampled_from(["active", "possibly_paused"]) | _free,
        is_bot=st.booleans(),
        last_contact_msg_at=_maybe_time,
        last_owner_msg_at=_maybe_time,
        autonomy_paused_reason=st.none() | st.sampled_from(["takeover", "ingest_error"]),
    ),
    freshness=st.builds(
        Freshness,
        newest_owner_manual_sent_at=_maybe_time,
        newest_contact_message_id=_maybe_id,
        base_message_sent_at=_maybe_time,
        base_message_present=st.booleans(),
        target_is_assistant_sent=st.none() | st.booleans(),
    ),
    ingestion=st.builds(
        IngestionState,
        pending_inbound=st.integers(0, 5),
        barrier_passed=st.booleans(),
    ),
    action=st.builds(
        ActionFacts,
        action_id=_maybe_id,
        kind=st.sampled_from(ActionKind),
        origin=st.sampled_from(ActionOrigin),
        state=st.sampled_from(ActionState),
        version=st.integers(1, 5),
        text_length=st.integers(0, 5_000),
        created_at=_times,
        expires_at=_maybe_time,
        shown_through_message_id=_maybe_id,
        owner_retry=st.booleans(),
    ),
    ai_generated=st.booleans(),
    sending_enabled=st.booleans(),
)


@settings(
    derandomize=True,
    deadline=None,
    max_examples=200,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(_contexts)
def test_decide_is_pure_and_total(context: PolicyContext) -> None:
    decision = decide(context)
    assert decide(context) == decision
    assert decide(replace(context)) == decision

    names = [c.name for c in decision.checks]
    order = [rule.__name__ for rule in rules.RULES]
    assert names == [n for n in order if n in names]
    for check in decision.checks:
        expected = rules.REASON_CODES[check.name]
        if check.passed:
            assert check.reason == rules.PASSED
        else:
            assert check.reason in expected

    failed = [c for c in decision.checks if not c.passed]
    if context.phase is Phase.PROPOSAL:
        expected_outcome = Outcome.DENY if failed else Outcome.REQUIRE_APPROVAL
    elif not failed:
        expected_outcome = Outcome.ALLOW_AUTO
    elif {c.name for c in failed} == {"inbox_drained"}:
        expected_outcome = Outcome.WAIT
    else:
        expected_outcome = Outcome.DENY
    assert decision.outcome is expected_outcome

    target = execution_target(decision)
    assert len(explain(decision)) == len(failed)
    if target is not None:
        assert decision.outcome is Outcome.DENY
        assert (ActionState.APPROVED, target[0]) in TRANSITIONS
