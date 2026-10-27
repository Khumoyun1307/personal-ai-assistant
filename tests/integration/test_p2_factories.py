"""Shared P2 seeds keep their references inside the requested account."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.core.errors import NotInAccountError
from tests.integration.p2_factories import (
    seed_account,
    seed_action,
    seed_automation,
    seed_conversation,
    seed_notification,
    seed_task,
    seed_triage,
)

pytestmark = pytest.mark.integration


async def test_factories_seed_flushed_account_scoped_rows(db_session: AsyncSession) -> None:
    owner = await seed_account(db_session)
    conversation = await seed_conversation(db_session, account_id=owner.id)
    rows = [
        await seed_action(db_session, account_id=owner.id, conversation_id=conversation.id),
        await seed_task(db_session, account_id=owner.id, conversation_id=conversation.id),
        await seed_triage(db_session, account_id=owner.id, conversation_id=conversation.id),
        await seed_automation(db_session, account_id=owner.id),
        await seed_notification(db_session, account_id=owner.id, conversation_id=conversation.id),
    ]
    assert all(row.id > 0 and row.account_id == owner.id for row in rows)


async def test_factories_refuse_a_foreign_conversation(db_session: AsyncSession) -> None:
    owner = await seed_account(db_session)
    other = await seed_account(db_session)
    conversation = await seed_conversation(db_session, account_id=other.id)
    for factory in (seed_action, seed_task, seed_triage, seed_notification):
        with pytest.raises(NotInAccountError):
            await factory(db_session, account_id=owner.id, conversation_id=conversation.id)
