"""CLI exit codes (S00.1 acceptance)."""

from __future__ import annotations

import pytest

from assistant.__main__ import EXIT_FAILED, main

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


def test_run_refuses_to_start_without_a_bot_token(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@db:5432/assistant")
    monkeypatch.chdir("/")

    assert main(["run"]) == EXIT_FAILED
    err = capsys.readouterr().err
    assert "BOT_TOKEN" in err
    assert "pw" not in err


def test_run_rejects_an_unknown_role(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pw@db:5432/assistant")
    monkeypatch.setenv("BOT_TOKEN", "123456:synthetic-test-token")
    monkeypatch.chdir("/")

    assert main(["run", "--roles", "poller,nonsense"]) == EXIT_FAILED
    assert "nonsense" in capsys.readouterr().err


def test_migrate_reports_invalid_configuration_by_name(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "mysql://user:pw@db:3306/assistant")
    monkeypatch.chdir("/")
    assert main(["migrate"]) == EXIT_FAILED
    assert "DATABASE_URL" in capsys.readouterr().err


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["nope"])
