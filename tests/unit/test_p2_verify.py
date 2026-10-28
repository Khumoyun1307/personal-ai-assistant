"""Content-free audit values and CLI failure boundaries."""

from contextlib import nullcontext
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
