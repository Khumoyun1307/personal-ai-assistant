"""S05.3b: `automation.fire` and the schedule reconciler job, over Postgres
and the real jobs table (S05.3b §3.2 step 4, §3.3).

Required tests, findable by name: `test_fire_twice_marks_fired_once`,
`test_crash_after_delivery_before_the_status_update_sends_no_second_notification`,
`test_a_fire_for_a_paused_automation_does_nothing`.

Synthetic data only; FakeChannel stands in for the control chat.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update

from assistant.automation.models import Automation, AutomationOccurrence
from assistant.automation.schedules import FireAction, Note
from assistant.core.jobs import DIGEST_TASK, FIRE_TASK
from assistant.inbox.digest import DigestDelivery
from assistant.inbox.models import Digest
from assistant.jobs.tasks_automation import ScheduleJobs
from assistant.telegram.control.digest import render_digest
from tests.fakes.channel import FakeChannel
from tests.integration.business_env import Env
from tests.integration.p2_factories import seed_account, seed_automation

pytestmark = pytest.mark.integration

#: 21:00 in Tashkent on 2026-10-18 (the env clock starts at 12:00 UTC that day).
EVENING = datetime(2026, 10, 18, 16, 0, tzinfo=UTC)
EPOCH = int(EVENING.timestamp())


def jobs(env: Env, channel: FakeChannel | None = None, *, digest: bool = True) -> ScheduleJobs:
    assert env.scheduler is not None
    return ScheduleJobs(
        session_factory=env.factory,
        clock=env.clock,
        scheduler=env.scheduler,
        digests=DigestDelivery(
            session_factory=env.factory,
            channel=channel or FakeChannel(),
            clock=env.clock,
            render=render_digest,
        ),
        digest_enabled=digest,
    )


async def queued(env: Env, task: str) -> list[dict[str, int]]:
    async with env.factory() as session:
        rows = await session.execute(
            text(
                "SELECT args FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = :t AND status = 'todo' ORDER BY id"
            ),
            {"t": task},
        )
        return [dict(args) for (args,) in rows.all()]


async def take(env: Env, task: str) -> None:
    """A worker picks the queued jobs up (`doing`), freeing their queueing locks."""
    async with env.factory() as session, session.begin():
        await session.execute(
            text(
                "UPDATE procrastinate.procrastinate_jobs SET status = 'doing' "
                "WHERE task_name = :t AND status = 'todo'"
            ),
            {"t": task},
        )


async def occurrence(env: Env) -> AutomationOccurrence:
    async with env.factory() as session:
        return (await session.execute(select(AutomationOccurrence))).scalar_one()


async def due_digest(env: Env, **automation_fields: Any) -> tuple[int, int]:
    """An account whose digest automation is due at EVENING, claimed by the
    reconciler job: (account id, automation id)."""
    async with env.factory() as session, session.begin():
        account = await seed_account(session, timezone="Asia/Tashkent")
        automation = await seed_automation(
            session,
            account_id=account.id,
            kind="digest",
            spec={
                "schema_version": 1,
                "max_lateness_minutes": 120,
                "dtstart_local": "2026-10-17T21:00",
                "kind": "digest",
            },
            rrule="FREQ=DAILY",
            timezone="Asia/Tashkent",
            next_run_at=EVENING,
            **automation_fields,
        )
        ids = (account.id, automation.id)
    env.clock.set(EVENING + timedelta(seconds=30))
    assert await jobs(env).reconcile_schedules() == 1
    assert await queued(env, FIRE_TASK) == [{"automation_id": ids[1], "occurrence_at": EPOCH}]
    return ids


async def test_fire_twice_marks_fired_once(env: Env) -> None:
    account_id, automation_id = await due_digest(env)
    handlers = jobs(env)
    first = await handlers.fire(automation_id, EPOCH)
    second = await handlers.fire(automation_id, EPOCH)
    assert (first.action, second.action) == (FireAction.DIGEST, FireAction.NONE)
    assert await queued(env, DIGEST_TASK) == [{"account_id": account_id, "period_end": EPOCH}]
    row = await occurrence(env)
    assert (row.status, row.fired_at) == ("fired", env.clock.now())


async def test_crash_after_delivery_before_the_status_update_sends_no_second_notification(
    env: Env,
) -> None:
    """The digest went out, then the job died before `claimed → fired`: the
    re-run defers `digest.build` again, which finds its `digests` row and
    sends nothing; the occurrence is marked fired."""
    account_id, automation_id = await due_digest(env)
    channel = FakeChannel()
    handlers = jobs(env, channel)
    await handlers.fire(automation_id, EPOCH)
    await take(env, DIGEST_TASK)
    assert (await handlers.build_digest(account_id, EPOCH)).status == "sent"
    # The crash: the occurrence never left `claimed`.
    async with env.factory() as session, session.begin():
        await session.execute(update(AutomationOccurrence).values(status="claimed", fired_at=None))

    assert (await handlers.fire(automation_id, EPOCH)).action is FireAction.DIGEST
    assert await queued(env, DIGEST_TASK) == [{"account_id": account_id, "period_end": EPOCH}]
    assert (await handlers.build_digest(account_id, EPOCH)).status == "duplicate"
    assert len(channel.sends) == 1
    assert (await occurrence(env)).status == "fired"


async def test_a_fire_for_a_paused_automation_does_nothing(env: Env) -> None:
    _, automation_id = await due_digest(env)
    async with env.factory() as session, session.begin():
        await session.execute(
            update(Automation).where(Automation.id == automation_id).values(status="paused")
        )
    plan = await jobs(env).fire(automation_id, EPOCH)
    assert (plan.action, plan.note) == (FireAction.SKIPPED, Note.INACTIVE)
    assert await queued(env, DIGEST_TASK) == []
    row = await occurrence(env)
    assert (row.status, row.note_code) == ("skipped", "inactive")


async def test_a_digest_fire_with_the_flag_off_is_skipped(env: Env) -> None:
    _, automation_id = await due_digest(env)
    plan = await jobs(env, digest=False).fire(automation_id, EPOCH)
    assert (plan.action, plan.note) == (FireAction.SKIPPED, Note.DISABLED)
    assert await queued(env, DIGEST_TASK) == []
    assert (await occurrence(env)).note_code == "disabled"


async def test_a_kind_without_a_handler_is_skipped_with_a_note(env: Env) -> None:
    async with env.factory() as session, session.begin():
        account = await seed_account(session, timezone="UTC")
        reminder = await seed_automation(
            session,
            account_id=account.id,
            kind="reminder",
            spec={
                "schema_version": 1,
                "max_lateness_minutes": 30,
                "dtstart_local": "2026-10-17T16:00",
            },
            rrule="FREQ=DAILY",
            timezone="UTC",
            next_run_at=EVENING,
        )
        reminder_id = reminder.id
    env.clock.set(EVENING + timedelta(seconds=30))
    handlers = jobs(env)
    await handlers.reconcile_schedules()
    plan = await handlers.fire(reminder_id, EPOCH)
    assert (plan.action, plan.note) == (FireAction.SKIPPED, Note.NO_HANDLER)
    assert (await occurrence(env)).status == "skipped"


async def test_the_reconciler_redefers_a_lost_fire_once_its_grace_passed(env: Env) -> None:
    _, automation_id = await due_digest(env)
    # The defer was lost (a crash between the commit and the defer).
    async with env.factory() as session, session.begin():
        await session.execute(text("DELETE FROM procrastinate.procrastinate_jobs"))
    handlers = jobs(env)
    env.clock.advance(minutes=1)
    assert await handlers.reconcile_schedules() == 0  # within the grace
    env.clock.advance(minutes=2)
    assert await handlers.reconcile_schedules() == 1
    assert await queued(env, FIRE_TASK) == [
        {"automation_id": automation_id, "occurrence_at": EPOCH}
    ]
    # A queued job is not deferred again.
    env.clock.advance(minutes=1)
    await handlers.reconcile_schedules()
    assert len(await queued(env, FIRE_TASK)) == 1
    # Once fired, nothing is re-deferred.
    await take(env, FIRE_TASK)
    await handlers.fire(automation_id, EPOCH)
    env.clock.advance(minutes=5)
    assert await handlers.reconcile_schedules() == 0


async def lose_the_fire_job(env: Env) -> None:
    """A crash between the reconciler's commit and its defer."""
    async with env.factory() as session, session.begin():
        await session.execute(text("DELETE FROM procrastinate.procrastinate_jobs"))


async def no_digests(env: Env) -> bool:
    async with env.factory() as session:
        return (await session.scalar(select(Digest.id).limit(1))) is None


# ------------------------------------------------ deadlines at execution (QA B2)
async def test_an_on_time_claim_that_waits_past_its_lateness_never_fires(env: Env) -> None:
    """AGENTS §13: queued work re-checks its conditions when it runs. A claim
    made on time whose fire job runs only after the rule's 120 minutes is
    settled `skipped_obsolete`; no digest is built."""
    _, automation_id = await due_digest(env)
    env.clock.set(EVENING + timedelta(hours=3))
    plan = await jobs(env).fire(automation_id, EPOCH)
    assert (plan.action, plan.note) == (FireAction.SKIPPED, "skipped_obsolete")
    assert await queued(env, DIGEST_TASK) == []
    row = await occurrence(env)
    assert (row.status, row.note_code, row.fired_at) == ("skipped", "skipped_obsolete", None)


async def test_an_on_time_claim_that_waits_within_its_lateness_still_fires(env: Env) -> None:
    account_id, automation_id = await due_digest(env)
    env.clock.set(EVENING + timedelta(minutes=119))
    assert (await jobs(env).fire(automation_id, EPOCH)).action is FireAction.DIGEST
    assert await queued(env, DIGEST_TASK) == [{"account_id": account_id, "period_end": EPOCH}]
    assert (await occurrence(env)).status == "fired"


async def test_an_automation_that_expires_while_its_claim_waits_never_fires(env: Env) -> None:
    _, automation_id = await due_digest(env, expires_at=EVENING + timedelta(minutes=10))
    env.clock.set(EVENING + timedelta(minutes=15))
    plan = await jobs(env).fire(automation_id, EPOCH)
    assert (plan.action, plan.note) == (FireAction.SKIPPED, "expired")
    assert await queued(env, DIGEST_TASK) == []
    row = await occurrence(env)
    assert (row.status, row.note_code) == ("skipped", "expired")


async def test_a_digest_build_that_waits_past_its_lateness_sends_nothing(env: Env) -> None:
    """The fire ran on time, but `digest.build` waited in its queue past the
    rule's lateness: nothing is sent, no `digests` row claims the period (the
    next digest covers it), and the occurrence is settled obsolete."""
    account_id, automation_id = await due_digest(env)
    channel = FakeChannel()
    handlers = jobs(env, channel)
    assert (await handlers.fire(automation_id, EPOCH)).action is FireAction.DIGEST
    env.clock.set(EVENING + timedelta(hours=3))
    result = await handlers.build_digest(account_id, EPOCH)
    assert (result.status, result.reason) == ("skipped", "obsolete")
    assert channel.sends == []
    assert await no_digests(env)
    row = await occurrence(env)
    assert (row.status, row.note_code, row.fired_at) == ("skipped", "skipped_obsolete", None)


# ------------------------------------------------ lost fires follow the rule (QA B1)
async def test_a_lost_fire_is_recovered_while_its_rule_still_allows_it(env: Env) -> None:
    """Recovery follows the rule's deadline (the occurrence + 120 minutes for
    the digest), not a fixed hour: a defer lost in a 70-minute outage is
    re-deferred once, and the digest still goes out, late with a note."""
    account_id, automation_id = await due_digest(env)
    await lose_the_fire_job(env)
    channel = FakeChannel()
    handlers = jobs(env, channel)
    env.clock.advance(minutes=70)
    assert await handlers.reconcile_schedules() == 1
    assert await queued(env, FIRE_TASK) == [
        {"automation_id": automation_id, "occurrence_at": EPOCH}
    ]
    env.clock.advance(minutes=1)
    assert await handlers.reconcile_schedules() == 0  # queued: not deferred again
    assert len(await queued(env, FIRE_TASK)) == 1

    await take(env, FIRE_TASK)
    assert (await handlers.fire(automation_id, EPOCH)).action is FireAction.DIGEST
    await take(env, DIGEST_TASK)
    assert (await handlers.build_digest(account_id, EPOCH)).status == "sent"
    [sent] = channel.sends
    assert "⏰ Late: this digest was due at 18 Oct 21:00." in sent
    assert (await occurrence(env)).status == "fired"
    env.clock.advance(minutes=5)
    assert await handlers.reconcile_schedules() == 0


async def test_a_lost_fire_past_its_rule_deadline_is_settled_obsolete(env: Env) -> None:
    """A claim whose rule no longer allows it never stays `claimed` forever:
    the reconciler records it `skipped_obsolete` once, and defers nothing."""
    _, automation_id = await due_digest(env)
    await lose_the_fire_job(env)
    handlers = jobs(env)
    env.clock.set(EVENING + timedelta(minutes=121))
    assert await handlers.reconcile_schedules() == 0
    assert await queued(env, FIRE_TASK) == []
    row = await occurrence(env)
    assert (row.status, row.note_code) == ("skipped", "skipped_obsolete")
    env.clock.advance(minutes=1)
    assert await handlers.reconcile_schedules() == 0
    assert (await jobs(env).fire(automation_id, EPOCH)).action is FireAction.NONE
