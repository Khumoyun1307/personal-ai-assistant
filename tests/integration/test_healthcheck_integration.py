"""Healthcheck against a real PostgreSQL (AGENTS §11)."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from assistant.__main__ import EXIT_OK, main, normalizer_warning
from assistant.memory.search import NORMALIZER_VERSION

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


def _set_stored_version(url: str, version: int) -> None:
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO app.system_state (key, value) VALUES ('normalizer_version', "
                    "jsonb_build_object('version', CAST(:v AS int))) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
                ),
                {"v": version},
            )
    finally:
        engine.dispose()


def test_healthcheck_warns_until_the_search_keys_are_reindexed(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_url)
    monkeypatch.chdir("/")
    _set_stored_version(migrated_url, 1)

    assert main(["healthcheck"]) == EXIT_OK  # a warning, not a failure
    assert "WARNING: search keys were built with normalizer v1" in capsys.readouterr().out

    assert main(["reindex-search"]) == EXIT_OK
    assert f"-> v{NORMALIZER_VERSION}" in capsys.readouterr().out
    assert main(["reindex-search"]) == EXIT_OK
    assert "nothing to do" in capsys.readouterr().out

    assert main(["healthcheck"]) == EXIT_OK
    assert "WARNING" not in capsys.readouterr().out


def test_normalizer_warning_only_for_an_older_stored_version() -> None:
    assert normalizer_warning(None) is None
    assert normalizer_warning(NORMALIZER_VERSION) is None
    assert normalizer_warning(NORMALIZER_VERSION - 1) is not None
