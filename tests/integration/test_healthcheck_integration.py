"""Healthcheck against a real PostgreSQL (AGENTS §11)."""

from __future__ import annotations

import pytest

from assistant.__main__ import EXIT_OK, main

pytestmark = pytest.mark.integration


def test_healthcheck_succeeds_against_the_test_database(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    test_database_url: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    monkeypatch.chdir("/")
    assert main(["healthcheck"]) == EXIT_OK
    assert "OK" in capsys.readouterr().out
