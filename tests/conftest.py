"""Shared test configuration."""

from __future__ import annotations

import os

import pytest

#: Settings that must not leak from the developer's .env into unit tests.
_ENV_KEYS = (
    "APP_ENV",
    "LOG_LEVEL",
    "LOG_FORMAT",
    "DATABASE_URL",
    "TEST_DATABASE_URL",
    "BOT_TOKEN",
    "OWNER_TELEGRAM_IDS",
    "OPENAI_API_KEY",
    "LLM_DEFAULT_MODEL",
    "LLM_MONTHLY_BUDGET_USD",
    "HEARTBEAT_URL",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove assistant settings from the environment for one test."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """The integration-test database, or skip.

    Session-scoped so it resolves before the function-scoped `clean_env`
    removes the variable from the environment.
    """
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set")
    return url
