"""Content-free audit values and CLI failure boundaries."""

from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime

import psycopg
import pytest
from tools.acceptance import p2_verify
from tools.acceptance.p2_verify import details

from assistant.actions.states import ActionState, StateReason


def test_verifier_state_and_reason_allowlists_match_the_lifecycle() -> None:
    assert {state.value for state in ActionState} == p2_verify.STATES
    assert {reason.value for reason in StateReason} == p2_verify.REASONS


def test_trial_check_accepts_an_explicit_aware_interval() -> None:
    args = p2_verify.parser().parse_args(
        [
            "trial-check",
            "--since",
            "2026-10-28T08:00:00+05:00",
            "--until",
            "2026-10-28T05:00:00Z",
        ]
    )
    assert args.mode == "trial-check"
    assert args.since == datetime(2026, 10, 28, 3, tzinfo=UTC)
    assert args.until == datetime(2026, 10, 28, 5, tzinfo=UTC)


@pytest.mark.parametrize(
    "interval",
    [
        ("2026-10-28T04:00:00Z", "2026-10-28T04:00:00Z"),
        ("2026-10-28T05:00:00Z", "2026-10-28T04:00:00Z"),
        ("2026-10-28T09:00:00+05:00", "2026-10-28T04:00:00Z"),
        ("2026-10-28", "2026-10-29T04:00:00Z"),
        ("2026-10-28T04:00:00Z", "not-a-timestamp"),
    ],
)
def test_trial_check_rejects_invalid_ranges_before_opening_the_database(
    monkeypatch: pytest.MonkeyPatch, interval: tuple[str, str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic")

    def forbidden(_url: str) -> None:
        raise AssertionError("invalid trial interval reached the database")

    monkeypatch.setattr(p2_verify, "open_database", forbidden)
    with pytest.raises(SystemExit) as error:
        p2_verify.main(["trial-check", "--since", interval[0], "--until", interval[1]])
    assert error.value.code == 2


@pytest.mark.parametrize("status", [0, 1])
def test_trial_check_cli_preserves_the_checkers_exit_status(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic")
    connection = object()
    monkeypatch.setattr(p2_verify, "open_database", lambda _url: nullcontext(connection))
    calls = []

    def check(conn: object, since: datetime, until: datetime) -> int:
        calls.append((conn, since, until))
        return status

    monkeypatch.setattr(p2_verify, "trial_check", check)
    assert (
        p2_verify.main(
            [
                "trial-check",
                "--since",
                "2026-10-28T03:00:00Z",
                "--until",
                "2026-10-28T05:00:00Z",
            ]
        )
        == status
    )
    assert calls == [
        (connection, datetime(2026, 10, 28, 3, tzinfo=UTC), datetime(2026, 10, 28, 5, tzinfo=UTC))
    ]


def test_audit_details_accept_only_structural_values() -> None:
    result = details(
        {
            "from": {"private": "canary"},
            "to": "succeeded",
            "reason": "canary",
            "version": "canary",
            "retry_after": ["canary"],
            "identity_pending": "canary",
            "attempt_id": "canary",
            "text": "canary",
        }
    )
    assert result == "to=succeeded"


@pytest.mark.parametrize("error_type", [RuntimeError, TypeError, psycopg.OperationalError])
def test_unexpected_failures_print_only_the_exception_class(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_type: type[Exception],
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic")
    monkeypatch.setattr(p2_verify, "open_database", lambda _url: nullcontext(None))

    def fail(_conn: object) -> None:
        raise error_type("synthetic-private-error-canary")

    monkeypatch.setattr(p2_verify, "release", fail)
    assert p2_verify.main(["release"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == f"verification_failed type={error_type.__name__}\n"


def history_event(name: str, order: int, attempt: str = "a") -> p2_verify._TrialEvent:
    before, after = {
        "action_approved": ("awaiting_approval", "approved"),
        "action_claimed": ("approved", "executing"),
        "action_rate_limited": ("executing", "approved"),
        "action_succeeded": ("executing", "succeeded"),
        "action_failed": ("executing", "failed"),
        "action_expired": ("executing", "expired"),
        "action_delivery_uncertain": ("executing", "delivery_uncertain"),
        "action_late_result": ("delivery_uncertain", "succeeded"),
    }[name]
    return p2_verify._TrialEvent(
        1,
        order,
        datetime(2026, 10, 28, tzinfo=UTC),
        name,
        1,
        attempt,
        before,
        after,
        "owner" if name == "action_approved" else "system",
    )


def history_codes(state: str, events: list[p2_verify._TrialEvent]) -> set[str]:
    action = p2_verify._TrialAction(
        1,
        1,
        1,
        "owner_dictated",
        state,
        False,
        True,
        datetime(2026, 10, 28, tzinfo=UTC),
        "UTC",
        True,
        True,
    )
    failures: set[str] = set()
    p2_verify._trial_history(action, events, lambda _check, code: failures.add(code))
    return failures


@pytest.mark.parametrize("state", ["approved", "scheduled", "failed", "cancelled"])
def test_trial_history_requires_an_outcome_except_for_the_current_executing_claim(
    state: str,
) -> None:
    events = [history_event("action_approved", 1), history_event("action_claimed", 2)]
    assert "missing_outcome" in history_codes(state, events)
    assert history_codes("executing", events) == set()


@pytest.mark.parametrize("state", ["approved", "scheduled"])
@pytest.mark.parametrize(
    "outcome", ["action_delivery_uncertain", "action_succeeded", "action_failed"]
)
def test_trial_history_only_rate_limits_can_rearm_an_approved_action(
    state: str, outcome: str
) -> None:
    events = [
        history_event("action_approved", 1),
        history_event("action_claimed", 2),
        history_event(outcome, 3),
    ]
    assert "rearmed_without_rate_limit" in history_codes(state, events)


def test_trial_history_rejects_a_stale_outcome_and_unclosed_latest_claim() -> None:
    events = [
        history_event("action_approved", 1),
        history_event("action_claimed", 2),
        history_event("action_rate_limited", 3),
        history_event("action_claimed", 4, "b"),
        history_event("action_succeeded", 5),
    ]
    assert {"outcome_not_latest_claim", "multiple_outcomes", "missing_outcome"} <= history_codes(
        "succeeded", events
    )


@pytest.mark.parametrize(
    "first",
    ["action_succeeded", "action_failed", "action_rate_limited", "action_delivery_uncertain"],
)
def test_trial_history_rejects_multiple_closures(first: str) -> None:
    events = [
        history_event("action_approved", 1),
        history_event("action_claimed", 2),
        history_event(first, 3),
        history_event("action_succeeded", 4),
    ]
    assert "multiple_outcomes" in history_codes("succeeded", events)


@pytest.mark.parametrize("damage", ["without_uncertain", "twice", "wrong_attempt"])
def test_trial_history_accepts_exactly_one_late_result_after_uncertain(damage: str) -> None:
    events = [history_event("action_approved", 1), history_event("action_claimed", 2)]
    if damage != "without_uncertain":
        events.append(history_event("action_delivery_uncertain", 3))
    events.append(history_event("action_late_result", 4, "b" if damage == "wrong_attempt" else "a"))
    if damage == "twice":
        events.append(history_event("action_late_result", 5))
    assert history_codes("succeeded", events)


def test_trial_history_reclaim_after_uncertain_counts_both_possible_deliveries() -> None:
    events = [
        history_event("action_approved", 1),
        history_event("action_claimed", 2),
        history_event("action_delivery_uncertain", 3),
        history_event("action_claimed", 4, "b"),
    ]
    assert "multiple_possible_deliveries" in history_codes("executing", events)


@pytest.mark.parametrize(
    "outcome", ["action_rate_limited", "action_failed", "action_expired", "action_late_result"]
)
def test_trial_history_definite_non_delivery_is_not_a_possible_delivery(outcome: str) -> None:
    events = [history_event("action_approved", 1), history_event("action_claimed", 2)]
    if outcome == "action_late_result":
        events.append(history_event("action_delivery_uncertain", 3))
        events.append(replace(history_event(outcome, 4), after="failed"))
    else:
        events.append(history_event(outcome, 3))
    events.append(history_event("action_claimed", 5, "b"))
    assert "multiple_possible_deliveries" not in history_codes("executing", events)


@pytest.mark.parametrize("damage", ["timestamp", "id"])
def test_trial_history_rejects_an_outcome_before_its_claim(damage: str) -> None:
    claim = history_event("action_claimed", 3)
    outcome = history_event("action_succeeded", 2)
    if damage == "timestamp":
        outcome = replace(outcome, at=outcome.at.replace(hour=1))
        claim = replace(claim, at=claim.at.replace(hour=2))
    # The history helper also guards callers supplying an unordered projection.
    events = [history_event("action_approved", 1), claim, outcome]
    assert "missing_matching_claim" in history_codes("succeeded", events)
