"""NFR-09: restored approval is never fresh authorization to send."""

from datetime import timedelta

import pytest

from assistant.db.models import SystemState
from assistant.privacy.post_restore import KEY_RESTORE_NOTICE, post_restore
from tests.scenarios.p2_drive import Scenario, run_queued

pytestmark = pytest.mark.integration


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S04.4")
async def test_a_restored_database_with_an_approved_action_sends_nothing(p2: Scenario) -> None:
    approved = await p2.seed(state="approved")
    executing = await p2.seed(state="executing", attempt_id="synthetic-lost-attempt")
    await post_restore(
        p2.env.factory, p2.env.clock, backup_taken_at=p2.env.clock.now() - timedelta(hours=1)
    )
    assert (await p2.action(approved.id)).state == "cancelled"
    assert (await p2.action(executing.id)).state == "delivery_uncertain"
    async with p2.env.factory() as session:
        notice = await session.get(SystemState, KEY_RESTORE_NOTICE)
        assert notice is not None
        account_notice = notice.value["actions"][str(p2.account_id)]
    assert [r["id"] for r in account_notice["cancelled"]] == [approved.id]
    assert [r["id"] for r in account_notice["uncertain"]] == [executing.id]
    assert approved.text not in str(account_notice)
    await p2.handlers.reconcile_pending()
    await run_queued(p2, "actions.execute")
    await p2.execute(approved.id)
    await p2.execute(executing.id)
    assert p2.business_sends() == []
