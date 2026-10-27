"""Privacy receipts count the union of cancelled and redacted action rows."""

import pytest
from sqlalchemy import select, update

from assistant.actions.models import Action
from assistant.actions.states import PENDING
from assistant.privacy.deletion import delete_conversation_data
from tests.integration.business_env import Env
from tests.integration.test_action_lifecycle_bulk import seed_actions

pytestmark = pytest.mark.integration


async def test_deletion_counts_each_affected_action_once_across_cancellation_and_redaction(
    env: Env,
) -> None:
    account_id, conversation_id, ids = await seed_actions(env)
    async with env.factory() as session, session.begin():
        await session.execute(
            update(Action)
            .where(
                Action.account_id == account_id,
                Action.state.in_(PENDING),
            )
            .values(text=None)
        )
        report = await delete_conversation_data(
            session,
            account_id=account_id,
            conversation_id=conversation_id,
            actor="owner",
            clock=env.clock,
        )
    assert report.actions == len(ids)


async def test_a_rolled_back_deletion_restores_action_state_and_text(env: Env) -> None:
    account_id, conversation_id, ids = await seed_actions(env)

    async def delete_and_rollback() -> None:
        async with env.factory() as session, session.begin():
            await delete_conversation_data(
                session,
                account_id=account_id,
                conversation_id=conversation_id,
                actor="owner",
                clock=env.clock,
            )
            raise RuntimeError("synthetic rollback")

    with pytest.raises(RuntimeError, match="synthetic rollback"):
        await delete_and_rollback()
    async with env.factory() as session:
        action = (
            await session.scalars(
                select(Action).where(
                    Action.id == ids["awaiting_approval"],
                    Action.account_id == account_id,
                )
            )
        ).one()
        assert action.state == "awaiting_approval"
        assert action.text == "synthetic action text"
        assert action.approval_token is not None
