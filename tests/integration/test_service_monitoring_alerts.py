"""End-to-end alert coverage for the I26 indicators not driven through delivery elsewhere.

`test_service_monitoring.py` drives unanswered turns, failed reviews, executing actions and
the retention sweep to a delivered alert. These cases cover the remaining indicators the same
way: synthetic degradation seeded in PostgreSQL / marker files, evaluated by the periodic
check, delivered through the fake control channel.

Per indicator: a brief blip never alerts, sustained degradation alerts exactly once with the
fixed content-free text, recovery sends exactly one recovery message and then stays quiet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import AuditLogEntry, ControlTurn
from assistant.actions.models import Action
from assistant.actions.states import ActionKind, ActionOrigin, ActionState
from assistant.core.jobs import OWNER_REQUEST_TASK, owner_control_lock
from assistant.db.uow import transaction
from assistant.inbox.backup_check import UPLOAD_MARKER_NAME
from assistant.inbox.notify import ALERT_TEXT, AlertKind, OwnerAlerts
from assistant.observability.service_monitoring import (
    ServiceMonitoringOutcome,
    ServiceMonitoringThresholds,
    check_service_monitoring,
)
from tests.fakes.channel import FakeChannel
from tests.fakes.clock import FakeClock
from tests.integration.actions_env import seed_chat
from tests.integration.test_service_monitoring import (
    _insert_llm_job,
    clock,  # noqa: F401  (fixture)
    create_backup_dir,
    factory,  # noqa: F401  (fixture)
    setup_owner,
)

pytestmark = pytest.mark.integration

CANARY = "CANARY-SECRET-MESSAGE-42"


@dataclass(frozen=True)
class _Ctx:
    factory: async_sessionmaker[AsyncSession]
    clock: FakeClock
    account_id: int
    backup_dir: Path


def _marker(ctx: _Ctx, name: str, when: datetime) -> None:
    (ctx.backup_dir / name).write_text(when.strftime("%Y%m%dT%H%M%SZ\n"), encoding="utf-8")


async def _seed_owner_request_queued(ctx: _Ctx) -> None:
    async with transaction(ctx.factory) as uow:
        owner_turn = ControlTurn(account_id=ctx.account_id, role="owner", text=CANARY, refs={})
        owner_turn.created_at = ctx.clock.now() - timedelta(minutes=10)
        uow.session.add(owner_turn)
        await uow.session.flush()
        # Answered, so only the queue-age indicator can fire.
        uow.session.add(
            ControlTurn(account_id=ctx.account_id, role="assistant", text="Ok", refs={})
        )
        turn_id = owner_turn.id
    async with ctx.factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO procrastinate.procrastinate_jobs "
                "(queue_name, task_name, priority, lock, args, status) "
                "VALUES ('llm', :task, 0, :lock, CAST(:args AS jsonb), "
                "CAST('todo' AS procrastinate.procrastinate_job_status))"
            ),
            {
                "task": OWNER_REQUEST_TASK,
                "lock": owner_control_lock(ctx.account_id),
                "args": f'{{"turn_id": {turn_id}}}',
            },
        )


async def _finish_jobs(ctx: _Ctx) -> None:
    async with ctx.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE procrastinate.procrastinate_jobs "
                "SET status = CAST('succeeded' AS procrastinate.procrastinate_job_status)"
            )
        )


async def _seed_llm_queued(ctx: _Ctx) -> None:
    await _insert_llm_job(
        ctx.factory,
        task="replies.draft",
        status="todo",
        event_type="deferred",
        event_at=ctx.clock.now() - timedelta(hours=1),
    )


async def _seed_llm_failed(ctx: _Ctx) -> None:
    for minutes_ago in (5, 10):
        await _insert_llm_job(
            ctx.factory,
            task="inbox.triage",
            status="failed",
            event_type="failed",
            event_at=ctx.clock.now() - timedelta(minutes=minutes_ago),
        )


async def _seed_delivery_uncertain(ctx: _Ctx) -> None:
    async with transaction(ctx.factory) as uow:
        chat = await seed_chat(uow.session, ctx.clock.now())
        uow.session.add(
            Action(
                account_id=chat.account_id,
                conversation_id=chat.conversation_id,
                kind=ActionKind.SEND_MESSAGE.value,
                origin=ActionOrigin.OWNER_DICTATED.value,
                state=ActionState.DELIVERY_UNCERTAIN.value,
                attempt_started_at=ctx.clock.now() - timedelta(minutes=30),
            )
        )
        # seed_chat creates its own account; keep a single reachable owner.
        await uow.session.execute(
            text("UPDATE app.accounts SET control_chat_id = NULL WHERE id != :id"),
            {"id": ctx.account_id},
        )


async def _resolve_delivery_uncertain(ctx: _Ctx) -> None:
    async with transaction(ctx.factory) as uow:
        await uow.session.execute(
            text("UPDATE app.actions SET state = 'succeeded' WHERE state = 'delivery_uncertain'")
        )


async def _seed_unresolved_presentation(ctx: _Ctx) -> None:
    async with transaction(ctx.factory) as uow:
        uow.session.add(
            AuditLogEntry(
                account_id=ctx.account_id,
                at=ctx.clock.now() - timedelta(minutes=30),
                actor="system",
                event="approval_card_presentation_claimed",
                subject_type="action",
                subject_id=999,
                details={"version": 1},
            )
        )


async def _resolve_presentation(ctx: _Ctx) -> None:
    async with transaction(ctx.factory) as uow:
        claim_ids = (
            (
                await uow.session.execute(
                    text("SELECT id FROM app.audit_log WHERE event = :e"),
                    {"e": "approval_card_presentation_claimed"},
                )
            )
            .scalars()
            .all()
        )
        for claim_id in claim_ids:
            uow.session.add(
                AuditLogEntry(
                    account_id=ctx.account_id,
                    at=ctx.clock.now(),
                    actor="system",
                    event="approval_card_presentation_resolved",
                    subject_type="action",
                    subject_id=999,
                    details={"version": 1, "claim_id": claim_id, "outcome": "shown"},
                )
            )


async def _seed_backup_stale(ctx: _Ctx) -> None:
    _marker(ctx, "last_success", ctx.clock.now() - timedelta(hours=30))


async def _heal_backup(ctx: _Ctx) -> None:
    _marker(ctx, "last_success", ctx.clock.now())


async def _seed_upload_stale(ctx: _Ctx) -> None:
    _marker(ctx, UPLOAD_MARKER_NAME, ctx.clock.now() - timedelta(hours=30))


async def _heal_upload(ctx: _Ctx) -> None:
    _marker(ctx, UPLOAD_MARKER_NAME, ctx.clock.now())


async def _seed_journal_failed(ctx: _Ctx) -> None:
    (ctx.backup_dir / "journal_status").write_text("failed\n", encoding="utf-8")


async def _heal_journal(ctx: _Ctx) -> None:
    (ctx.backup_dir / "journal_status").write_text("acknowledged\n", encoding="utf-8")


_SCENARIOS = {
    "owner_request_queued": (_seed_owner_request_queued, _finish_jobs),
    "llm_jobs_queued": (_seed_llm_queued, _finish_jobs),
    "llm_jobs_failed": (_seed_llm_failed, _finish_jobs),
    "actions_delivery_uncertain": (_seed_delivery_uncertain, _resolve_delivery_uncertain),
    "card_presentations_unresolved": (_seed_unresolved_presentation, _resolve_presentation),
    "backup_stale": (_seed_backup_stale, _heal_backup),
    "backup_upload_stale": (_seed_upload_stale, _heal_upload),
    "privacy_journal_failed": (_seed_journal_failed, _heal_journal),
}


@pytest.mark.parametrize("reason", sorted(_SCENARIOS))
async def test_indicator_blip_then_sustained_alert_then_recovery(
    factory: async_sessionmaker[AsyncSession],  # noqa: F811
    clock: FakeClock,  # noqa: F811
    tmp_path: Path,
    reason: str,
) -> None:
    account_id = await setup_owner(factory, clock)
    ctx = _Ctx(factory, clock, account_id, create_backup_dir(tmp_path, clock.now()))
    seed, heal = _SCENARIOS[reason]
    channel = FakeChannel()
    alerts = OwnerAlerts(factory, channel, clock)
    thresholds = ServiceMonitoringThresholds(
        consecutive_checks=3, failed_reviews_threshold=2, action_unresolved_seconds=900
    )

    async def check() -> ServiceMonitoringOutcome:
        return await check_service_monitoring(
            factory,
            clock,
            alerts,
            thresholds=thresholds,
            backup_status_path=ctx.backup_dir,
            backup_upload_required=True,
        )

    # Healthy baseline is silent.
    assert not (await check()).indicators.degraded
    assert channel.sends == []

    # A brief blip (one degraded check, then healthy) neither alerts nor "recovers".
    await seed(ctx)
    clock.advance(minutes=1)
    blip = await check()
    assert blip.indicators.degradation_reasons == (reason,)
    assert not blip.alerted
    await heal(ctx)
    clock.advance(minutes=1)
    after_blip = await check()
    assert not after_blip.indicators.degraded
    assert not after_blip.recovered
    assert after_blip.consecutive_checks == 0
    assert channel.sends == []

    # Sustained degradation: exactly one alert, on the third consecutive check.
    await seed(ctx)
    for expected_alert in (False, False, True):
        clock.advance(minutes=1)
        outcome = await check()
        assert outcome.indicators.degradation_reasons == (reason,)
        assert outcome.alerted is expected_alert
    for _ in range(3):
        clock.advance(minutes=1)
        assert not (await check()).alerted
    assert len(channel.sends) == 1
    assert ALERT_TEXT[AlertKind.SERVICE_DEGRADED] in channel.sends[0]
    # Content-free: nothing from the seeded rows or the indicator name leaks into the alert.
    assert CANARY not in channel.sends[0]
    assert reason not in channel.sends[0]

    # Recovery sends exactly one recovery message, then stays quiet.
    await heal(ctx)
    clock.advance(minutes=1)
    recovered = await check()
    assert not recovered.indicators.degraded
    assert recovered.recovered
    assert len(channel.sends) == 2
    assert ALERT_TEXT[AlertKind.SERVICE_RECOVERED] in channel.sends[1]
    assert CANARY not in channel.sends[1]
    clock.advance(minutes=1)
    assert not (await check()).recovered
    assert len(channel.sends) == 2
