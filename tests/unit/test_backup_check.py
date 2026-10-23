"""Backup and off-site upload freshness (S02.6; ARCHITECTURE §15.4, §17).

The alert delivery and its dedupe are covered against Postgres in
tests/integration/test_system_tasks.py; these tests pin which marker is read
and when an alert is raised, with an in-memory alert sink.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from assistant.inbox.backup_check import UPLOAD_MARKER_NAME, check_backup
from assistant.inbox.notify import AlertKind
from tests.fakes.clock import FakeClock

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


@dataclass
class RecordingAlerts:
    sent: list[tuple[AlertKind, str, dict[str, int | str]]] = field(default_factory=list)

    async def notify_all_owners(
        self,
        kind: AlertKind,
        *,
        dedupe_key: str,
        cooldown: timedelta | None = None,
        **values: int | str,
    ) -> int:
        self.sent.append((kind, dedupe_key, values))
        return 1


def _markers(tmp_path: Path, *, dump_age_h: float | None, upload_age_h: float | None) -> Path:
    status = tmp_path / "last_success"
    if dump_age_h is not None:
        status.write_text((NOW - timedelta(hours=dump_age_h)).isoformat(), encoding="utf-8")
    if upload_age_h is not None:
        (tmp_path / UPLOAD_MARKER_NAME).write_text(
            (NOW - timedelta(hours=upload_age_h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            encoding="utf-8",
        )
    return status


async def test_without_a_remote_the_upload_marker_is_not_checked(tmp_path: Path) -> None:
    alerts = RecordingAlerts()
    status = _markers(tmp_path, dump_age_h=2, upload_age_h=None)

    report = await check_backup(FakeClock(NOW), alerts, status_path=status)

    assert report is not None
    assert (report.stale, report.upload_stale) == (False, None)
    assert alerts.sent == []


async def test_fresh_dump_and_fresh_upload_raise_nothing(tmp_path: Path) -> None:
    alerts = RecordingAlerts()
    status = _markers(tmp_path, dump_age_h=2, upload_age_h=2)

    report = await check_backup(FakeClock(NOW), alerts, status_path=status, upload_required=True)

    assert report is not None
    assert (report.stale, report.upload_stale) == (False, False)
    assert report.last_upload_at == NOW - timedelta(hours=2)
    assert alerts.sent == []


async def test_a_missing_upload_marker_alerts_while_a_remote_is_configured(
    tmp_path: Path,
) -> None:
    alerts = RecordingAlerts()
    status = _markers(tmp_path, dump_age_h=2, upload_age_h=None)

    report = await check_backup(FakeClock(NOW), alerts, status_path=status, upload_required=True)

    assert report is not None
    assert (report.stale, report.upload_stale, report.upload_alerted) == (False, True, True)
    assert alerts.sent == [
        (AlertKind.BACKUP_UPLOAD_STALE, "backup_upload_stale", {"hours": "unknown"})
    ]


async def test_a_stale_upload_alerts_with_its_age_in_hours(tmp_path: Path) -> None:
    alerts = RecordingAlerts()
    status = _markers(tmp_path, dump_age_h=2, upload_age_h=50)

    await check_backup(FakeClock(NOW), alerts, status_path=status, upload_required=True)

    assert alerts.sent == [(AlertKind.BACKUP_UPLOAD_STALE, "backup_upload_stale", {"hours": "50"})]


async def test_a_stale_dump_and_upload_raise_two_separate_alerts(tmp_path: Path) -> None:
    alerts = RecordingAlerts()
    status = _markers(tmp_path, dump_age_h=30, upload_age_h=30)

    report = await check_backup(FakeClock(NOW), alerts, status_path=status, upload_required=True)

    assert report is not None
    assert (report.alerted, report.upload_alerted) == (True, True)
    assert [kind for kind, _, _ in alerts.sent] == [
        AlertKind.BACKUP_STALE,
        AlertKind.BACKUP_UPLOAD_STALE,
    ]
