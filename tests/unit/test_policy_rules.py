"""The S04.1 rule table (spec §3.1; ARCHITECTURE §9.3), one parametrised table per rule.

`ctx()` builds an execution-phase context in which every rule passes; each case
overrides only the facts it is about, so a failure names exactly one rule.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from assistant.actions.states import ActionKind, ActionOrigin, ActionState
from assistant.core.delivery import MAX_TEXT_LENGTH
from assistant.core.reply_window import REPLY_WINDOW, SAFETY_MARGIN
from assistant.policy import rules
from assistant.policy.context import (
    AccountSnapshot,
    ActionFacts,
    CheckResult,
    ConnectionSnapshot,
    ConversationSnapshot,
    Freshness,
    IngestionState,
    Phase,
    PolicyContext,
)

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
CONTACT_AT = NOW - timedelta(minutes=40)
CREATED_AT = NOW - timedelta(minutes=30)

_BASE = PolicyContext(
    now=NOW,
    phase=Phase.EXECUTION,
    account=AccountSnapshot(
        account_id=1, status="active", autonomy_paused=False, managed_consent_ok=True
    ),
    connection=ConnectionSnapshot(exists=True, status="active", is_enabled=True, can_reply=True),
    conversation=ConversationSnapshot(
        conversation_id=10,
        account_id=1,
        mode="observe",
        availability="active",
        is_bot=False,
        last_contact_msg_at=CONTACT_AT,
        last_owner_msg_at=None,
        autonomy_paused_reason=None,
    ),
    freshness=Freshness(
        newest_owner_manual_sent_at=None,
        newest_contact_message_id=100,
        base_message_sent_at=CONTACT_AT,
        base_message_present=True,
        target_is_assistant_sent=None,
    ),
    ingestion=IngestionState(pending_inbound=0, barrier_passed=True),
    action=ActionFacts(
        action_id=7,
        kind=ActionKind.SEND_MESSAGE,
        origin=ActionOrigin.AGENT_PROPOSAL,
        state=ActionState.APPROVED,
        version=1,
        text_length=12,
        created_at=CREATED_AT,
        expires_at=NOW + timedelta(hours=1),
        shown_through_message_id=100,
        owner_retry=False,
    ),
    ai_generated=True,
    sending_enabled=True,
)


def ctx(**overrides: Any) -> PolicyContext:
    """The passing execution context with fields replaced.

    A dict value for a nested snapshot replaces only the named fields of that
    snapshot: `ctx(account={"status": "blocked"})`.
    """
    changes: dict[str, Any] = {}
    for name, value in overrides.items():
        current = getattr(_BASE, name)
        if isinstance(value, dict) and is_dataclass(current):
            changes[name] = replace(current, **value)
        else:
            changes[name] = value
    return replace(_BASE, **changes)


def outcome(result: CheckResult | None) -> tuple[bool, str] | None:
    return None if result is None else (result.passed, result.reason)


OK = (True, "ok")


def test_the_default_context_passes_every_rule() -> None:
    results = [rule(ctx()) for rule in rules.RULES]
    assert all(r is None or r.passed for r in results)


def test_rule_names_and_order_match_the_contract() -> None:
    assert [rule.__name__ for rule in rules.RULES] == [
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
        "target_is_assistant_sent",
        "length_ok",
        "consent_valid",
        "sending_enabled",
        "autonomy_not_paused",
        "delegation_active",
    ]
    assert list(rules.REASON_CODES) == [rule.__name__ for rule in rules.RULES]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("active", OK),
        ("onboarding", (False, "account_inactive")),
        ("blocked", (False, "account_inactive")),
        ("deleted", (False, "account_inactive")),
    ],
)
def test_account_active_rule(status: str, expected: tuple[bool, str]) -> None:
    for phase in Phase:
        result = rules.account_active(ctx(phase=phase, account={"status": status}))
        assert outcome(result) == expected


@pytest.mark.parametrize(
    ("connection", "expected"),
    [
        ({}, OK),
        (
            {"exists": False, "status": None, "is_enabled": False, "can_reply": False},
            (False, "no_connection"),
        ),
        # A row that does not exist wins over whatever stale values it carries.
        ({"exists": False}, (False, "no_connection")),
        ({"is_enabled": False}, (False, "connection_disabled")),
        ({"status": "disabled"}, (False, "connection_disabled")),
        ({"status": "pending_owner"}, (False, "connection_disabled")),
        ({"status": None}, (False, "connection_disabled")),
        ({"status": "stale"}, (False, "connection_stale")),
        # Stale says more (reconnect) than the disabled flag it usually comes with.
        ({"status": "stale", "is_enabled": False}, (False, "connection_stale")),
        ({"can_reply": False}, (False, "no_reply_right")),
        # Rights are only meaningful on a live connection.
        ({"is_enabled": False, "can_reply": False}, (False, "connection_disabled")),
        ({"status": "stale", "can_reply": False}, (False, "connection_stale")),
    ],
)
def test_connection_active_rule_cases(
    connection: dict[str, Any], expected: tuple[bool, str]
) -> None:
    for phase in Phase:
        result = rules.connection_active(ctx(phase=phase, connection=connection))
        assert outcome(result) == expected


@pytest.mark.parametrize(
    ("conversation", "expected"),
    [
        ({}, OK),
        ({"account_id": 2}, (False, "not_owned")),
        ({"is_bot": True}, (False, "bot_chat")),
        ({"account_id": 2, "is_bot": True}, (False, "not_owned")),
    ],
)
def test_conversation_owned_rule_refuses_foreign_and_bot_chats(
    conversation: dict[str, Any], expected: tuple[bool, str]
) -> None:
    for phase in Phase:
        result = rules.conversation_owned(ctx(phase=phase, conversation=conversation))
        assert outcome(result) == expected


@pytest.mark.parametrize(
    ("mode", "availability", "owner_retry", "expected"),
    [
        ("observe", "active", False, OK),
        ("assist", "active", False, OK),
        ("ignore", "active", False, (False, "chat_ignored")),
        # An owner retry overrides "possibly paused", never Ignore.
        ("ignore", "active", True, (False, "chat_ignored")),
        ("ignore", "possibly_paused", True, (False, "chat_ignored")),
        ("observe", "possibly_paused", False, (False, "chat_possibly_paused")),
        ("observe", "possibly_paused", True, OK),
    ],
)
def test_conversation_available_rule(
    mode: str, availability: str, owner_retry: bool, expected: tuple[bool, str]
) -> None:
    for phase in Phase:
        context = ctx(
            phase=phase,
            conversation={"mode": mode, "availability": availability},
            action={"owner_retry": owner_retry},
        )
        assert outcome(rules.conversation_available(context)) == expected


@pytest.mark.parametrize(
    ("contact_at", "owner_at", "expected"),
    [
        (NOW - timedelta(hours=1), None, OK),
        # Exactly at close - margin the window counts as closed.
        (NOW - REPLY_WINDOW + SAFETY_MARGIN, None, (False, "window_closed")),
        (NOW - REPLY_WINDOW + SAFETY_MARGIN + timedelta(seconds=1), None, OK),
        (NOW - REPLY_WINDOW, None, (False, "window_closed")),
        # No contact message: no window, whatever the owner did.
        (None, NOW - timedelta(minutes=1), (False, "window_closed")),
        # The owner's own later messages never extend the window.
        (NOW - REPLY_WINDOW, NOW - timedelta(minutes=1), (False, "window_closed")),
    ],
)
def test_window_open_rule_uses_the_contact_anchor_and_the_margin(
    contact_at: datetime | None, owner_at: datetime | None, expected: tuple[bool, str]
) -> None:
    """Autonomous sends keep the conservative pre-check (invariant 8)."""
    for phase in Phase:
        context = ctx(
            phase=phase,
            conversation={"last_contact_msg_at": contact_at, "last_owner_msg_at": owner_at},
            action={"origin": ActionOrigin.DELEGATION_AUTO},
        )
        assert outcome(rules.window_open(context)) == expected


@pytest.mark.parametrize("origin", [ActionOrigin.OWNER_DICTATED, ActionOrigin.AGENT_PROPOSAL])
@pytest.mark.parametrize("contact_at", [NOW - timedelta(hours=1), NOW - REPLY_WINDOW, None])
def test_window_open_rule_does_not_apply_to_owner_approved_actions(
    origin: ActionOrigin, contact_at: datetime | None
) -> None:
    """Owner decision 2026-10-30 (P2-O20): an owner-approved send is attempted
    once and Telegram enforces the window; our estimate is no check for it."""
    for phase in Phase:
        context = ctx(
            phase=phase, conversation={"last_contact_msg_at": contact_at}, action={"origin": origin}
        )
        assert rules.window_open(context) is None


@pytest.mark.parametrize("kind", [ActionKind.EDIT_MESSAGE, ActionKind.DELETE_OWN])
@pytest.mark.parametrize("origin", [ActionOrigin.OWNER_DICTATED, ActionOrigin.AGENT_PROPOSAL])
def test_window_open_rule_still_applies_to_owner_origin_edits_and_deletes(
    kind: ActionKind, origin: ActionOrigin
) -> None:
    """P2-O20 exempts owner-approved sends only; edits keep the pre-check."""
    for phase in Phase:
        context = ctx(
            phase=phase,
            conversation={"last_contact_msg_at": NOW - REPLY_WINDOW},
            action={"origin": origin, "kind": kind},
        )
        assert outcome(rules.window_open(context)) == (False, "window_closed")


BASE_AT = NOW - timedelta(hours=2)


@pytest.mark.parametrize(
    ("owner_at", "expected"),
    [
        (None, OK),
        (BASE_AT - timedelta(minutes=1), OK),
        (BASE_AT, OK),
        (BASE_AT + timedelta(seconds=1), (False, "takeover")),
        # Dated after the base even though it is older than the action itself:
        # an owner message ingested late still counts as a takeover.
        (CREATED_AT - timedelta(minutes=5), (False, "takeover")),
    ],
)
def test_no_takeover_since_compares_telegram_dates_with_the_base_message(
    owner_at: datetime | None, expected: tuple[bool, str]
) -> None:
    for phase in Phase:
        context = ctx(
            phase=phase,
            freshness={"base_message_sent_at": BASE_AT, "newest_owner_manual_sent_at": owner_at},
        )
        assert outcome(rules.no_takeover_since(context)) == expected


@pytest.mark.parametrize(
    ("owner_at", "expected"),
    [
        (None, OK),
        (CREATED_AT - timedelta(hours=3), OK),
        (CREATED_AT, OK),
        (CREATED_AT + timedelta(seconds=1), (False, "takeover")),
    ],
)
def test_no_takeover_since_without_a_base_uses_created_at(
    owner_at: datetime | None, expected: tuple[bool, str]
) -> None:
    for phase in Phase:
        context = ctx(
            phase=phase,
            freshness={"base_message_sent_at": None, "newest_owner_manual_sent_at": owner_at},
        )
        assert outcome(rules.no_takeover_since(context)) == expected


def test_no_takeover_since_uses_created_at_for_owner_requested_actions() -> None:
    # Decisions R3: the loader leaves base_message_sent_at unset for owner-requested
    # actions, so the owner's own earlier manual reply (after the contact's message)
    # does not outdate what the owner has just asked for.
    owner_replied_at = CONTACT_AT + timedelta(minutes=2)
    for origin in (ActionOrigin.OWNER_DICTATED, ActionOrigin.AGENT_PROPOSAL):
        for phase in Phase:
            context = ctx(
                phase=phase,
                action={"origin": origin, "created_at": owner_replied_at + timedelta(minutes=1)},
                freshness={
                    "base_message_sent_at": None,
                    "newest_owner_manual_sent_at": owner_replied_at,
                },
            )
            assert outcome(rules.no_takeover_since(context)) == OK
            # ...while a manual reply after the request still takes over.
            later = replace(
                context,
                freshness=replace(
                    context.freshness,
                    newest_owner_manual_sent_at=owner_replied_at + timedelta(minutes=2),
                ),
            )
            assert outcome(rules.no_takeover_since(later)) == (False, "takeover")


@pytest.mark.parametrize(
    ("newest_contact", "shown_through", "expected"),
    [
        (None, None, OK),
        (None, 5, OK),
        (100, 100, OK),
        (99, 100, OK),
        (101, 100, (False, "new_contact_message")),
        # A contact message exists but the draft was shown through nothing.
        (100, None, (False, "new_contact_message")),
    ],
)
def test_not_stale_compares_ingestion_order_ids(
    newest_contact: int | None, shown_through: int | None, expected: tuple[bool, str]
) -> None:
    context = ctx(
        freshness={"newest_contact_message_id": newest_contact},
        action={"shown_through_message_id": shown_through},
    )
    assert outcome(rules.not_stale(context)) == expected


def test_not_stale_fails_when_the_base_message_was_deleted() -> None:
    gone = ctx(freshness={"base_message_present": False})
    assert outcome(rules.not_stale(gone)) == (False, "source_deleted")
    # A deleted source is reported even when a newer contact message also exists.
    both = ctx(freshness={"base_message_present": False, "newest_contact_message_id": 101})
    assert outcome(rules.not_stale(both)) == (False, "source_deleted")


@pytest.mark.parametrize("state", list(ActionState))
def test_not_already_executed_requires_approved(state: ActionState) -> None:
    result = rules.not_already_executed(ctx(action={"state": state}))
    expected = OK if state is ActionState.APPROVED else (False, "already_claimed")
    assert outcome(result) == expected


@pytest.mark.parametrize(
    ("expires_at", "expected"),
    [
        (None, OK),
        (NOW + timedelta(seconds=1), OK),
        (NOW, (False, "max_age")),
        (NOW - timedelta(seconds=1), (False, "max_age")),
    ],
)
def test_not_expired_rule(expires_at: datetime | None, expected: tuple[bool, str]) -> None:
    assert outcome(rules.not_expired(ctx(action={"expires_at": expires_at}))) == expected


@pytest.mark.parametrize(
    ("pending", "expected"),
    [(0, OK), (1, (False, "inbox_pending")), (250, (False, "inbox_pending"))],
)
def test_inbox_drained_gives_wait(pending: int, expected: tuple[bool, str]) -> None:
    # The WAIT outcome itself is the engine's (test_policy_engine); the rule
    # reports which half of the drain is missing.
    context = ctx(ingestion={"pending_inbound": pending})
    assert outcome(rules.inbox_drained(context)) == expected


def test_inbox_drained_waits_for_the_barrier() -> None:
    context = ctx(ingestion={"pending_inbound": 0, "barrier_passed": False})
    assert outcome(rules.inbox_drained(context)) == (False, "barrier_pending")
    # Pending rows are named first: they are the concrete thing to wait for.
    both = ctx(ingestion={"pending_inbound": 3, "barrier_passed": False})
    assert outcome(rules.inbox_drained(both)) == (False, "inbox_pending")


def test_inbox_drained_waits_when_the_barrier_belongs_to_another_process_run() -> None:
    # Decisions R1: the loader reports a barrier written by a previous process run
    # as not passed; the rule must then hold the send, however empty the inbox is.
    previous_run = IngestionState(pending_inbound=0, barrier_passed=False)
    assert outcome(rules.inbox_drained(ctx(ingestion=previous_run))) == (
        False,
        "barrier_pending",
    )


@pytest.mark.parametrize(
    ("kind", "target", "expected"),
    [
        (ActionKind.EDIT_MESSAGE, True, OK),
        (ActionKind.DELETE_OWN, True, OK),
        (ActionKind.EDIT_MESSAGE, False, (False, "not_assistant_message")),
        (ActionKind.DELETE_OWN, False, (False, "not_assistant_message")),
        (ActionKind.EDIT_MESSAGE, None, (False, "not_assistant_message")),
        (ActionKind.DELETE_OWN, None, (False, "not_assistant_message")),
        (ActionKind.SEND_MESSAGE, None, None),
        (ActionKind.SEND_MESSAGE, False, None),
    ],
)
def test_rg12_target_is_assistant_sent_denies_owner_and_contact_targets(
    kind: ActionKind, target: bool | None, expected: tuple[bool, str] | None
) -> None:
    # False = the target is the owner's or a contact's message; None = it does not
    # resolve to a row of this conversation. Both are refused (invariant 14).
    for phase in Phase:
        context = ctx(
            phase=phase, action={"kind": kind}, freshness={"target_is_assistant_sent": target}
        )
        assert outcome(rules.target_is_assistant_sent(context)) == expected


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


@pytest.mark.parametrize(
    ("length", "expected"),
    [
        (0, (False, "empty_text")),
        (1, OK),
        (MAX_TEXT_LENGTH, OK),
        (MAX_TEXT_LENGTH + 1, (False, "too_long")),
        # 2048 emoji are 4096 UTF-16 units; one more pushes it over, although
        # it is only 2049 code points.
        (utf16_units("\U0001f600" * 2048), OK),
        (utf16_units("\U0001f600" * 2049), (False, "too_long")),
    ],
)
def test_length_ok_counts_utf16_units_and_rejects_empty_and_over_4096(
    length: int, expected: tuple[bool, str]
) -> None:
    assert MAX_TEXT_LENGTH == 4096
    for phase in Phase:
        for kind in (ActionKind.SEND_MESSAGE, ActionKind.EDIT_MESSAGE):
            context = ctx(phase=phase, action={"kind": kind, "text_length": length})
            assert outcome(rules.length_ok(context)) == expected
        # A deletion has no text to measure.
        deletion = ctx(phase=phase, action={"kind": ActionKind.DELETE_OWN, "text_length": length})
        assert rules.length_ok(deletion) is None


@pytest.mark.parametrize(
    ("ai_generated", "consent_ok", "expected"),
    [
        (True, True, OK),
        (True, False, (False, "consent_missing")),
        # Owner-dictated text never went through a model.
        (False, False, OK),
        (False, True, OK),
    ],
)
def test_consent_valid_applies_only_to_ai_generated_text(
    ai_generated: bool, consent_ok: bool, expected: tuple[bool, str]
) -> None:
    for phase in Phase:
        context = ctx(
            phase=phase,
            ai_generated=ai_generated,
            account={"managed_consent_ok": consent_ok},
        )
        assert outcome(rules.consent_valid(context)) == expected


def test_sending_enabled_is_checked_only_at_execution() -> None:
    # P2-T21: with sending off, proposals still become ordinary approval cards.
    assert rules.sending_enabled(ctx(phase=Phase.PROPOSAL, sending_enabled=False)) is None
    assert rules.sending_enabled(ctx(phase=Phase.PROPOSAL, sending_enabled=True)) is None
    assert outcome(rules.sending_enabled(ctx(sending_enabled=True))) == OK
    assert outcome(rules.sending_enabled(ctx(sending_enabled=False))) == (
        False,
        "sending_disabled",
    )


@pytest.mark.parametrize(
    ("account_paused", "conversation_reason", "expected"),
    [
        (False, None, OK),
        (True, None, (False, "autonomy_paused")),
        (False, "takeover", (False, "autonomy_paused")),
        (False, "ingest_error", (False, "autonomy_paused")),
        (True, "takeover", (False, "autonomy_paused")),
    ],
)
def test_rg19_an_autonomous_action_is_denied_while_paused(
    account_paused: bool, conversation_reason: str | None, expected: tuple[bool, str]
) -> None:
    for phase in Phase:
        context = ctx(
            phase=phase,
            action={"origin": ActionOrigin.DELEGATION_AUTO},
            account={"autonomy_paused": account_paused},
            conversation={"autonomy_paused_reason": conversation_reason},
        )
        assert outcome(rules.autonomy_not_paused(context)) == expected
        # Pausing autonomy never blocks what the owner approved by hand.
        for origin in (ActionOrigin.OWNER_DICTATED, ActionOrigin.AGENT_PROPOSAL):
            owner = replace(context, action=replace(context.action, origin=origin))
            assert rules.autonomy_not_paused(owner) is None


def test_an_autonomous_action_never_passes_without_a_delegation_in_p2() -> None:
    for phase in Phase:
        context = ctx(phase=phase, action={"origin": ActionOrigin.DELEGATION_AUTO})
        assert context.delegation is None
        assert outcome(rules.delegation_active(context)) == (False, "no_delegation")
        for origin in (ActionOrigin.OWNER_DICTATED, ActionOrigin.AGENT_PROPOSAL):
            owner = ctx(phase=phase, action={"origin": origin})
            assert rules.delegation_active(owner) is None


EXECUTION_ONLY = {
    "not_stale",
    "not_already_executed",
    "not_expired",
    "inbox_drained",
    "sending_enabled",
}


@pytest.mark.parametrize("phase", list(Phase))
@pytest.mark.parametrize("kind", list(ActionKind))
@pytest.mark.parametrize("origin", list(ActionOrigin))
def test_rules_not_applicable_to_a_phase_or_kind_are_left_out(
    phase: Phase, kind: ActionKind, origin: ActionOrigin
) -> None:
    context = ctx(phase=phase, action={"kind": kind, "origin": origin})
    applicable = {rule.__name__ for rule in rules.RULES if rule(context) is not None}
    expected = {rule.__name__ for rule in rules.RULES}
    if phase is Phase.PROPOSAL:
        expected -= EXECUTION_ONLY
    if kind is ActionKind.SEND_MESSAGE:
        expected.discard("target_is_assistant_sent")
    if kind is ActionKind.DELETE_OWN:
        expected.discard("length_ok")
    if origin is not ActionOrigin.DELEGATION_AUTO:
        expected -= {"autonomy_not_paused", "delegation_active"}
        if kind is ActionKind.SEND_MESSAGE:
            # P2-O20: our window estimate no longer bounds owner-approved sends.
            expected.discard("window_open")
    assert applicable == expected


def test_every_rule_reports_only_its_declared_reason_codes() -> None:
    # The codes a failing rule may emit are declared next to it, so explanations
    # and the execution mapping can be checked against one list.
    for rule in rules.RULES:
        assert rules.REASON_CODES[rule.__name__], rule.__name__
    all_codes = [code for codes in rules.REASON_CODES.values() for code in codes]
    assert len(all_codes) == len(set(all_codes)), "a reason code belongs to one rule"
    assert rules.PASSED not in all_codes


def test_rules_read_only_the_context() -> None:
    # Pure: the same context gives equal results, and a frozen context is not mutated.
    context = ctx()
    before = {f.name: getattr(context, f.name) for f in fields(context)}
    first = [rule(context) for rule in rules.RULES]
    second = [rule(context) for rule in rules.RULES]
    assert first == second
    assert before == {f.name: getattr(context, f.name) for f in fields(context)}
