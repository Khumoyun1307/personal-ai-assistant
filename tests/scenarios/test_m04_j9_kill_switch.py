"""J9: pause is immediate; a model can request, never perform, resume."""

import pytest
from sqlalchemy import select

from assistant.accounts.models import Account
from assistant.agent.models import AgentRun, ToolConfirmation
from tests.integration.business_env import OWNER, deliver, private_message
from tests.scenarios.p2_drive import Scenario, run_queued, tool_outputs

pytestmark = pytest.mark.integration


async def paused(p2: Scenario) -> bool:
    async with p2.env.factory() as session:
        return bool(
            await session.scalar(select(Account.autonomy_paused).where(Account.id == p2.account_id))
        )


async def test_pause_blocks_an_autonomous_action_at_claim(p2: Scenario) -> None:
    row = await p2.seed(state="approved", origin="delegation_auto")
    await deliver(p2.env, private_message(OWNER, "/pause"))
    assert await paused(p2)
    await p2.execute(row.id)
    final = await p2.action(row.id)
    assert final.state in {"cancelled", "blocked"}
    assert final.state_reason == "autonomy_paused"
    assert p2.business_sends() == []


async def test_a_stop_word_pauses_before_any_model_call(p2: Scenario) -> None:
    await deliver(p2.env, private_message(OWNER, "STOP"))
    assert await paused(p2)
    assert await run_queued(p2, "agent.owner_request") == 0
    assert p2.fake.requests == []
    assert p2.business_sends() == []


@pytest.mark.parametrize("tainted", [False, True])
async def test_a_model_resume_call_never_resumes(p2: Scenario, tainted: bool) -> None:
    await deliver(p2.env, private_message(OWNER, "/pause"))
    await p2.owner("resume_autonomy", forwarded=tainted)
    assert await paused(p2)
    async with p2.env.factory() as session:
        runs = list(
            await session.scalars(select(AgentRun).where(AgentRun.account_id == p2.account_id))
        )
        assert runs[-1].tainted is tainted
        confirmations = list(
            await session.scalars(
                select(ToolConfirmation).where(ToolConfirmation.account_id == p2.account_id)
            )
        )
    assert len(confirmations) == 1
    assert any(r.get("status") == "awaiting_confirmation" for r in tool_outputs(p2))
    assert p2.business_sends() == []


async def test_pause_does_not_block_an_owner_approved_send(p2: Scenario) -> None:
    await deliver(p2.env, private_message(OWNER, "/pause"))
    row = await p2.seed(state="approved", origin="owner_dictated", base_message_id=None)
    await p2.execute(row.id)
    assert await paused(p2)
    assert (await p2.action(row.id)).state == "succeeded"
    assert len(p2.business_sends()) == 1
