"""Service-completion monitoring (I26, ARCHITECTURE §17, finding R4-09).

Computes content-free service-completion indicators across accounts and queues:
- Oldest pending owner_request job age and unanswered owner turns past threshold;
- Queued / failed LLM-backed job ages (triage, summary, monitors, drafting);
- Failed or degraded triage/summary reviews in a window;
- Actions in executing / delivery_uncertain and card presentations unresolved;
- Last successful retention sweep age;
- Last successful backup and backup-upload age, and privacy-journal shipping status;
- Sustained degradation detection across N consecutive checks with alerts and recovery.

All data is strictly content-free (counts, ages, and enums/statuses only; never contact names,
chat text, or tokens).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.core.clock import Clock
from assistant.db.models import (
    KEY_LAST_RETENTION_SWEEP_OK,
    KEY_SERVICE_MONITORING,
    SystemState,
)
from assistant.db.uow import transaction
from assistant.inbox.backup_check import UPLOAD_MARKER_NAME, read_last_success
from assistant.inbox.notify import AlertKind, OwnerAlerts
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

LLM_TASKS: dict[str, tuple[str, ...]] = {
    "triage": ("inbox.triage",),
    "summary": ("memory.refresh_summary",),
    "monitors": ("monitors.evaluate",),
    "drafting": ("replies.draft", "replies.revise"),
}


@dataclass(frozen=True, slots=True)
class ServiceMonitoringThresholds:
    """Safe defaults for service-completion indicators (ARCHITECTURE §17, I26)."""

    owner_request_age_seconds: int = 300
    unanswered_turns_threshold: int = 1
    unanswered_turn_age_seconds: int = 300
    llm_job_age_seconds: int = 600
    review_window_seconds: int = 3600
    failed_reviews_threshold: int = 5
    action_unresolved_seconds: int = 300
    retention_sweep_max_age_hours: int = 4
    backup_max_age_hours: int = 26
    backup_upload_max_age_hours: int = 26
    consecutive_checks: int = 3

    @classmethod
    def from_settings(cls, settings: Any) -> ServiceMonitoringThresholds:
        return cls(
            owner_request_age_seconds=getattr(
                settings, "service_monitoring_owner_request_age_seconds", 300
            ),
            unanswered_turns_threshold=getattr(
                settings, "service_monitoring_unanswered_turns_threshold", 1
            ),
            unanswered_turn_age_seconds=getattr(
                settings, "service_monitoring_unanswered_turn_age_seconds", 300
            ),
            llm_job_age_seconds=getattr(settings, "service_monitoring_llm_job_age_seconds", 600),
            review_window_seconds=getattr(
                settings, "service_monitoring_review_window_seconds", 3600
            ),
            failed_reviews_threshold=getattr(
                settings, "service_monitoring_failed_reviews_threshold", 5
            ),
            action_unresolved_seconds=getattr(
                settings, "service_monitoring_action_unresolved_seconds", 300
            ),
            retention_sweep_max_age_hours=getattr(
                settings, "service_monitoring_retention_sweep_max_age_hours", 4
            ),
            backup_max_age_hours=getattr(settings, "service_monitoring_backup_max_age_hours", 26),
            backup_upload_max_age_hours=getattr(
                settings, "service_monitoring_backup_upload_max_age_hours", 26
            ),
            consecutive_checks=getattr(settings, "service_monitoring_consecutive_checks", 3),
        )


@dataclass(frozen=True, slots=True)
class ServiceIndicators:
    """Content-free service completion metrics.

    Never contains message text, contact names or contact identifiers (§6, §9).
    """

    # 1. Owner requests & turns
    oldest_pending_owner_request_age_seconds: int | None = None
    unanswered_owner_turns: int = 0
    oldest_unanswered_owner_turn_age_seconds: int | None = None

    # 2. LLM-backed jobs (triage, summary, monitors, drafting)
    queued_llm_jobs: dict[str, int] = field(default_factory=dict)
    oldest_queued_llm_job_age_seconds: int | None = None
    failed_llm_jobs: dict[str, int] = field(default_factory=dict)
    oldest_failed_llm_job_age_seconds: int | None = None

    # 3. Reviews (triage & summary)
    failed_reviews_window: int = 0

    # 4. Actions & presentations
    actions_executing: int = 0
    oldest_executing_action_age_seconds: int | None = None
    actions_delivery_uncertain: int = 0
    oldest_delivery_uncertain_action_age_seconds: int | None = None
    unresolved_card_presentations: int = 0
    oldest_unresolved_presentation_age_seconds: int | None = None

    # 5. Retention sweep
    last_retention_sweep_age_seconds: int | None = None

    # 6. Backups & privacy journal
    last_backup_age_seconds: int | None = None
    last_backup_upload_age_seconds: int | None = None
    journal_status: str | None = None
    last_journal_upload_age_seconds: int | None = None

    # Degradation evaluation
    degraded: bool = False
    degradation_reasons: tuple[str, ...] = ()

    # Extension points for future stabilization increments:
    # - I14 (request-attempt indicators)
    # - I25 (structural incident trace indicators)
    request_attempt_indicators: Any = None
    incident_trace_indicators: Any = None


@dataclass(frozen=True, slots=True)
class ServiceMonitoringOutcome:
    indicators: ServiceIndicators
    alerted: bool
    recovered: bool
    consecutive_checks: int


async def get_oldest_pending_owner_request_age(
    session: AsyncSession, now: datetime, *, account_id: int | None = None
) -> int | None:
    """Age of the oldest queued or running agent.owner_request job."""
    lock_val = f"acct:{account_id}:control" if account_id is not None else None
    query = text("""
        SELECT EXTRACT(EPOCH FROM (:now - min(COALESCE(t.created_at, e.at, :now))))
        FROM procrastinate.procrastinate_jobs AS j
        LEFT JOIN procrastinate.procrastinate_events AS e
          ON e.job_id = j.id AND e.type::text = 'deferred'
        LEFT JOIN app.control_turns AS t
          ON CAST(j.args ->> 'turn_id' AS bigint) = t.id
        WHERE j.task_name = 'agent.owner_request'
          AND j.status::text IN ('todo', 'doing')
          AND (
              CAST(:account_id AS bigint) IS NULL
              OR t.account_id = :account_id
              OR (t.account_id IS NULL AND j.lock = CAST(:lock_val AS text))
          )
    """)
    res = (
        await session.execute(query, {"now": now, "account_id": account_id, "lock_val": lock_val})
    ).scalar()
    return None if res is None else max(int(res), 0)


async def get_unanswered_owner_turns(
    session: AsyncSession,
    now: datetime,
    *,
    account_id: int | None = None,
    threshold_seconds: int = 300,
) -> tuple[int, int | None]:
    """Count and oldest age of owner turns without a completed response past threshold."""
    threshold_time = now - timedelta(seconds=threshold_seconds)
    query = text("""
        SELECT count(t.id) AS count,
               EXTRACT(EPOCH FROM (:now - min(t.created_at))) AS oldest_age
        FROM app.control_turns AS t
        WHERE (CAST(:account_id AS bigint) IS NULL OR t.account_id = :account_id)
          AND t.role = 'owner'
          AND t.refs -> 'revision_action_id' IS NULL
          AND t.created_at <= :threshold_time
          AND NOT EXISTS (
              SELECT 1 FROM app.control_turns AS a
              WHERE a.account_id = t.account_id
                AND a.role = 'assistant'
                AND a.id > t.id
          )
    """)
    row = (
        await session.execute(
            query,
            {"now": now, "threshold_time": threshold_time, "account_id": account_id},
        )
    ).one()
    count = int(row[0] or 0)
    age = None if row[1] is None or count == 0 else max(int(row[1]), 0)
    return count, age


async def get_llm_job_indicators(
    session: AsyncSession, now: datetime, *, failed_window_seconds: int = 3600
) -> tuple[dict[str, int], int | None, dict[str, int], int | None]:
    """Queued and failed LLM-backed jobs (triage, summary, monitors, drafting).

    Queue age counts from the later of the defer time and the scheduled time, so a job
    deliberately scheduled for later is not "late". Procrastinate keeps failed jobs forever
    (`delete_old_jobs` skips them), so failures count only inside `failed_window_seconds`;
    otherwise one old failure would keep the alert raised for good.
    """
    task_to_category = {task: cat for cat, task_list in LLM_TASKS.items() for task in task_list}

    queued_query = text("""
        SELECT j.task_name,
               count(j.id) AS count,
               EXTRACT(
                   EPOCH FROM (:now - min(COALESCE(GREATEST(e.at, j.scheduled_at), :now)))
               ) AS oldest_age
        FROM procrastinate.procrastinate_jobs AS j
        LEFT JOIN procrastinate.procrastinate_events AS e
          ON e.job_id = j.id AND e.type::text = 'deferred'
        WHERE j.task_name IN (
            'inbox.triage', 'memory.refresh_summary', 'monitors.evaluate',
            'replies.draft', 'replies.revise'
        )
          AND j.status::text IN ('todo', 'doing')
        GROUP BY j.task_name
    """)
    queued_rows = (await session.execute(queued_query, {"now": now})).all()

    queued_counts: dict[str, int] = dict.fromkeys(LLM_TASKS, 0)
    oldest_queued_age: int | None = None
    for task_name, count, age in queued_rows:
        cat = task_to_category.get(str(task_name))
        if cat:
            queued_counts[cat] += int(count or 0)
        if age is not None:
            age_int = max(int(age), 0)
            oldest_queued_age = (
                age_int if oldest_queued_age is None else max(oldest_queued_age, age_int)
            )

    failed_query = text("""
        SELECT j.task_name,
               count(j.id) AS count,
               EXTRACT(EPOCH FROM (:now - min(e.at))) AS oldest_age
        FROM procrastinate.procrastinate_jobs AS j
        JOIN procrastinate.procrastinate_events AS e
          ON e.job_id = j.id AND e.type::text = 'failed'
        WHERE j.task_name IN (
            'inbox.triage', 'memory.refresh_summary', 'monitors.evaluate',
            'replies.draft', 'replies.revise'
        )
          AND j.status::text = 'failed'
          AND e.at >= :window_start
        GROUP BY j.task_name
    """)
    failed_rows = (
        await session.execute(
            failed_query,
            {"now": now, "window_start": now - timedelta(seconds=failed_window_seconds)},
        )
    ).all()

    failed_counts: dict[str, int] = dict.fromkeys(LLM_TASKS, 0)
    oldest_failed_age: int | None = None
    for task_name, count, age in failed_rows:
        cat = task_to_category.get(str(task_name))
        if cat:
            failed_counts[cat] += int(count or 0)
        if age is not None:
            age_int = max(int(age), 0)
            oldest_failed_age = (
                age_int if oldest_failed_age is None else max(oldest_failed_age, age_int)
            )

    return queued_counts, oldest_queued_age, failed_counts, oldest_failed_age


async def get_failed_reviews_count(
    session: AsyncSession,
    now: datetime,
    *,
    account_id: int | None = None,
    window_seconds: int = 3600,
) -> int:
    """Failed or degraded triage/summary reviews in the specified time window."""
    window_start = now - timedelta(seconds=window_seconds)
    query = text("""
        SELECT count(id)
        FROM app.agent_runs
        WHERE (CAST(:account_id AS bigint) IS NULL OR account_id = :account_id)
          AND run_type IN ('triage', 'summarize')
          AND started_at >= :window_start
          AND (status IN ('failed', 'refused') OR error_class IS NOT NULL)
    """)
    res = (
        await session.execute(query, {"window_start": window_start, "account_id": account_id})
    ).scalar()
    return int(res or 0)


async def get_action_indicators(
    session: AsyncSession,
    now: datetime,
    *,
    account_id: int | None = None,
    threshold_seconds: int = 300,
) -> tuple[int, int | None, int, int | None]:
    """Executing and delivery_uncertain actions unresolved past the threshold."""
    threshold_time = now - timedelta(seconds=threshold_seconds)

    exec_query = text("""
        SELECT count(id),
               EXTRACT(EPOCH FROM (:now - min(attempt_started_at)))
        FROM app.actions
        WHERE (CAST(:account_id AS bigint) IS NULL OR account_id = :account_id)
          AND state = 'executing'
          AND attempt_started_at <= :threshold_time
    """)
    exec_row = (
        await session.execute(
            exec_query,
            {"now": now, "threshold_time": threshold_time, "account_id": account_id},
        )
    ).one()
    exec_count = int(exec_row[0] or 0)
    exec_age = None if exec_row[1] is None or exec_count == 0 else max(int(exec_row[1]), 0)

    unc_query = text("""
        SELECT count(id),
               EXTRACT(EPOCH FROM (:now - min(COALESCE(attempt_started_at, updated_at))))
        FROM app.actions
        WHERE (CAST(:account_id AS bigint) IS NULL OR account_id = :account_id)
          AND state = 'delivery_uncertain'
          AND COALESCE(attempt_started_at, updated_at) <= :threshold_time
    """)
    unc_row = (
        await session.execute(
            unc_query,
            {"now": now, "threshold_time": threshold_time, "account_id": account_id},
        )
    ).one()
    unc_count = int(unc_row[0] or 0)
    unc_age = None if unc_row[1] is None or unc_count == 0 else max(int(unc_row[1]), 0)

    return exec_count, exec_age, unc_count, unc_age


async def get_unresolved_card_presentations(
    session: AsyncSession,
    now: datetime,
    *,
    account_id: int | None = None,
    threshold_seconds: int = 300,
) -> tuple[int, int | None]:
    """Card presentations claimed but not resolved past the threshold."""
    threshold_time = now - timedelta(seconds=threshold_seconds)
    query = text("""
        SELECT count(claim.id),
               EXTRACT(EPOCH FROM (:now - min(claim.at)))
        FROM app.audit_log AS claim
        WHERE (CAST(:account_id AS bigint) IS NULL OR claim.account_id = :account_id)
          AND claim.event = 'approval_card_presentation_claimed'
          AND claim.subject_type = 'action'
          AND claim.at <= :threshold_time
          AND NOT EXISTS (
              SELECT 1 FROM app.audit_log AS res
              WHERE res.account_id = claim.account_id
                AND res.event = 'approval_card_presentation_resolved'
                AND res.subject_type = 'action'
                AND res.subject_id = claim.subject_id
                AND CAST(res.details ->> 'claim_id' AS bigint) = claim.id
                AND res.details ->> 'outcome' IN ('shown', 'not_delivered')
          )
    """)
    row = (
        await session.execute(
            query,
            {"now": now, "threshold_time": threshold_time, "account_id": account_id},
        )
    ).one()
    count = int(row[0] or 0)
    age = None if row[1] is None or count == 0 else max(int(row[1]), 0)
    return count, age


async def get_last_retention_sweep_age(session: AsyncSession, now: datetime) -> int | None:
    """Age of the last successful retention sweep, from SystemState or procrastinate_events."""
    row = await session.get(SystemState, KEY_LAST_RETENTION_SWEEP_OK)
    if row is not None and "swept_at" in row.value:
        try:
            swept_at = datetime.fromisoformat(row.value["swept_at"])
            return max(int((now - swept_at).total_seconds()), 0)
        except ValueError, TypeError:
            pass

    query = text("""
        SELECT EXTRACT(EPOCH FROM (:now - max(e.at)))
        FROM procrastinate.procrastinate_jobs AS j
        JOIN procrastinate.procrastinate_events AS e
          ON e.job_id = j.id AND e.type::text = 'succeeded'
        WHERE j.task_name = 'system.retention_sweep'
          AND j.status::text = 'succeeded'
    """)
    res = (await session.execute(query, {"now": now})).scalar()
    return None if res is None else max(int(res), 0)


def check_backup_markers(
    status_path: Path | None, now: datetime
) -> tuple[int | None, int | None, str | None, int | None]:
    """Read backup, upload, and privacy journal markers. Content-free."""
    if status_path is None:
        return None, None, None, None

    if status_path.is_dir():
        backup_file = status_path / "last_success"
        upload_marker = status_path / UPLOAD_MARKER_NAME
        journal_status_path = status_path / "journal_status"
        last_journal_path = status_path / "last_journal_upload_success"
    else:
        backup_file = status_path
        upload_marker = status_path.with_name(UPLOAD_MARKER_NAME)
        journal_status_path = status_path.with_name("journal_status")
        last_journal_path = status_path.with_name("last_journal_upload_success")

    last_backup = read_last_success(backup_file)
    last_backup_age = max(int((now - last_backup).total_seconds()), 0) if last_backup else None

    last_upload = read_last_success(upload_marker)
    last_upload_age = max(int((now - last_upload).total_seconds()), 0) if last_upload else None

    journal_status: str | None = None
    try:
        raw_status = journal_status_path.read_text(encoding="utf-8").strip()
        journal_status = raw_status or None
    except OSError:
        journal_status = None

    last_journal = read_last_success(last_journal_path)
    last_journal_age = max(int((now - last_journal).total_seconds()), 0) if last_journal else None

    return last_backup_age, last_upload_age, journal_status, last_journal_age


def evaluate_degradation(
    indicators: ServiceIndicators,
    thresholds: ServiceMonitoringThresholds,
    *,
    backup_required: bool = False,
    backup_upload_required: bool = False,
) -> tuple[bool, tuple[str, ...]]:
    """Determine whether any indicator exceeds safe service thresholds."""
    reasons: list[str] = []

    if (
        indicators.oldest_pending_owner_request_age_seconds is not None
        and indicators.oldest_pending_owner_request_age_seconds
        > thresholds.owner_request_age_seconds
    ):
        reasons.append("owner_request_queued")

    if (
        indicators.unanswered_owner_turns >= thresholds.unanswered_turns_threshold
        and indicators.oldest_unanswered_owner_turn_age_seconds is not None
        and indicators.oldest_unanswered_owner_turn_age_seconds
        > thresholds.unanswered_turn_age_seconds
    ):
        reasons.append("unanswered_owner_turns")

    if (
        indicators.oldest_queued_llm_job_age_seconds is not None
        and indicators.oldest_queued_llm_job_age_seconds > thresholds.llm_job_age_seconds
    ):
        reasons.append("llm_jobs_queued")

    if sum(indicators.failed_llm_jobs.values()) >= thresholds.failed_reviews_threshold:
        reasons.append("llm_jobs_failed")

    if indicators.failed_reviews_window >= thresholds.failed_reviews_threshold:
        reasons.append("failed_reviews")

    if (
        indicators.actions_executing > 0
        and indicators.oldest_executing_action_age_seconds is not None
        and indicators.oldest_executing_action_age_seconds > thresholds.action_unresolved_seconds
    ):
        reasons.append("actions_executing")

    if (
        indicators.actions_delivery_uncertain > 0
        and indicators.oldest_delivery_uncertain_action_age_seconds is not None
        and indicators.oldest_delivery_uncertain_action_age_seconds
        > thresholds.action_unresolved_seconds
    ):
        reasons.append("actions_delivery_uncertain")

    if (
        indicators.unresolved_card_presentations > 0
        and indicators.oldest_unresolved_presentation_age_seconds is not None
        and indicators.oldest_unresolved_presentation_age_seconds
        > thresholds.action_unresolved_seconds
    ):
        reasons.append("card_presentations_unresolved")

    if (
        indicators.last_retention_sweep_age_seconds is not None
        and indicators.last_retention_sweep_age_seconds
        > thresholds.retention_sweep_max_age_hours * 3600
    ):
        reasons.append("retention_sweep_stale")

    if (
        backup_required
        and indicators.last_backup_age_seconds is not None
        and indicators.last_backup_age_seconds > thresholds.backup_max_age_hours * 3600
    ):
        reasons.append("backup_stale")

    if (
        backup_upload_required
        and indicators.last_backup_upload_age_seconds is not None
        and indicators.last_backup_upload_age_seconds
        > thresholds.backup_upload_max_age_hours * 3600
    ):
        reasons.append("backup_upload_stale")

    if indicators.journal_status is not None and indicators.journal_status not in (
        "acknowledged",
        "",
    ):
        reasons.append(f"privacy_journal_{indicators.journal_status}")

    return bool(reasons), tuple(reasons)


async def compute_service_indicators(
    session: AsyncSession,
    now: datetime,
    *,
    account_id: int | None = None,
    thresholds: ServiceMonitoringThresholds | None = None,
    backup_status_path: Path | None = None,
    backup_upload_required: bool = False,
) -> ServiceIndicators:
    """Compute all content-free service-completion indicators."""
    t = thresholds or ServiceMonitoringThresholds()

    oldest_pending_req = await get_oldest_pending_owner_request_age(
        session, now, account_id=account_id
    )
    unanswered_turns, oldest_unanswered = await get_unanswered_owner_turns(
        session, now, account_id=account_id, threshold_seconds=t.unanswered_turn_age_seconds
    )
    queued_llm, oldest_queued, failed_llm, oldest_failed = await get_llm_job_indicators(
        session, now, failed_window_seconds=t.review_window_seconds
    )
    failed_reviews = await get_failed_reviews_count(
        session, now, account_id=account_id, window_seconds=t.review_window_seconds
    )
    exec_count, exec_age, unc_count, unc_age = await get_action_indicators(
        session, now, account_id=account_id, threshold_seconds=t.action_unresolved_seconds
    )
    unresolved_pres_count, unresolved_pres_age = await get_unresolved_card_presentations(
        session, now, account_id=account_id, threshold_seconds=t.action_unresolved_seconds
    )
    retention_age = await get_last_retention_sweep_age(session, now)

    last_backup_age, last_upload_age, journal_status, last_journal_upload_age = (
        check_backup_markers(backup_status_path, now)
    )

    base = ServiceIndicators(
        oldest_pending_owner_request_age_seconds=oldest_pending_req,
        unanswered_owner_turns=unanswered_turns,
        oldest_unanswered_owner_turn_age_seconds=oldest_unanswered,
        queued_llm_jobs=queued_llm,
        oldest_queued_llm_job_age_seconds=oldest_queued,
        failed_llm_jobs=failed_llm,
        oldest_failed_llm_job_age_seconds=oldest_failed,
        failed_reviews_window=failed_reviews,
        actions_executing=exec_count,
        oldest_executing_action_age_seconds=exec_age,
        actions_delivery_uncertain=unc_count,
        oldest_delivery_uncertain_action_age_seconds=unc_age,
        unresolved_card_presentations=unresolved_pres_count,
        oldest_unresolved_presentation_age_seconds=unresolved_pres_age,
        last_retention_sweep_age_seconds=retention_age,
        last_backup_age_seconds=last_backup_age,
        last_backup_upload_age_seconds=last_upload_age,
        journal_status=journal_status,
        last_journal_upload_age_seconds=last_journal_upload_age,
    )

    degraded, reasons = evaluate_degradation(
        base,
        t,
        backup_required=backup_status_path is not None,
        backup_upload_required=backup_upload_required,
    )

    return ServiceIndicators(
        oldest_pending_owner_request_age_seconds=base.oldest_pending_owner_request_age_seconds,
        unanswered_owner_turns=base.unanswered_owner_turns,
        oldest_unanswered_owner_turn_age_seconds=base.oldest_unanswered_owner_turn_age_seconds,
        queued_llm_jobs=base.queued_llm_jobs,
        oldest_queued_llm_job_age_seconds=base.oldest_queued_llm_job_age_seconds,
        failed_llm_jobs=base.failed_llm_jobs,
        oldest_failed_llm_job_age_seconds=base.oldest_failed_llm_job_age_seconds,
        failed_reviews_window=base.failed_reviews_window,
        actions_executing=base.actions_executing,
        oldest_executing_action_age_seconds=base.oldest_executing_action_age_seconds,
        actions_delivery_uncertain=base.actions_delivery_uncertain,
        oldest_delivery_uncertain_action_age_seconds=(
            base.oldest_delivery_uncertain_action_age_seconds
        ),
        unresolved_card_presentations=base.unresolved_card_presentations,
        oldest_unresolved_presentation_age_seconds=base.oldest_unresolved_presentation_age_seconds,
        last_retention_sweep_age_seconds=base.last_retention_sweep_age_seconds,
        last_backup_age_seconds=base.last_backup_age_seconds,
        last_backup_upload_age_seconds=base.last_backup_upload_age_seconds,
        journal_status=base.journal_status,
        last_journal_upload_age_seconds=base.last_journal_upload_age_seconds,
        degraded=degraded,
        degradation_reasons=reasons,
    )


async def check_service_monitoring(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    alerts: OwnerAlerts,
    *,
    thresholds: ServiceMonitoringThresholds | None = None,
    backup_status_path: Path | None = None,
    backup_upload_required: bool = False,
) -> ServiceMonitoringOutcome:
    """Periodic check job: tracks consecutive degradation checks and alerts on degradation."""
    now = clock.now()
    t = thresholds or ServiceMonitoringThresholds()

    async with transaction(session_factory) as uow:
        state_row = await uow.session.get(SystemState, KEY_SERVICE_MONITORING, with_for_update=True)
        if state_row is None:
            state_row = SystemState(key=KEY_SERVICE_MONITORING, value={})
            uow.session.add(state_row)

        current_consecutive = int(state_row.value.get("consecutive_degraded_checks", 0))
        currently_alerted = bool(state_row.value.get("currently_alerted", False))

        indicators = await compute_service_indicators(
            uow.session,
            now,
            thresholds=t,
            backup_status_path=backup_status_path,
            backup_upload_required=backup_upload_required,
        )

        should_alert = False
        should_recover = False

        if indicators.degraded:
            new_consecutive = current_consecutive + 1
            if new_consecutive >= t.consecutive_checks and not currently_alerted:
                should_alert = True
                new_alerted = True
            else:
                new_alerted = currently_alerted
        else:
            new_consecutive = 0
            if currently_alerted:
                should_recover = True
            new_alerted = False

        state_row.value = {
            "consecutive_degraded_checks": new_consecutive,
            "currently_alerted": new_alerted,
            "degraded_reasons": list(indicators.degradation_reasons),
            "last_check_at": now.isoformat(),
        }

    # Deliver alerts outside the state transaction
    if should_alert:
        logger.warning(
            "service_degraded_alert",
            reasons=indicators.degradation_reasons,
            consecutive=new_consecutive,
        )
        await alerts.notify_all_owners(
            AlertKind.SERVICE_DEGRADED,
            dedupe_key=f"service_degraded:{int(now.timestamp())}",
            cooldown=timedelta(minutes=15),
        )
    elif should_recover:
        logger.info("service_recovered_alert")
        await alerts.notify_all_owners(
            AlertKind.SERVICE_RECOVERED,
            dedupe_key=f"service_recovered:{int(now.timestamp())}",
            cooldown=timedelta(minutes=15),
        )

    return ServiceMonitoringOutcome(
        indicators=indicators,
        alerted=should_alert,
        recovered=should_recover,
        consecutive_checks=new_consecutive,
    )


def format_service_status(indicators: ServiceIndicators) -> str:
    """Format indicators into a content-free summary for CLI and logs."""
    lines = [
        "Service completion indicators:",
        (
            f"  Unanswered owner turns: {indicators.unanswered_owner_turns}"
            + (
                f" (oldest {indicators.oldest_unanswered_owner_turn_age_seconds}s)"
                if indicators.oldest_unanswered_owner_turn_age_seconds is not None
                else ""
            )
        ),
        (
            f"  Oldest pending owner request: "
            f"{indicators.oldest_pending_owner_request_age_seconds}s"
            if indicators.oldest_pending_owner_request_age_seconds is not None
            else "  Oldest pending owner request: none"
        ),
        (
            f"  Queued LLM jobs: {sum(indicators.queued_llm_jobs.values())}"
            + (
                f" (oldest {indicators.oldest_queued_llm_job_age_seconds}s)"
                if indicators.oldest_queued_llm_job_age_seconds is not None
                else ""
            )
        ),
        (
            f"  Failed LLM jobs: {sum(indicators.failed_llm_jobs.values())}"
            + (
                f" (oldest {indicators.oldest_failed_llm_job_age_seconds}s)"
                if indicators.oldest_failed_llm_job_age_seconds is not None
                else ""
            )
        ),
        f"  Failed reviews (window): {indicators.failed_reviews_window}",
        f"  Actions executing: {indicators.actions_executing}",
        f"  Actions delivery uncertain: {indicators.actions_delivery_uncertain}",
        f"  Unresolved card presentations: {indicators.unresolved_card_presentations}",
        (
            f"  Last retention sweep: {indicators.last_retention_sweep_age_seconds}s ago"
            if indicators.last_retention_sweep_age_seconds is not None
            else "  Last retention sweep: none"
        ),
        (
            f"  Last backup: {indicators.last_backup_age_seconds}s ago"
            if indicators.last_backup_age_seconds is not None
            else "  Last backup: none"
        ),
        (
            f"  Last backup upload: {indicators.last_backup_upload_age_seconds}s ago"
            if indicators.last_backup_upload_age_seconds is not None
            else "  Last backup upload: none"
        ),
        f"  Privacy journal status: {indicators.journal_status or 'none'}",
        (
            f"  Status: degraded ({', '.join(indicators.degradation_reasons)})"
            if indicators.degraded
            else "  Status: normal"
        ),
    ]
    return "\n".join(lines)
