"""CLI exit codes (S00.1 acceptance)."""

from __future__ import annotations

import pytest

from assistant.__main__ import EXIT_FAILED, EXIT_NOT_IMPLEMENTED, main

VALID_DB_URL = "postgresql+psycopg://user:pw@db:5432/assistant"


def test_healthcheck_fails_on_invalid_configuration(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql://user:pw@db:3306/assistant")
    monkeypatch.chdir("/")  # keep a developer's .env out of the test
    assert main(["healthcheck"]) == EXIT_FAILED
    err = capsys.readouterr().err
    assert "DATABASE_URL" in err
    assert "pw" not in err.replace("must start with", "")


def test_healthcheck_fails_when_database_is_unreachable(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@127.0.0.1:1/assistant")
    monkeypatch.chdir("/")
    assert main(["healthcheck"]) == EXIT_FAILED
    err = capsys.readouterr().err
    assert "DATABASE_URL" in err
    assert "pw" not in err


def test_unimplemented_commands_report_clearly(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for command in ("migrate", "run"):
        assert main([command]) == EXIT_NOT_IMPLEMENTED
        assert "not implemented" in capsys.readouterr().err


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["nope"])
