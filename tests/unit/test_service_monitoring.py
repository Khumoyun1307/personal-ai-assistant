"""Unit tests for content-free service-completion monitoring (I26, ARCHITECTURE §17)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from assistant.core.control_texts import CATALOGUE, text
from assistant.inbox.notify import ALERT_TEXT, AlertKind
from assistant.observability.service_monitoring import (
    ServiceIndicators,
    ServiceMonitoringThresholds,
    check_backup_markers,
    evaluate_degradation,
    format_service_status,
)
from assistant.observability.status import ServiceStatusReport

LOCALES = ("uz-Latn", "uz-Cyrl", "ru", "en")


def test_thresholds_defaults() -> None:
    t = ServiceMonitoringThresholds()
    assert t.owner_request_age_seconds == 300
    assert t.unanswered_turns_threshold == 1
    assert t.unanswered_turn_age_seconds == 300
    assert t.llm_job_age_seconds == 600
    assert t.review_window_seconds == 3600
    assert t.failed_reviews_threshold == 5
    assert t.action_unresolved_seconds == 300
    assert t.retention_sweep_max_age_hours == 4
    assert t.backup_max_age_hours == 26
    assert t.backup_upload_max_age_hours == 26
    assert t.consecutive_checks == 3


def test_thresholds_from_settings() -> None:
    class DummySettings:
        service_monitoring_owner_request_age_seconds = 120
        service_monitoring_unanswered_turns_threshold = 2
        service_monitoring_unanswered_turn_age_seconds = 400
        service_monitoring_llm_job_age_seconds = 800
        service_monitoring_review_window_seconds = 7200
        service_monitoring_failed_reviews_threshold = 8
        service_monitoring_action_unresolved_seconds = 500
        service_monitoring_retention_sweep_max_age_hours = 6
        service_monitoring_backup_max_age_hours = 30
        service_monitoring_backup_upload_max_age_hours = 36
        service_monitoring_consecutive_checks = 5

    t = ServiceMonitoringThresholds.from_settings(DummySettings())
    assert t.owner_request_age_seconds == 120
    assert t.unanswered_turns_threshold == 2
    assert t.unanswered_turn_age_seconds == 400
    assert t.llm_job_age_seconds == 800
    assert t.review_window_seconds == 7200
    assert t.failed_reviews_threshold == 8
    assert t.action_unresolved_seconds == 500
    assert t.retention_sweep_max_age_hours == 6
    assert t.backup_max_age_hours == 30
    assert t.backup_upload_max_age_hours == 36
    assert t.consecutive_checks == 5


def test_evaluate_degradation_healthy() -> None:
    indicators = ServiceIndicators(
        last_retention_sweep_age_seconds=1800,  # 30 min (threshold 4 h)
        last_backup_age_seconds=3600,  # 1 h (threshold 26 h)
        journal_status="acknowledged",
    )
    t = ServiceMonitoringThresholds()
    degraded, reasons = evaluate_degradation(indicators, t)
    assert not degraded
    assert not reasons


@pytest.mark.parametrize(
    ("override_kwargs", "expected_reason"),
    [
        (
            {"oldest_pending_owner_request_age_seconds": 350},
            "owner_request_queued",
        ),
        (
            {"unanswered_owner_turns": 2, "oldest_unanswered_owner_turn_age_seconds": 350},
            "unanswered_owner_turns",
        ),
        (
            {"oldest_queued_llm_job_age_seconds": 650},
            "llm_jobs_queued",
        ),
        (
            {"failed_llm_jobs": {"triage": 5}},
            "llm_jobs_failed",
        ),
        (
            {"failed_reviews_window": 6},
            "failed_reviews",
        ),
        (
            {"actions_executing": 1, "oldest_executing_action_age_seconds": 350},
            "actions_executing",
        ),
        (
            {"actions_delivery_uncertain": 1, "oldest_delivery_uncertain_action_age_seconds": 350},
            "actions_delivery_uncertain",
        ),
        (
            {"unresolved_card_presentations": 1, "oldest_unresolved_presentation_age_seconds": 350},
            "card_presentations_unresolved",
        ),
        (
            {"last_retention_sweep_age_seconds": 5 * 3600},
            "retention_sweep_stale",
        ),
        (
            {"last_backup_age_seconds": 30 * 3600},
            "backup_stale",
        ),
        (
            {"last_backup_upload_age_seconds": 30 * 3600},
            "backup_upload_stale",
        ),
        (
            {"journal_status": "error"},
            "privacy_journal_error",
        ),
    ],
)
def test_evaluate_degradation_indicators(
    override_kwargs: dict[str, object], expected_reason: str
) -> None:
    base = {
        "last_retention_sweep_age_seconds": 60,
        "last_backup_age_seconds": 60,
        "journal_status": "acknowledged",
    }
    base.update(override_kwargs)
    indicators = ServiceIndicators(**base)  # type: ignore[arg-type]
    t = ServiceMonitoringThresholds()
    degraded, reasons = evaluate_degradation(
        indicators,
        t,
        backup_required=True,
        backup_upload_required="last_backup_upload_age_seconds" in override_kwargs,
    )
    assert degraded
    assert expected_reason in reasons


def test_format_service_status_content_free() -> None:
    indicators = ServiceIndicators(
        oldest_pending_owner_request_age_seconds=120,
        unanswered_owner_turns=1,
        oldest_unanswered_owner_turn_age_seconds=650,
        queued_llm_jobs={"triage": 2},
        oldest_queued_llm_job_age_seconds=300,
        failed_llm_jobs={"drafting": 1},
        oldest_failed_llm_job_age_seconds=600,
        failed_reviews_window=0,
        actions_executing=0,
        oldest_executing_action_age_seconds=None,
        actions_delivery_uncertain=0,
        oldest_delivery_uncertain_action_age_seconds=None,
        unresolved_card_presentations=0,
        oldest_unresolved_presentation_age_seconds=None,
        last_retention_sweep_age_seconds=1200,
        last_backup_age_seconds=3600,
        last_backup_upload_age_seconds=3600,
        journal_status="acknowledged",
        degraded=False,
    )
    formatted = format_service_status(indicators)
    assert "Service completion indicators:" in formatted
    assert "Unanswered owner turns: 1 (oldest 650s)" in formatted
    assert "Oldest pending owner request: 120s" in formatted
    assert "Queued LLM jobs: 2 (oldest 300s)" in formatted
    assert "Failed LLM jobs: 1 (oldest 600s)" in formatted
    assert "Failed reviews (window): 0" in formatted
    assert "Actions executing: 0" in formatted
    assert "Unresolved card presentations: 0" in formatted
    assert "Last retention sweep: 1200s ago" in formatted
    assert "Last backup: 3600s ago" in formatted
    assert "Privacy journal status: acknowledged" in formatted
    assert "Status: normal" in formatted
    # Content-free guarantee
    assert "@" not in formatted


def test_service_status_report_line() -> None:
    healthy_report = ServiceStatusReport(degraded=False)
    assert healthy_report.line == "Service: normal"

    degraded_report = ServiceStatusReport(
        degraded=True,
        degradation_reasons=("retention_sweep_stale", "backup_stale"),
    )
    assert degraded_report.line == "⚠️ Service: degraded (2 issues)"


def test_check_backup_markers(tmp_path: Path) -> None:
    now = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
    (tmp_path / "last_success").write_text("20261111T100000Z\n", encoding="utf-8")
    (tmp_path / "last_upload_success").write_text("20261111T090000Z\n", encoding="utf-8")
    (tmp_path / "journal_status").write_text("acknowledged\n", encoding="utf-8")

    backup_age, upload_age, status, _ = check_backup_markers(tmp_path, now)
    assert backup_age == 7200  # 2 hours
    assert upload_age == 10800  # 3 hours
    assert status == "acknowledged"


def test_check_backup_markers_missing(tmp_path: Path) -> None:
    now = datetime(2026, 11, 11, 12, 0, 0, tzinfo=UTC)
    backup_age, upload_age, status, _ = check_backup_markers(tmp_path / "nonexistent", now)
    assert backup_age is None
    assert upload_age is None
    assert status is None


@pytest.mark.parametrize("locale", LOCALES)
def test_operational_texts_for_service_monitoring(locale: str) -> None:
    assert text("alert_service_degraded", locale)
    assert text("alert_service_recovered", locale)


def test_alert_text_parity_with_catalogue() -> None:
    assert CATALOGUE["alert_service_degraded"]["en"] == ALERT_TEXT[AlertKind.SERVICE_DEGRADED]
    assert CATALOGUE["alert_service_recovered"]["en"] == ALERT_TEXT[AlertKind.SERVICE_RECOVERED]
