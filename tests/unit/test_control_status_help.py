"""Truthful deterministic control texts (stabilization Q2), without I/O."""

from __future__ import annotations

import pytest

from assistant.inbox.notifications import NotificationCounts
from assistant.observability.status import (
    ActionsStatus,
    AutonomyStatus,
    BudgetStatus,
    ConnectionStatus,
    ConsentStatus,
    CoverageStatus,
    InboxStatus,
    StatusReport,
)
from assistant.telegram.control.handlers import HELP_TEXT, render_status
from assistant.telegram.render import render_control_message


def test_help_lists_pause_resume_and_explains_the_pause_scope() -> None:
    [text] = render_control_message(HELP_TEXT)

    assert "/pause — stop automatic replies" in text
    assert "/resume — allow automatic replies again" in text
    assert "Send <b>stop</b> at any time to pause automatic replies." in text
    assert "Alerts, digest and replies you approve keep working." in text
    assert "disconnect me in Telegram's Chat Automation settings" in text
    assert "To stop everything" in text
    assert "pause everything" not in text


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        (
            NotificationCounts(queued=2, deferred=3, batched=4, uncertain_24h=5),
            "Alerts: 2 queued, 3 held for quiet hours or cap, "
            "4 waiting for the digest, 5 uncertain (24 h)",
        ),
        (
            NotificationCounts(),
            "Alerts: 0 queued, 0 held for quiet hours or cap, "
            "0 waiting for the digest, 0 uncertain (24 h)",
        ),
    ],
)
def test_status_renders_the_shared_content_free_alert_counts(
    counts: NotificationCounts, expected: str
) -> None:
    report = StatusReport(
        account_status="active",
        timezone="UTC",
        connection=ConnectionStatus(present=False),
        coverage=CoverageStatus(
            conversations=0,
            by_mode={},
            coverage_started_at=None,
            unresolved_possible=0,
            unresolved_definite=0,
            delayed=0,
            older_unresolved_gaps=0,
            latest_gap=None,
            paused_by_ingest_error=0,
        ),
        consent=ConsentStatus(control_chat=False, managed_chats=False, policy_version=None),
        autonomy=AutonomyStatus(paused=True, conversations_paused={}),
        inbox=InboxStatus(pending=0, failed=0, poller_heartbeat_age_seconds=None),
        budget=BudgetStatus(month_to_date_usd="0", monthly_limit_usd=None),
        actions=ActionsStatus(awaiting_approval=0, sending=0, uncertain_7d=0, failed_24h=0),
        notifications=counts,
    )

    [text] = render_control_message(render_status(report, visibility="Synthetic visibility"))

    assert expected in text
    assert report.notifications_line == expected
