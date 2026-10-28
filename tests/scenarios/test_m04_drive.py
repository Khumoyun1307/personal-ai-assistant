"""The scenario harness must work before feature failures can be expected."""

import pytest
from sqlalchemy import select

from assistant.accounts.models import Account
from assistant.actions.models import Action
from assistant.db.uow import transaction
from tests.scenarios.p2_drive import Scenario, advance, run_queued

pytestmark = pytest.mark.integration


async def test_m04_setup_uses_a_real_inbox_and_records_synthetic_source(p2: Scenario) -> None:
    async with p2.env.factory() as session:
        account = await session.get(Account, p2.account_id)
        assert account is not None
        assert account.status == "active"
    assert p2.fake.requests == []
    assert await p2.rows() == []
    assert p2.business_sends() == []


async def test_job_driver_invokes_the_registered_body_once_and_leaves_future_jobs(
    p2: Scenario,
) -> None:
    seen = []

    @p2.app.task(name="qa.synthetic", queue="system")
    async def synthetic(value: int) -> None:
        seen.append(value)
        async with transaction(p2.env.factory) as uow:
            await uow.session.execute(select(Account.id).where(Account.id == p2.account_id))

    scheduler = p2.env.scheduler
    assert scheduler is not None
    from datetime import timedelta

    await scheduler.defer("qa.synthetic", args={"value": 1})
    await scheduler.defer("qa.synthetic", args={"value": 2})
    await scheduler.defer(
        "qa.synthetic", args={"value": 3}, schedule_at=p2.env.clock.now() + timedelta(seconds=60)
    )
    assert await run_queued(p2, "qa.synthetic", limit=1) == 1
    assert seen == [1]
    assert await run_queued(p2, "qa.synthetic") == 1
    assert seen == [1, 2]
    await advance(p2, 60)
    assert await run_queued(p2, "qa.synthetic") == 1
    assert seen == [1, 2, 3]
    assert await run_queued(p2, "qa.synthetic") == 0


async def test_seed_helpers_commit_real_actions_without_sending(p2: Scenario) -> None:
    row = await p2.seed()
    async with p2.env.factory() as session:
        stored = await session.get(Action, row.id)
        assert stored is not None
        assert stored.account_id == p2.account_id
        assert stored.state == "awaiting_approval"
        assert stored.base_message_id is not None
    assert p2.business_sends() == []
