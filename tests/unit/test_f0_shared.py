"""F0 shared types keep the policy boundary free of adapters and the database."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from assistant.config import Settings
from assistant.conversations.models import Conversation
from assistant.conversations.window import window_closes_at, window_open
from assistant.core import reply_window
from assistant.core.delivery import ErrorClass, SendResult
from assistant.telegram.errors import ErrorClass as TelegramErrorClass
from assistant.telegram.gateway import SendResult as TelegramSendResult


def test_core_window_and_conversation_window_agree() -> None:
    now = datetime(2026, 10, 27, tzinfo=UTC)
    for anchor in (None, now, now - timedelta(hours=24)):
        conversation = Conversation(account_id=1, contact_id=1, last_contact_msg_at=anchor)
        assert reply_window.closes_at(anchor) == window_closes_at(conversation)
        assert reply_window.is_open(anchor, now) == window_open(conversation, now)


def test_delivery_moves_preserve_existing_imports_and_outcomes() -> None:
    assert ErrorClass is TelegramErrorClass
    assert SendResult is TelegramSendResult
    assert SendResult(ErrorClass.OK, message_id=0).ok
    assert SendResult(ErrorClass.TRANSIENT).delivery_uncertain
    assert SendResult(ErrorClass.AMBIGUOUS).delivery_uncertain
    assert not SendResult(ErrorClass.RATE_LIMITED).delivery_uncertain


def test_policy_imports_nothing_that_reaches_the_database() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import assistant.policy.context; import sys; assert 'sqlalchemy' not in sys.modules",
        ],
        check=True,
    )


def test_p2_settings_defaults_and_effort_blank(clean_env: None) -> None:
    settings = Settings(_env_file=None, database_url="postgresql+psycopg://u:p@db/test")
    assert settings.approval_max_age_hours == 12
    assert settings.draft_debounce_seconds == 20
    assert settings.triage_debounce_seconds == 20
    assert settings.monitor_debounce_seconds == 20
    assert settings.mark_uncertain_after_seconds == 120
    assert settings.connection_stale_alert_minutes == 30
    # D10 (2026-10-30): the draft writer moved to medium with owner_request.
    defaults = {
        "llm_draft_reply_effort": "medium",
        "llm_triage_effort": "low",
        "llm_evaluate_monitor_effort": "low",
    }
    for name, default in defaults.items():
        assert getattr(settings, name) == default
        blank = Settings(
            _env_file=None, database_url="postgresql+psycopg://u:p@db/test", **{name: ""}
        )
        assert getattr(blank, name) is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("approval_max_age_hours", 0),
        ("approval_max_age_hours", 25),
        ("draft_debounce_seconds", 4),
        ("draft_debounce_seconds", 301),
        ("triage_debounce_seconds", 4),
        ("triage_debounce_seconds", 301),
        ("monitor_debounce_seconds", 4),
        ("monitor_debounce_seconds", 301),
        ("mark_uncertain_after_seconds", 119),
        ("connection_stale_alert_minutes", 0),
        ("llm_draft_reply_effort", "invalid"),
        ("llm_triage_effort", "invalid"),
        ("llm_evaluate_monitor_effort", "invalid"),
    ],
)
def test_p2_settings_reject_invalid_values(clean_env: None, name: str, value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql+psycopg://u:p@db/test", **{name: value})
