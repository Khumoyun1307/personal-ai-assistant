"""The driver itself must work before any missing-feature marker is added."""

import pytest

from assistant.accounts.models import Account
from assistant.db.uow import transaction
from tests.scenarios.m05_drive import M05Scenario, advance, run_queued

pytestmark = pytest.mark.integration


async def test_m05_driver_uses_real_inbox_and_no_external_clients(m05: M05Scenario) -> None:
    [account] = await m05.rows(Account)
    assert account.status == "active"
    assert m05.fake.requests == []
    assert m05.settings.bot_token is None
    assert m05.settings.openai_api_key is None


async def test_m05_driver_respects_due_time_and_invokes_real_registration(m05: M05Scenario) -> None:
    from datetime import timedelta

    seen = []

    @m05.app.task(name="qa.m05_driver", queue="system")
    async def body(value: int) -> None:
        async with transaction(m05.env.factory):
            seen.append(value)

    assert m05.env.scheduler is not None
    await m05.env.scheduler.defer(
        "qa.m05_driver", args={"value": 1}, schedule_at=m05.env.clock.now() + timedelta(seconds=1)
    )
    assert await run_queued(m05, "qa.m05_driver") == 0
    await advance(m05, 1)
    assert await run_queued(m05, "qa.m05_driver") == 1
    assert await run_queued(m05, "qa.m05_driver") == 0
    assert seen == [1]
