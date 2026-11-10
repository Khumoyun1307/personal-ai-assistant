"""Explicit reconciliation of synthetic restores with no omitted privacy history.

Tests that simulate an actual lost deletion must replay or purge instead. This
helper preserves pre-I20 action/scheduling/consent tests while exercising the
new mandatory manual confirmation step.
"""

from uuid import UUID

from sqlalchemy import select

from assistant.accounts.models import Account
from assistant.db.models import KEY_RESTORE_PENDING, SystemState
from assistant.privacy.journal import complete_manual
from tests.integration.business_env import Env


async def reconcile_known_test_history(env: Env) -> None:
    async with env.factory() as session:
        marker = await session.get(SystemState, KEY_RESTORE_PENDING)
        assert marker is not None
        assert marker.value["maintenance_complete"] is True
        restore_id = UUID(marker.value["restore_id"])
        account_ids = frozenset(await session.scalars(select(Account.id)))
    await complete_manual(
        env.factory,
        env.clock,
        restore_id=restore_id,
        account_ids=account_ids,
        owner_confirmed=True,
    )
