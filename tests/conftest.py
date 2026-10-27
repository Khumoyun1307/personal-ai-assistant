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
    "LLM_OWNER_REQUEST_EFFORT",
    "LLM_SUMMARIZE_EFFORT",
    "SUMMARY_REFRESH_DEBOUNCE_MINUTES",
    "SUMMARY_REFRESH_MAX_WAIT_HOURS",
    "APPROVAL_MAX_AGE_HOURS",
    "DRAFT_DEBOUNCE_SECONDS",
    "TRIAGE_DEBOUNCE_SECONDS",
    "MONITOR_DEBOUNCE_SECONDS",
    "MARK_UNCERTAIN_AFTER_SECONDS",
    "CONNECTION_STALE_ALERT_MINUTES",
    "LLM_DRAFT_REPLY_EFFORT",
    "LLM_TRIAGE_EFFORT",
    "LLM_EVALUATE_MONITOR_EFFORT",
    "LLM_ROUTE_OWNER_REQUEST_MODEL",
    "LLM_ROUTE_TRIAGE_MODEL",
    "LLM_ROUTE_DRAFT_REPLY_MODEL",
    "LLM_ROUTE_SUMMARIZE_MODEL",
    "LLM_ROUTE_EXTRACT_MEMORY_MODEL",
    "LLM_ROUTE_EVALUATE_MONITOR_MODEL",
    "LLM_ROUTE_INTERPRET_INSTRUCTION_MODEL",
    "FEATURE_FLAGS",
    "HEARTBEAT_URL",
    "PRIVACY_POLICY_URL",
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
