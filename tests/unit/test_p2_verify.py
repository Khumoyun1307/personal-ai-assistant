"""Content-free audit values and CLI failure boundaries."""

from contextlib import nullcontext

import psycopg
import pytest
from tools.acceptance import p2_verify
from tools.acceptance.p2_verify import details


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
