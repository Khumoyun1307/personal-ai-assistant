"""After a restore: the gate and the post-restore transaction (ARCHITECTURE §15.6).

A restored database must not run until `post-restore` has paused autonomy,
dropped queued jobs, recorded the possible gap and cleared the marker — and
the owner must be told on the next start.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.accounts.models import Account, AuditLogEntry
from assistant.automation.models import Automation, AutomationOccurrence
from assistant.conversations.models import CoverageGap
from assistant.db.models import KEY_RESTORE_PENDING, SystemState
from assistant.db.uow import transaction
from assistant.inbox.backup_check import deliver_restore_notice
from assistant.inbox.notify import OwnerAlerts
from assistant.privacy.post_restore import (
    KEY_RESTORE_NOTICE,
    RestorePendingError,
    assert_not_restore_pending,
    post_restore,
)
from tests.fakes.channel import FakeChannel
from tests.integration.business_env import OWNER, Env, connect, deliver, private_message
from tests.integration.p2_factories import seed_account, seed_automation

pytestmark = pytest.mark.integration


async def _mark_restored(env: Env) -> None:
    async with transaction(env.factory) as uow:
        uow.session.add(SystemState(key=KEY_RESTORE_PENDING, value={"file": "synthetic.dump.age"}))


async def test_post_restore_cancels_pending_actions_and_marks_executing_uncertain(env: Env) -> None:
    from assistant.actions.models import Action
    from tests.integration.test_action_lifecycle_bulk import seed_actions

    account_id, _, ids = await seed_actions(env)
    report = await post_restore(
        env.factory, env.clock, backup_taken_at=env.clock.now() - timedelta(hours=1)
    )
    assert report.actions_cancelled == 4
    assert report.actions_uncertain == 1
    async with env.factory() as session:
        rows = {row.id: row for row in await session.scalars(select(Action))}
        for state in ("proposed", "awaiting_approval", "approved", "scheduled"):
            action = rows[ids[state]]
            assert action.state == "cancelled"
            assert action.state_reason == "restored_from_backup"
            assert action.approval_token is None
        assert rows[ids["executing"]].state == "delivery_uncertain"
        assert rows[ids["succeeded"]].state == "succeeded"
        notice = await session.get(SystemState, KEY_RESTORE_NOTICE)
        assert notice is not None
        assert len(notice.value["actions"][str(account_id)]["cancelled"]) == 4
        assert notice.value["actions"][str(account_id)]["uncertain"][0]["id"] == ids["executing"]


async def test_restore_notice_lists_only_its_accounts_action_ids_and_local_times(env: Env) -> None:
    from tests.integration.test_action_lifecycle_bulk import seed_actions

    account_id, _, ids = await seed_actions(env)
    other_account, _, other_ids = await seed_actions(env, user_id=7000080002)
    async with env.factory() as session, session.begin():
        first = await session.get(Account, account_id)
        second = await session.get(Account, other_account)
        assert first is not None
        assert second is not None
        first.control_chat_id = 7000080001
        first.timezone = "America/Phoenix"
        second.control_chat_id = 7000080002
        second.timezone = "synthetic-invalid-timezone"
    await post_restore(env.factory, env.clock, backup_taken_at=env.clock.now() - timedelta(hours=1))
    channel = FakeChannel()
    assert await deliver_restore_notice(env.factory, OwnerAlerts(env.factory, channel, env.clock))
    assert set(channel.recipients) == {account_id, other_account}
    sent = dict(zip(channel.recipients, channel.sends, strict=True))
    assert "2026-10-18 05:00 MST" in sent[account_id]
    assert "2026-10-18 12:00 UTC" in sent[other_account]
    for owner, own_ids, foreign_ids in (
        (account_id, ids, other_ids),
        (other_account, other_ids, ids),
    ):
        assert f"#{own_ids['executing']} (" in sent[owner]
        assert f"#{own_ids['awaiting_approval']} (" in sent[owner]
        assert all(f"#{action_id} (" not in sent[owner] for action_id in foreign_ids.values())
        assert "synthetic action text" not in sent[owner]


async def test_a_restored_database_refuses_to_run_until_post_restore(env: Env) -> None:
    await _mark_restored(env)
    async with env.factory() as session:
        with pytest.raises(RestorePendingError, match="post-restore"):
            await assert_not_restore_pending(session)


async def test_post_restore_pauses_drops_jobs_records_the_gap_and_clears_the_marker(
    env: Env,
) -> None:
    await connect(env)  # leaves a queued connections.notify_owner job behind
    await _mark_restored(env)
    async with env.factory() as session:
        queued_before = (
            await session.execute(
                text("SELECT count(*) FROM procrastinate.procrastinate_jobs WHERE status = 'todo'")
            )
        ).scalar_one()
    assert queued_before >= 1
    taken_at = env.clock.now() - timedelta(hours=5)

    report = await post_restore(env.factory, env.clock, backup_taken_at=taken_at)

    assert report.accounts_paused == 1
    assert report.jobs_deleted == queued_before
    assert report.gaps_recorded == 1
    async with env.factory() as session:
        account = (
            await session.execute(select(Account).where(Account.telegram_user_id == OWNER))
        ).scalar_one()
        assert account.autonomy_paused is True
        [gap] = (await session.execute(select(CoverageGap))).scalars().all()
        assert (gap.kind, gap.certainty) == ("restore", "possible")
        assert (gap.at_risk_from, gap.at_risk_to) == (taken_at, env.clock.now())
        assert await session.get(SystemState, KEY_RESTORE_PENDING) is None
        notice = await session.get(SystemState, KEY_RESTORE_NOTICE)
        assert notice is not None
        assert notice.value["backup_taken_at"] == taken_at.isoformat()
        audits = (
            (
                await session.execute(
                    select(AuditLogEntry).where(AuditLogEntry.event == "restored_from_backup")
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 1
        queued_after = (
            await session.execute(
                text("SELECT count(*) FROM procrastinate.procrastinate_jobs WHERE status = 'todo'")
            )
        ).scalar_one()
    assert queued_after == 0
    # The gate is open again.
    async with env.factory() as session:
        await assert_not_restore_pending(session)


async def test_the_restore_notice_is_delivered_once_on_the_next_run(env: Env) -> None:
    await connect(env)
    taken_at = datetime(2026, 10, 18, 2, 0, tzinfo=UTC)
    await post_restore(env.factory, env.clock, backup_taken_at=taken_at)
    channel = FakeChannel()
    alerts = OwnerAlerts(env.factory, channel, env.clock)

    assert await deliver_restore_notice(env.factory, alerts) is True
    assert await deliver_restore_notice(env.factory, alerts) is False

    assert len(channel.sends) == 1
    assert "restored from a backup taken 2026-10-18T02:00:00+00:00" in channel.sends[0]
    async with env.factory() as session:
        assert await session.get(SystemState, KEY_RESTORE_NOTICE) is None
    # /status shows the possible gap.
    await deliver(env, private_message(OWNER, "/status"))
    assert "Latest gap (possible)" in env.sender.texts[-1]


async def test_post_restore_skips_past_occurrences_with_a_note_and_reschedules(env: Env) -> None:
    """Step 4 (S05.3b §3.7): nothing from a rewound backup fires by itself.
    Missed occurrences of active schedules are recorded `skipped_restore`
    (at most ten per automation) and the next run is computed from now; a
    `claimed` occurrence, whose job the restore dropped, is skipped too.
    Paused automations and event-only monitors are untouched. The owner's
    notice says how many (counts only)."""
    now = env.clock.now()  # 2026-10-18 12:00 UTC
    nine = now.replace(hour=9, minute=0)
    tomorrow = nine + timedelta(days=1)
    daily: dict[str, Any] = {
        "kind": "reminder",
        "spec": {
            "schema_version": 1,
            "max_lateness_minutes": 60,
            "dtstart_local": "2026-09-01T09:00",
        },
        "rrule": "FREQ=DAILY",
        "timezone": "UTC",
    }
    async with env.factory() as session, session.begin():
        account = await seed_account(session)
        account_id = account.id
        behind = await seed_automation(
            session, account_id=account_id, next_run_at=nine - timedelta(days=3), **daily
        )
        far_behind = await seed_automation(
            session, account_id=account_id, next_run_at=nine - timedelta(days=15), **daily
        )
        running = await seed_automation(
            session, account_id=account_id, next_run_at=tomorrow, **daily
        )
        session.add(
            AutomationOccurrence(
                account_id=account_id,
                automation_id=running.id,
                occurrence_at=nine,
                status="claimed",
                created_at=nine,
            )
        )
        paused = await seed_automation(
            session,
            account_id=account_id,
            status="paused",
            next_run_at=nine - timedelta(days=2),
            **daily,
        )
        monitor = await seed_automation(
            session, account_id=account_id, next_run_at=nine - timedelta(days=1)
        )
        ids = (behind.id, far_behind.id, running.id, paused.id, monitor.id)

    report = await post_restore(env.factory, env.clock, backup_taken_at=now - timedelta(hours=5))

    async with env.factory() as session:
        automations = {
            row.id: row
            for row in await session.scalars(
                select(Automation).where(Automation.account_id == account_id)
            )
        }
        ledger = list(
            await session.scalars(
                select(AutomationOccurrence).order_by(
                    AutomationOccurrence.automation_id, AutomationOccurrence.occurrence_at
                )
            )
        )
        notice = await session.get(SystemState, KEY_RESTORE_NOTICE)
    rows = {
        automation_id: [
            (o.occurrence_at, o.status, o.note_code)
            for o in ledger
            if o.automation_id == automation_id
        ]
        for automation_id in ids
    }
    behind_id, far_id, running_id, paused_id, monitor_id = ids
    # Three missed days and this morning's 09:00, each with the note.
    assert rows[behind_id] == [
        (nine - timedelta(days=days), "skipped", "skipped_restore") for days in (3, 2, 1, 0)
    ]
    # Fifteen days behind: ten rows, then the jump.
    assert len(rows[far_id]) == 10
    assert rows[far_id][0][0] == nine - timedelta(days=15)
    assert {(status, note) for _, status, note in rows[far_id]} == {("skipped", "skipped_restore")}
    assert rows[running_id] == [(nine, "skipped", "skipped_restore")]
    assert rows[paused_id] == rows[monitor_id] == []
    for automation_id in (behind_id, far_id, running_id):
        assert automations[automation_id].next_run_at == tomorrow
        assert automations[automation_id].status == "active"
    assert automations[paused_id].next_run_at == nine - timedelta(days=2)
    assert (automations[monitor_id].status, automations[monitor_id].next_run_at) == (
        "active",
        nine - timedelta(days=1),
    )
    assert report.schedules_skipped == 15
    assert notice is not None
    assert notice.value["schedules_skipped"] == {str(account_id): 15}

    channel = FakeChannel()
    assert await deliver_restore_notice(env.factory, OwnerAlerts(env.factory, channel, env.clock))
    [sent] = channel.sends
    assert "15 scheduled items were skipped because of the restore." in sent


async def test_post_restore_rejects_a_future_timestamp(env: Env) -> None:
    with pytest.raises(ValueError, match="future"):
        await post_restore(
            env.factory, env.clock, backup_taken_at=env.clock.now() + timedelta(minutes=1)
        )
