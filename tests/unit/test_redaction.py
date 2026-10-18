"""Logs never carry content or secrets (AGENTS §9, ARCHITECTURE §14.4).

Named regression suite for the logging-hygiene invariant: never weaken these.
"""

from __future__ import annotations

import pytest

from assistant.observability.redaction import (
    REDACTED,
    drop_sensitive_keys,
    redact_exception,
    scrub_sensitive_values,
    scrub_text,
)

BOT_TOKEN = "8123456789:AAH1bQ7xKqY-ZZ_pLm3nOpQrStUvWxYz012"
OPENAI_KEY = "sk-proj-AbCdEfGhIjKlMnOpQrSt"


class FakeDiag:
    constraint_name = "messages_account_id_fkey"


class FakeDbError(Exception):
    """Stands in for a psycopg integrity error, which carries the failing row."""

    sqlstate = "23503"
    diag = FakeDiag()


def _process(event: dict[str, object]) -> dict[str, object]:
    event = redact_exception(None, "info", event)
    event = drop_sensitive_keys(None, "info", event)
    return scrub_sensitive_values(None, "info", event)


@pytest.mark.parametrize(
    "key",
    ["text", "caption", "first_name", "username", "phone_number", "bot_token", "api_key"],
)
def test_content_and_secret_keys_are_dropped(key: str) -> None:
    event = _process({"event": "message_stored", key: "Salom, ertaga uchrashamizmi?"})
    assert event[key] == REDACTED


def test_identifiers_survive() -> None:
    event = _process({"event": "message_stored", "conversation_id": 42, "update_id": 991})
    assert event["conversation_id"] == 42
    assert event["update_id"] == 991


@pytest.mark.parametrize(
    "raw",
    [
        f"calling https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        f"token={BOT_TOKEN}",
        f"Authorization: Bearer {OPENAI_KEY}",
        "postgresql+psycopg://assistant:hunter2@db:5432/assistant",
        "contact +998 90 123 45 67 called",
    ],
)
def test_secret_shapes_are_scrubbed_anywhere_in_a_string(raw: str) -> None:
    scrubbed = scrub_text(raw)
    assert REDACTED in scrubbed
    for secret in (BOT_TOKEN, OPENAI_KEY, "hunter2", "90 123 45 67"):
        assert secret not in scrubbed


@pytest.mark.parametrize(
    "raw",
    ["2026-10-17", "2026-10-17T03:54:12Z", "conversation 1234567890", "update_id=441029"],
)
def test_identifiers_and_dates_are_not_over_redacted(raw: str) -> None:
    """Over-redaction would make the logs useless; dates and ids must survive."""
    assert scrub_text(raw) == raw


def test_nested_structures_are_scrubbed() -> None:
    event = _process(
        {
            "event": "telegram_call",
            "request": {"url": f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"},
            "payload": {"text": "secret plan", "chat_id": 7},
        }
    )
    request = event["request"]
    payload = event["payload"]
    assert isinstance(request, dict)
    assert isinstance(payload, dict)
    assert BOT_TOKEN not in str(request)
    assert payload["text"] == REDACTED
    assert payload["chat_id"] == 7


def test_exception_is_reduced_to_typed_fields() -> None:
    try:
        raise FakeDbError("Failing row contains (1, 'Salom, qalaysan?')")
    except FakeDbError as error:
        event = _process({"event": "db_write_failed", "exc_info": error})

    assert event["exc_type"] == "FakeDbError"
    assert event["sqlstate"] == "23503"
    assert event["constraint"] == "messages_account_id_fkey"
    assert "exc_info" not in event
    assert "Salom" not in str(event)


def test_exception_without_diagnostics_still_reports_its_type() -> None:
    try:
        raise ValueError(f"bad token {BOT_TOKEN}")
    except ValueError as error:
        event = _process({"event": "failed", "exc_info": error})

    assert event["exc_type"] == "ValueError"
    assert BOT_TOKEN not in str(event)
