"""Scratch restore gates and content-free notice projections."""

from contextlib import nullcontext
from typing import Any

import pytest
from tools.acceptance import restore_verify


class Database:
    def __init__(self, *, actions: int = 0, jobs: int = 0, notice: Any = None) -> None:
        self.actions = actions
        self.jobs = jobs
        self.notice = {"actions": {}} if notice is None else notice

    def execute(self, sql: str, params: Any = None) -> Any:
        assert "text" not in sql
        assert "caption" not in sql
        if "GROUP BY state_reason" in sql:
            return [("restored_from_backup", 2), ("SYNTHETIC-PRIVATE-CANARY", 1), (None, 1)]
        if "app.actions" in sql:
            return [("approved", self.actions)]
        if "procrastinate_jobs" in sql:
            return [("todo", self.jobs)]
        assert "restore_notice_pending" in str(params)
        return self

    def fetchone(self) -> Any:
        return None if self.notice is False else (self.notice,)


@pytest.mark.parametrize(
    "state", ["proposed", "awaiting_approval", "revising", "approved", "scheduled", "executing"]
)
def test_every_sendable_state_fails_the_restore_gate(state: str) -> None:
    class PendingDatabase(Database):
        def execute(self, sql: str, params: Any = None) -> Any:
            if "GROUP BY state" in sql and "state_reason" not in sql:
                return [(state, 1)]
            return super().execute(sql, params)

    assert restore_verify.verify(PendingDatabase()) == 1


@pytest.mark.parametrize(("actions", "jobs"), [(0, 0), (1, 0), (0, 1)])
def test_restore_gates_pending_actions_and_jobs(
    actions: int, jobs: int, capsys: pytest.CaptureFixture[str]
) -> None:
    assert restore_verify.verify(Database(actions=actions, jobs=jobs)) == int(actions + jobs > 0)
    output = capsys.readouterr().out
    assert f"pending_actions={actions}" in output
    assert f"pending_jobs={jobs}" in output
    assert "state_reason=restored_from_backup count=2" in output
    assert "state_reason=invalid count=1" in output
    assert "SYNTHETIC-PRIVATE-CANARY" not in output


def test_restore_notice_prints_only_numeric_ids_and_derived_counts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    notice = {
        "actions": {
            "17": {
                "cancelled": [{"id": 11, "created_at": "SYNTHETIC-PRIVATE-CANARY"}],
                "uncertain": [{"id": 12, "extra": "SYNTHETIC-PRIVATE-CANARY"}],
            }
        },
        "extra": "SYNTHETIC-PRIVATE-CANARY",
    }
    assert restore_verify.verify(Database(notice=notice)) == 0
    output = capsys.readouterr().out
    assert "restore_notice_pending=yes" in output
    assert "account=17 cancelled=1 uncertain=1" in output
    assert "account=17 cancelled_action=11" in output
    assert "account=17 uncertain_action=12" in output
    assert "SYNTHETIC-PRIVATE-CANARY" not in output


@pytest.mark.parametrize(
    "notice",
    [
        False,
        [],
        {"actions": []},
        {"actions": {"SYNTHETIC-PRIVATE-CANARY": {}}},
        {"actions": {"1": {"cancelled": [{"id": True}], "uncertain": []}}},
        {"actions": {"1": {"cancelled": [{"id": "SYNTHETIC-PRIVATE-CANARY"}], "uncertain": []}}},
    ],
)
def test_missing_or_malformed_notice_fails_without_printing_payload(
    notice: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert restore_verify.verify(Database(notice=notice)) == 1
    assert "SYNTHETIC-PRIVATE-CANARY" not in capsys.readouterr().out


def test_restore_cli_reports_database_error_without_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic/restore_test")
    monkeypatch.setattr(restore_verify, "open_database", lambda _url: nullcontext(Database()))

    def fail(_conn: Any) -> int:
        raise RuntimeError("SYNTHETIC-PRIVATE-CANARY")

    monkeypatch.setattr(restore_verify, "verify", fail)
    assert restore_verify.main() == 1
    assert capsys.readouterr().err == "restore_verification_failed type=RuntimeError\n"
