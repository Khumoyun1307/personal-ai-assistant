"""Integration tests for service-completion monitoring (I26, ARCHITECTURE §17).

- Evaluates content-free service indicators from PostgreSQL and procrastinate.
- Sustained degradation over N consecutive checks raises exactly one alert.
- Degradation clearance sends a recovery notification and resets the tracking state.
- Tenant isolation: indicators are strictly scoped to the monitored tenant.
- Content-free guarantee: no message text, contact names, or identifiers appear in alerts or status.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import service
from assistant.accounts.models import Account, AuditLogEntry, ControlTurn
from assistant.actions.models import Action
from assistant.actions.states import ActionKind, ActionOrigin, ActionState
from assistant.agent.models import AgentRun
from assistant.core.jobs import OWNER_REQUEST_TASK, owner_control_lock
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.models import (
    KEY_LAST_RETENTION_SWEEP_OK,
    KEY_SERVICE_MONITORING,
    SystemState,
)
from assistant.db.uow import transaction
from assistant.inbox.notify import ALERT_TEXT, AlertKind, OwnerAlerts
from assistant.jobs.app import build_app
from assistant.jobs.scheduler import ProcrastinateScheduler
from assistant.observability.service_monitoring import (
    ServiceMonitoringThresholds,
    check_service_monitoring,
    compute_service_indicators,
    format_service_status,
)
from assistant.observability.status import build_status
from tests.fakes.channel import FakeChannel
from tests.fakes.clock import FakeClock
from tests.integration.actions_env import seed_chat

pytestmark = pytest.mark.integration

OWNER_A = 7000000001
OWNER_B = 7000000002
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)

TRUNCATE = (
    "TRUNCATE app.accounts, app.inbound_updates, app.system_state, app.actions, "
    "app.conversations, app.contacts, app.agent_runs, app.audit_log, app.control_turns CASCADE; "
    "TRUNCATE procrastinate.procrastinate_jobs, procrastinate.procrastinate_events, "
    "procrastinate.procrastinate_periodic_defers, procrastinate.procrastinate_workers CASCADE"
)


@pytest.fixture
async def factory(migrated_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(TRUNCATE)
    yield build_session_factory(engine)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(TRUNCATE)
    await engine.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(NOW)


async def setup_owner(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, *, user_id: int = OWNER_A
) -> int:
    async with transaction(factory) as uow:
        account, _ = await service.get_or_create_account(
            uow.session, telegram_user_id=user_id, clock=clock
        )
        await service.set_control_chat(uow.session, account, user_id)
        return account.id


def create_backup_dir(tmp_path: Path, now: datetime) -> Path:
    status_dir = tmp_path / "backup_status"
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / "last_success").write_text(now.strftime("%Y%m%dT%H%M%SZ\n"), encoding="utf-8")
    (status_dir / "last_upload_success").write_text(
        now.strftime("%Y%m%dT%H%M%SZ\n"), encoding="utf-8"
    )
    (status_dir / "journal_status").write_text("acknowledged\n", encoding="utf-8")
    return status_dir


async def test_service_monitoring_healthy_produces_no_alerts(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, tmp_path: Path
) -> None:
    await setup_owner(factory, clock)
    channel = FakeChannel()
    alerts = OwnerAlerts(factory, channel, clock)
    backup_dir = create_backup_dir(tmp_path, clock.now())

    async with transaction(factory) as uow:
        uow.session.add(
            SystemState(
                key=KEY_LAST_RETENTION_SWEEP_OK,
                value={"swept_at": clock.now().isoformat()},
            )
        )

    thresholds = ServiceMonitoringThresholds()
    outcome = await check_service_monitoring(
        factory,
        clock,
        alerts,
        thresholds=thresholds,
        backup_status_path=backup_dir,
        backup_upload_required=True,
    )

    assert not outcome.indicators.degraded
    assert not outcome.alerted
    assert not outcome.indicators.degradation_reasons
    assert channel.sends == []


async def test_service_monitoring_sustained_window_and_recovery(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, tmp_path: Path
) -> None:
    await setup_owner(factory, clock)
    channel = FakeChannel()
    alerts = OwnerAlerts(factory, channel, clock)
    backup_dir = create_backup_dir(tmp_path, clock.now())

    # Seed retention sweep as stale (5 hours ago, threshold is 4 hours)
    stale_time = clock.now() - timedelta(hours=5)
    async with transaction(factory) as uow:
        uow.session.add(
            SystemState(
                key=KEY_LAST_RETENTION_SWEEP_OK,
                value={"swept_at": stale_time.isoformat()},
            )
        )

    thresholds = ServiceMonitoringThresholds(consecutive_checks=3, retention_sweep_max_age_hours=4)

    # Check 1: Degraded detected, failure count = 1 -> no alert sent yet
    outcome1 = await check_service_monitoring(
        factory,
        clock,
        alerts,
        thresholds=thresholds,
        backup_status_path=backup_dir,
        backup_upload_required=True,
    )
    assert outcome1.indicators.degraded
    assert not outcome1.alerted
    assert channel.sends == []

    # Check 2: Advance clock by 1 minute, failure count = 2 -> still no alert
    clock.advance(minutes=1)
    outcome2 = await check_service_monitoring(
        factory,
        clock,
        alerts,
        thresholds=thresholds,
        backup_status_path=backup_dir,
        backup_upload_required=True,
    )
    assert outcome2.indicators.degraded
    assert not outcome2.alerted
    assert channel.sends == []

    # Check 3: Advance clock by 1 minute, failure count = 3 -> alert sent!
    clock.advance(minutes=1)
    outcome3 = await check_service_monitoring(
        factory,
        clock,
        alerts,
        thresholds=thresholds,
        backup_status_path=backup_dir,
        backup_upload_required=True,
    )
    assert outcome3.indicators.degraded
    assert outcome3.alerted
    assert len(channel.sends) == 1
    assert ALERT_TEXT[AlertKind.SERVICE_DEGRADED] in channel.sends[0]

    # Check 4: Still degraded -> deduplicated, no duplicate alert sent
    clock.advance(minutes=1)
    outcome4 = await check_service_monitoring(
        factory,
        clock,
        alerts,
        thresholds=thresholds,
        backup_status_path=backup_dir,
        backup_upload_required=True,
    )
    assert outcome4.indicators.degraded
    assert not outcome4.alerted
    assert len(channel.sends) == 1

    # Now clear degradation: record successful retention sweep
    async with transaction(factory) as uow:
        state = await uow.session.get(SystemState, KEY_LAST_RETENTION_SWEEP_OK)
        assert state is not None
        state.value = {"swept_at": clock.now().isoformat()}

    # Check 5: Healthy again -> recovery alert sent!
    clock.advance(minutes=1)
    outcome5 = await check_service_monitoring(
        factory,
        clock,
        alerts,
        thresholds=thresholds,
        backup_status_path=backup_dir,
        backup_upload_required=True,
    )
    assert not outcome5.indicators.degraded
    assert outcome5.recovered
    assert len(channel.sends) == 2
    assert ALERT_TEXT[AlertKind.SERVICE_RECOVERED] in channel.sends[1]

    # Check 6: Next healthy check -> no further messages
    clock.advance(minutes=1)
    outcome6 = await check_service_monitoring(
        factory,
        clock,
        alerts,
        thresholds=thresholds,
        backup_status_path=backup_dir,
        backup_upload_required=True,
    )
    assert not outcome6.indicators.degraded
    assert not outcome6.recovered
    assert len(channel.sends) == 2


async def test_pending_owner_request_indicator(
    migrated_url: str, factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    account_id = await setup_owner(factory, clock)
    old_turn_time = clock.now() - timedelta(minutes=10)

    async with transaction(factory) as uow:
        turn = ControlTurn(
            account_id=account_id,
            role="owner",
            text="Synthetic question",
            refs={},
        )
        turn.created_at = old_turn_time
        uow.session.add(turn)
        await uow.session.flush()
        turn_id = turn.id

    app = build_app(migrated_url)

    @app.task(name=OWNER_REQUEST_TASK, queue="llm")
    async def dummy_owner_request(turn_id: int) -> None:
        pass

    scheduler = ProcrastinateScheduler(app)
    async with app.open_async():
        await scheduler.defer(
            OWNER_REQUEST_TASK,
            args={"turn_id": turn_id},
            lock=owner_control_lock(account_id),
        )

    thresholds = ServiceMonitoringThresholds(owner_request_age_seconds=300)
    async with factory() as session:
        indicators = await compute_service_indicators(session, clock.now(), thresholds=thresholds)

    assert indicators.oldest_pending_owner_request_age_seconds is not None
    assert indicators.oldest_pending_owner_request_age_seconds >= 600
    assert indicators.degraded


async def test_unanswered_owner_turns_indicator(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    account_id = await setup_owner(factory, clock)
    turn_time = clock.now() - timedelta(minutes=15)

    async with transaction(factory) as uow:
        turn = ControlTurn(
            account_id=account_id,
            role="owner",
            text="Unanswered question",
            refs={},
        )
        turn.created_at = turn_time
        uow.session.add(turn)

    thresholds = ServiceMonitoringThresholds(
        unanswered_turns_threshold=1,
        unanswered_turn_age_seconds=600,
    )
    async with factory() as session:
        ind1 = await compute_service_indicators(session, clock.now(), thresholds=thresholds)

    assert ind1.unanswered_owner_turns == 1
    assert ind1.oldest_unanswered_owner_turn_age_seconds is not None
    assert ind1.oldest_unanswered_owner_turn_age_seconds >= 900
    assert ind1.degraded

    # Assistant answers the question
    async with transaction(factory) as uow:
        reply = ControlTurn(
            account_id=account_id,
            role="assistant",
            text="Synthetic answer",
            refs={},
        )
        uow.session.add(reply)

    async with factory() as session:
        ind2 = await compute_service_indicators(session, clock.now(), thresholds=thresholds)

    assert ind2.unanswered_owner_turns == 0
    assert ind2.oldest_unanswered_owner_turn_age_seconds is None


async def test_failed_reviews_indicator(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    account_id = await setup_owner(factory, clock)

    async with transaction(factory) as uow:
        # Add 3 failed triage runs within the window
        for i in range(3):
            run = AgentRun(
                account_id=account_id,
                run_type="triage",
                model="gpt-6-luna",
                status="failed",
                error_class="APIConnectionError",
                started_at=clock.now() - timedelta(minutes=10 * (i + 1)),
            )
            uow.session.add(run)

        # Add 1 failed run outside the 1-hour window
        old_run = AgentRun(
            account_id=account_id,
            run_type="triage",
            model="gpt-6-luna",
            status="failed",
            error_class="APIConnectionError",
            started_at=clock.now() - timedelta(hours=3),
        )
        uow.session.add(old_run)

    thresholds = ServiceMonitoringThresholds(
        failed_reviews_threshold=3,
        review_window_seconds=3600,
    )
    async with factory() as session:
        indicators = await compute_service_indicators(session, clock.now(), thresholds=thresholds)

    assert indicators.failed_reviews_window == 3
    assert indicators.degraded


async def test_actions_executing_and_uncertain_indicators(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    await setup_owner(factory, clock)

    async with transaction(factory) as uow:
        chat = await seed_chat(uow.session, clock.now())
        # Action in executing past threshold (20 min ago)
        act_exec = Action(
            account_id=chat.account_id,
            conversation_id=chat.conversation_id,
            kind=ActionKind.SEND_MESSAGE.value,
            origin=ActionOrigin.OWNER_DICTATED.value,
            state=ActionState.EXECUTING.value,
            attempt_started_at=clock.now() - timedelta(minutes=20),
        )
        # Action in delivery_uncertain past threshold (25 min ago)
        act_unc = Action(
            account_id=chat.account_id,
            conversation_id=chat.conversation_id,
            kind=ActionKind.SEND_MESSAGE.value,
            origin=ActionOrigin.OWNER_DICTATED.value,
            state=ActionState.DELIVERY_UNCERTAIN.value,
            attempt_started_at=clock.now() - timedelta(minutes=25),
        )
        uow.session.add_all([act_exec, act_unc])

    thresholds = ServiceMonitoringThresholds(action_unresolved_seconds=900)
    async with factory() as session:
        indicators = await compute_service_indicators(session, clock.now(), thresholds=thresholds)

    assert indicators.actions_executing == 1
    assert indicators.oldest_executing_action_age_seconds is not None
    assert indicators.oldest_executing_action_age_seconds >= 1200
    assert indicators.actions_delivery_uncertain == 1
    assert indicators.oldest_delivery_uncertain_action_age_seconds is not None
    assert indicators.oldest_delivery_uncertain_action_age_seconds >= 1500
    assert indicators.degraded


async def test_unresolved_card_presentations_indicator(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    account_id = await setup_owner(factory, clock)

    async with transaction(factory) as uow:
        claim = AuditLogEntry(
            account_id=account_id,
            at=clock.now() - timedelta(minutes=20),
            actor="system",
            event="approval_card_presentation_claimed",
            subject_type="action",
            subject_id=999,
            details={"version": 1},
        )
        uow.session.add(claim)
        await uow.session.flush()
        claim_id = claim.id

    thresholds = ServiceMonitoringThresholds(action_unresolved_seconds=900)
    async with factory() as session:
        ind1 = await compute_service_indicators(session, clock.now(), thresholds=thresholds)

    assert ind1.unresolved_card_presentations == 1
    assert ind1.oldest_unresolved_presentation_age_seconds is not None
    assert ind1.oldest_unresolved_presentation_age_seconds >= 1200
    assert ind1.degraded

    # Resolve presentation
    async with transaction(factory) as uow:
        resolution = AuditLogEntry(
            account_id=account_id,
            at=clock.now() - timedelta(minutes=5),
            actor="system",
            event="approval_card_presentation_resolved",
            subject_type="action",
            subject_id=999,
            details={"version": 1, "claim_id": claim_id, "outcome": "shown"},
        )
        uow.session.add(resolution)

    async with factory() as session:
        ind2 = await compute_service_indicators(session, clock.now(), thresholds=thresholds)

    assert ind2.unresolved_card_presentations == 0
    assert ind2.oldest_unresolved_presentation_age_seconds is None


async def test_tenant_isolation_on_indicators(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    account_a = await setup_owner(factory, clock, user_id=OWNER_A)
    account_b = await setup_owner(factory, clock, user_id=OWNER_B)

    # Insert an unanswered turn and a failed review strictly for Account B
    async with transaction(factory) as uow:
        turn_b = ControlTurn(
            account_id=account_b,
            role="owner",
            text="Account B private turn",
            refs={},
        )
        turn_b.created_at = clock.now() - timedelta(minutes=30)
        uow.session.add(turn_b)

        run_b = AgentRun(
            account_id=account_b,
            run_type="triage",
            model="gpt-6-luna",
            status="failed",
            error_class="InternalError",
            started_at=clock.now() - timedelta(minutes=15),
        )
        uow.session.add(run_b)

    async with factory() as session:
        ind_a = await compute_service_indicators(session, clock.now(), account_id=account_a)
        ind_b = await compute_service_indicators(session, clock.now(), account_id=account_b)

    # Account A is unaffected
    assert ind_a.unanswered_owner_turns == 0
    assert ind_a.failed_reviews_window == 0

    # Account B reflects its own issues
    assert ind_b.unanswered_owner_turns == 1
    assert ind_b.failed_reviews_window == 1


async def test_content_free_redaction_and_status(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    account_id = await setup_owner(factory, clock)
    secret_text = "CANARY-SECRET-MESSAGE-42"

    async with transaction(factory) as uow:
        turn = ControlTurn(
            account_id=account_id,
            role="owner",
            text=secret_text,
            refs={},
        )
        turn.created_at = clock.now() - timedelta(minutes=20)
        uow.session.add(turn)

    async with factory() as session:
        indicators = await compute_service_indicators(session, clock.now())
        account = await session.get(Account, account_id)
        assert account is not None
        status = await build_status(session, account, clock.now(), monthly_budget_usd=None)

    # Content-free guarantee
    formatted = format_service_status(indicators)
    assert secret_text not in formatted
    assert secret_text not in status.service.line
    assert status.service.degraded
    assert "⚠️ Service: degraded" in status.service.line


async def _insert_llm_job(
    factory: async_sessionmaker[AsyncSession],
    *,
    task: str,
    status: str,
    event_type: str,
    event_at: datetime,
) -> None:
    async with factory() as session, session.begin():
        job_id = (
            await session.execute(
                text(
                    "INSERT INTO procrastinate.procrastinate_jobs "
                    "(queue_name, task_name, priority, args, status) "
                    "VALUES ('llm', :task, 0, CAST('{}' AS jsonb), "
                    "CAST(:status AS procrastinate.procrastinate_job_status)) "
                    "RETURNING id"
                ),
                {"task": task, "status": status},
            )
        ).scalar_one()
        await session.execute(
            text("DELETE FROM procrastinate.procrastinate_events WHERE job_id = :job_id"),
            {"job_id": job_id},
        )
        await session.execute(
            text(
                "INSERT INTO procrastinate.procrastinate_events (job_id, type, at) "
                "VALUES (:job_id, CAST(:etype AS procrastinate.procrastinate_job_event_type), :at)"
            ),
            {"job_id": job_id, "etype": event_type, "at": event_at},
        )


async def test_failed_llm_jobs_count_only_inside_window(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    """Procrastinate keeps failed jobs forever, so old failures must not hold the alert up."""
    await setup_owner(factory, clock)
    thresholds = ServiceMonitoringThresholds(failed_reviews_threshold=2, review_window_seconds=3600)
    for minutes_ago in (10, 20):
        await _insert_llm_job(
            factory,
            task="inbox.triage",
            status="failed",
            event_type="failed",
            event_at=clock.now() - timedelta(minutes=minutes_ago),
        )
    await _insert_llm_job(
        factory,
        task="memory.refresh_summary",
        status="failed",
        event_type="failed",
        event_at=clock.now() - timedelta(hours=5),
    )

    async with factory() as session:
        inside = await compute_service_indicators(session, clock.now(), thresholds=thresholds)
    assert sum(inside.failed_llm_jobs.values()) == 2
    assert inside.failed_llm_jobs["summary"] == 0
    assert "llm_jobs_failed" in inside.degradation_reasons

    clock.advance(hours=2)
    async with factory() as session:
        later = await compute_service_indicators(session, clock.now(), thresholds=thresholds)
    assert sum(later.failed_llm_jobs.values()) == 0
    assert "llm_jobs_failed" not in later.degradation_reasons


async def test_queued_llm_job_age_counts_from_scheduled_time(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    await setup_owner(factory, clock)
    await _insert_llm_job(
        factory,
        task="replies.draft",
        status="todo",
        event_type="deferred",
        event_at=clock.now() - timedelta(hours=1),
    )
    thresholds = ServiceMonitoringThresholds(llm_job_age_seconds=600)
    async with factory() as session:
        stuck = await compute_service_indicators(session, clock.now(), thresholds=thresholds)
    assert stuck.queued_llm_jobs["drafting"] == 1
    assert stuck.oldest_queued_llm_job_age_seconds is not None
    assert stuck.oldest_queued_llm_job_age_seconds >= 3600
    assert "llm_jobs_queued" in stuck.degradation_reasons

    # The same job deliberately scheduled 5 minutes ago is only 5 minutes late.
    async with factory() as session, session.begin():
        await session.execute(
            text("UPDATE procrastinate.procrastinate_jobs SET scheduled_at = :at"),
            {"at": clock.now() - timedelta(minutes=5)},
        )
    async with factory() as session:
        fresh = await compute_service_indicators(session, clock.now(), thresholds=thresholds)
    assert fresh.oldest_queued_llm_job_age_seconds is not None
    assert fresh.oldest_queued_llm_job_age_seconds < 600
    assert "llm_jobs_queued" not in fresh.degradation_reasons


async def _seed_unanswered_turn(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, account_id: int
) -> None:
    async with transaction(factory) as uow:
        turn = ControlTurn(account_id=account_id, role="owner", text="Synthetic", refs={})
        turn.created_at = clock.now() - timedelta(minutes=30)
        uow.session.add(turn)


async def _clear_unanswered_turn(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, account_id: int
) -> None:
    async with transaction(factory) as uow:
        uow.session.add(ControlTurn(account_id=account_id, role="assistant", text="Ok", refs={}))


async def _seed_failed_reviews(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, account_id: int
) -> None:
    async with transaction(factory) as uow:
        for minutes_ago in (5, 10, 15, 20, 25):
            uow.session.add(
                AgentRun(
                    account_id=account_id,
                    run_type="summarize",
                    model="gpt-6-luna",
                    status="failed",
                    error_class="APIConnectionError",
                    started_at=clock.now() - timedelta(minutes=minutes_ago),
                )
            )


async def _clear_failed_reviews(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, account_id: int
) -> None:
    async with transaction(factory) as uow:
        await uow.session.execute(text("DELETE FROM app.agent_runs"))


async def _seed_executing_action(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, account_id: int
) -> None:
    async with transaction(factory) as uow:
        chat = await seed_chat(uow.session, clock.now())
        uow.session.add(
            Action(
                account_id=chat.account_id,
                conversation_id=chat.conversation_id,
                kind=ActionKind.SEND_MESSAGE.value,
                origin=ActionOrigin.OWNER_DICTATED.value,
                state=ActionState.EXECUTING.value,
                attempt_started_at=clock.now() - timedelta(minutes=30),
            )
        )
        # seed_chat creates its own account; keep a single reachable owner.
        await uow.session.execute(
            text("UPDATE app.accounts SET control_chat_id = NULL WHERE id != :id"),
            {"id": account_id},
        )


async def _clear_executing_action(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, account_id: int
) -> None:
    async with transaction(factory) as uow:
        await uow.session.execute(
            text("UPDATE app.actions SET state = 'succeeded' WHERE state = 'executing'")
        )


async def _seed_stale_retention(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, account_id: int
) -> None:
    async with transaction(factory) as uow:
        uow.session.add(
            SystemState(
                key=KEY_LAST_RETENTION_SWEEP_OK,
                value={"swept_at": (clock.now() - timedelta(hours=9)).isoformat()},
            )
        )


async def _clear_stale_retention(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, account_id: int
) -> None:
    async with transaction(factory) as uow:
        state = await uow.session.get(SystemState, KEY_LAST_RETENTION_SWEEP_OK)
        assert state is not None
        state.value = {"swept_at": clock.now().isoformat()}


_SEED_CLEAR = {
    "unanswered_turn": (_seed_unanswered_turn, _clear_unanswered_turn),
    "failed_reviews": (_seed_failed_reviews, _clear_failed_reviews),
    "executing_action": (_seed_executing_action, _clear_executing_action),
    "stale_retention": (_seed_stale_retention, _clear_stale_retention),
}


@pytest.mark.parametrize("scenario", sorted(_SEED_CLEAR))
async def test_each_indicator_alerts_once_after_window_then_recovers(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, scenario: str
) -> None:
    account_id = await setup_owner(factory, clock)
    seed, clear = _SEED_CLEAR[scenario]
    channel = FakeChannel()
    alerts = OwnerAlerts(factory, channel, clock)
    thresholds = ServiceMonitoringThresholds(consecutive_checks=3)
    await seed(factory, clock, account_id)

    for _ in range(2):
        outcome = await check_service_monitoring(factory, clock, alerts, thresholds=thresholds)
        assert outcome.indicators.degraded
        assert not outcome.alerted
        clock.advance(minutes=1)
    assert channel.sends == []

    outcome = await check_service_monitoring(factory, clock, alerts, thresholds=thresholds)
    assert outcome.alerted
    assert len(channel.sends) == 1

    for _ in range(3):
        clock.advance(minutes=1)
        again = await check_service_monitoring(factory, clock, alerts, thresholds=thresholds)
        assert not again.alerted
    assert len(channel.sends) == 1

    await clear(factory, clock, account_id)
    clock.advance(minutes=1)
    recovered = await check_service_monitoring(factory, clock, alerts, thresholds=thresholds)
    assert not recovered.indicators.degraded
    assert recovered.recovered
    assert len(channel.sends) == 2
    clock.advance(minutes=1)
    quiet = await check_service_monitoring(factory, clock, alerts, thresholds=thresholds)
    assert not quiet.recovered
    assert len(channel.sends) == 2


async def test_failed_llm_job_multiple_events_counted_once(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    """A retried job generates multiple failed events; it must be counted once (DISTINCT)."""
    await setup_owner(factory, clock)
    thresholds = ServiceMonitoringThresholds(failed_reviews_threshold=2, review_window_seconds=3600)

    # Insert a single failed job with 3 failed events (simulating 3 retries)
    async with factory() as session, session.begin():
        job_id = (
            await session.execute(
                text(
                    "INSERT INTO procrastinate.procrastinate_jobs "
                    "(queue_name, task_name, priority, args, status) "
                    "VALUES ('llm', 'inbox.triage', 0, CAST('{}' AS jsonb), "
                    "CAST('failed' AS procrastinate.procrastinate_job_status)) "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        for minutes_ago in (5, 10, 15):
            await session.execute(
                text(
                    "INSERT INTO procrastinate.procrastinate_events (job_id, type, at) "
                    "VALUES (:job_id, "
                    "CAST('failed' AS procrastinate.procrastinate_job_event_type), :at)"
                ),
                {"job_id": job_id, "at": clock.now() - timedelta(minutes=minutes_ago)},
            )

    async with factory() as session:
        indicators = await compute_service_indicators(session, clock.now(), thresholds=thresholds)

    # Must be 1 distinct job, not 3 events
    assert indicators.failed_llm_jobs["triage"] == 1
    assert sum(indicators.failed_llm_jobs.values()) == 1
    assert not indicators.degraded


async def test_service_monitoring_missing_backup_markers_degraded(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, tmp_path: Path
) -> None:
    """When backup status path is configured but markers are missing, service is degraded."""
    await setup_owner(factory, clock)
    empty_backup_dir = tmp_path / "empty_backup_status"
    empty_backup_dir.mkdir(parents=True, exist_ok=True)

    async with transaction(factory) as uow:
        uow.session.add(
            SystemState(
                key=KEY_LAST_RETENTION_SWEEP_OK,
                value={"swept_at": clock.now().isoformat()},
            )
        )

    # With backup_status_path configured and upload required: missing markers count as degraded
    async with factory() as session:
        indicators = await compute_service_indicators(
            session,
            clock.now(),
            backup_status_path=empty_backup_dir,
            backup_upload_required=True,
        )
    assert indicators.degraded
    assert "backup_stale" in indicators.degradation_reasons
    assert "backup_upload_stale" in indicators.degradation_reasons

    # In local dev (backup_status_path is None): stays silent
    async with factory() as session:
        dev_indicators = await compute_service_indicators(
            session,
            clock.now(),
            backup_status_path=None,
            backup_upload_required=False,
        )
    assert not dev_indicators.degraded
    assert "backup_stale" not in dev_indicators.degradation_reasons
    assert "backup_upload_stale" not in dev_indicators.degradation_reasons


async def test_service_monitoring_delivery_failure_retries_and_second_incident(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock, tmp_path: Path
) -> None:
    """Delivery failure leaves state unalerted so the next check retries.
    A second incident shortly after recovery still alerts (unique per-incident key).
    """
    await setup_owner(factory, clock)
    channel = FakeChannel()
    alerts = OwnerAlerts(factory, channel, clock)
    backup_dir = create_backup_dir(tmp_path, clock.now())

    # Seed stale retention sweep
    stale_time = clock.now() - timedelta(hours=5)
    async with transaction(factory) as uow:
        uow.session.add(
            SystemState(
                key=KEY_LAST_RETENTION_SWEEP_OK,
                value={"swept_at": stale_time.isoformat()},
            )
        )

    thresholds = ServiceMonitoringThresholds(consecutive_checks=3, retention_sweep_max_age_hours=4)

    # Check 1 and 2
    for _ in range(2):
        outcome = await check_service_monitoring(
            factory,
            clock,
            alerts,
            thresholds=thresholds,
            backup_status_path=backup_dir,
            backup_upload_required=True,
        )
        assert outcome.indicators.degraded
        assert not outcome.alerted
        clock.advance(minutes=1)

    # Check 3: Degradation sustained, but delivery fails!
    channel.deliver = False
    outcome3 = await check_service_monitoring(
        factory,
        clock,
        alerts,
        thresholds=thresholds,
        backup_status_path=backup_dir,
        backup_upload_required=True,
    )
    assert outcome3.indicators.degraded
    assert not outcome3.alerted

    # Verify currently_alerted was NOT marked true in the DB
    async with factory() as session:
        state_row = await session.get(SystemState, KEY_SERVICE_MONITORING)
        assert state_row is not None
        assert not state_row.value.get("currently_alerted")

    # Check 4: Next check, channel restored -> delivery succeeds!
    channel.deliver = True
    clock.advance(minutes=1)
    outcome4 = await check_service_monitoring(
        factory,
        clock,
        alerts,
        thresholds=thresholds,
        backup_status_path=backup_dir,
        backup_upload_required=True,
    )
    assert outcome4.indicators.degraded
    assert outcome4.alerted
    assert len(channel.sends) == 2  # 1 failed attempt, 1 successful send
    assert ALERT_TEXT[AlertKind.SERVICE_DEGRADED] in channel.sends[1]

    # Verify currently_alerted IS now marked true in the DB
    async with factory() as session:
        state_row = await session.get(SystemState, KEY_SERVICE_MONITORING)
        assert state_row is not None
        assert state_row.value.get("currently_alerted")

    # Clear degradation
    async with transaction(factory) as uow:
        state = await uow.session.get(SystemState, KEY_LAST_RETENTION_SWEEP_OK)
        assert state is not None
        state.value = {"swept_at": clock.now().isoformat()}

    # Recovery check
    clock.advance(minutes=1)
    outcome_rec = await check_service_monitoring(
        factory,
        clock,
        alerts,
        thresholds=thresholds,
        backup_status_path=backup_dir,
        backup_upload_required=True,
    )
    assert not outcome_rec.indicators.degraded
    assert outcome_rec.recovered
    assert ALERT_TEXT[AlertKind.SERVICE_RECOVERED] in channel.sends[-1]

    # Advance clock by only 2 minutes (within the 15-minute cooldown window)
    clock.advance(minutes=2)

    # Second incident begins
    async with transaction(factory) as uow:
        state = await uow.session.get(SystemState, KEY_LAST_RETENTION_SWEEP_OK)
        assert state is not None
        state.value = {"swept_at": (clock.now() - timedelta(hours=6)).isoformat()}

    for _ in range(2):
        out_sub = await check_service_monitoring(
            factory,
            clock,
            alerts,
            thresholds=thresholds,
            backup_status_path=backup_dir,
            backup_upload_required=True,
        )
        assert out_sub.indicators.degraded
        assert not out_sub.alerted
        clock.advance(minutes=1)

    # Third check of second incident: should alert, NOT swallowed by cooldown
    out_second_inc = await check_service_monitoring(
        factory,
        clock,
        alerts,
        thresholds=thresholds,
        backup_status_path=backup_dir,
        backup_upload_required=True,
    )
    assert out_second_inc.indicators.degraded
    assert out_second_inc.alerted
