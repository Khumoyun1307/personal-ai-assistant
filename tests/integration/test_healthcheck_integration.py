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


def _reset(url: str, *, with_message: bool) -> None:
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE app.accounts CASCADE"))
            if not with_message:
                return
            account = conn.execute(
                text("INSERT INTO app.accounts (telegram_user_id) VALUES (4545) RETURNING id")
            ).scalar_one()
            contact = conn.execute(
                text(
                    "INSERT INTO app.contacts (account_id, telegram_user_id) "
                    "VALUES (:a, 77) RETURNING id"
                ),
                {"a": account},
            ).scalar_one()
            conversation = conn.execute(
                text(
                    "INSERT INTO app.conversations (account_id, business_chat_id, contact_id) "
                    "VALUES (:a, 77, :c) RETURNING id"
                ),
                {"a": account, "c": contact},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO app.messages (account_id, conversation_id, direction, sent_at, "
                    "content_type, text, search_key) VALUES (:a, :c, 'contact', now(), 'text', "
                    "'Synthetic', 'synthetic')"
                ),
                {"a": account, "c": conversation},
            )
    finally:
        engine.dispose()


def test_a_fresh_install_without_messages_does_not_warn(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_url)
    monkeypatch.chdir("/")
    _reset(migrated_url, with_message=False)
    _set_stored_version(migrated_url, 1)
    assert main(["healthcheck"]) == EXIT_OK
    assert "WARNING" not in capsys.readouterr().out


def test_healthcheck_warns_until_the_search_keys_are_reindexed(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    migrated_url: str,
) -> None:
    monkeypatch.setenv("DATABASE_URL", migrated_url)
    monkeypatch.chdir("/")
    _reset(migrated_url, with_message=True)
    _set_stored_version(migrated_url, 1)

    assert main(["healthcheck"]) == EXIT_OK  # a warning, not a failure
    assert "WARNING: search keys were built with normalizer v1" in capsys.readouterr().out

    assert main(["reindex-search"]) == EXIT_OK
    assert f"-> v{NORMALIZER_VERSION}" in capsys.readouterr().out
    assert main(["reindex-search"]) == EXIT_OK
    assert "nothing to do" in capsys.readouterr().out

    assert main(["healthcheck"]) == EXIT_OK
    assert "WARNING" not in capsys.readouterr().out
    _reset(migrated_url, with_message=False)


def test_normalizer_warning_only_for_an_older_stored_version() -> None:
    assert normalizer_warning(None) is None
    assert normalizer_warning(NORMALIZER_VERSION) is None
    assert normalizer_warning(NORMALIZER_VERSION - 1) is not None
