"""The reviewed action lifecycle, including settled and uncertain outcomes."""

from assistant.actions.states import (
    LIVE_CARD,
    PENDING,
    STATE_REASON_EXPLANATIONS,
    TERMINAL,
    TRANSITIONS,
    ActionState,
    StateReason,
)


def test_transitions_match_the_architecture_diagram() -> None:
    expected = {
        ("proposed", "awaiting_approval"),
        ("proposed", "approved"),
        ("proposed", "blocked"),
        ("proposed", "cancelled"),
        ("awaiting_approval", "approved"),
        ("awaiting_approval", "revising"),
        ("awaiting_approval", "rejected"),
        ("awaiting_approval", "outdated"),
        ("awaiting_approval", "expired"),
        ("awaiting_approval", "cancelled"),
        ("revising", "awaiting_approval"),
        ("revising", "outdated"),
        ("revising", "expired"),
        ("revising", "cancelled"),
        ("approved", "scheduled"),
        ("approved", "executing"),
        ("approved", "outdated"),
        ("approved", "expired"),
        ("approved", "blocked"),
        ("approved", "cancelled"),
        ("scheduled", "approved"),
        ("scheduled", "cancelled"),
        ("executing", "succeeded"),
        ("executing", "failed"),
        ("executing", "approved"),
        ("executing", "delivery_uncertain"),
        ("executing", "expired"),
        ("delivery_uncertain", "succeeded"),
        ("delivery_uncertain", "failed"),
    }
    assert expected == TRANSITIONS


def test_settled_states_have_no_way_back_to_sending() -> None:
    assert not any(source in TERMINAL for source, _ in TRANSITIONS)


def test_delivery_uncertain_moves_only_to_succeeded_or_failed() -> None:
    assert {target for source, target in TRANSITIONS if source == "delivery_uncertain"} == {
        ActionState.SUCCEEDED,
        ActionState.FAILED,
    }


def test_every_state_reason_has_an_owner_explanation() -> None:
    assert set(STATE_REASON_EXPLANATIONS) == set(StateReason)
    assert all(text.strip() for text in STATE_REASON_EXPLANATIONS.values())


def test_state_sets_cover_the_lifecycle() -> None:
    assert LIVE_CARD < PENDING
    assert not PENDING & TERMINAL
    assert set(ActionState) == PENDING | TERMINAL | {
        ActionState.EXECUTING,
        ActionState.DELIVERY_UNCERTAIN,
    }


def test_executing_may_expire_and_proposed_may_be_cancelled() -> None:
    assert (ActionState.EXECUTING, ActionState.EXPIRED) in TRANSITIONS
    assert (ActionState.PROPOSED, ActionState.CANCELLED) in TRANSITIONS
