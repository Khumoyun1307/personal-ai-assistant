"""After a restore: the gate and the post-restore transaction (ARCHITECTURE §15.6).

A restored database must not run until `post-restore` has paused autonomy,
dropped queued jobs, recorded the possible gap and cleared the marker — and
the owner must be told on the next start.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from assistant.accounts.models import Account, AuditLogEntry
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


async def test_post_restore_rejects_a_future_timestamp(env: Env) -> None:
    with pytest.raises(ValueError, match="future"):
        await post_restore(
            env.factory, env.clock, backup_taken_at=env.clock.now() + timedelta(minutes=1)
        )
