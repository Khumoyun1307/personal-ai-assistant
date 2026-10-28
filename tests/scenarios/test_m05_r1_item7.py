"""R1 item 7: deleting a chat removes its derived P2 state only."""

import pytest

from assistant.automation.models import Automation
from assistant.db.uow import transaction
from assistant.inbox.models import ConversationTriage, Notification, Task
from tests.integration.business_env import callback, deliver, live_card
from tests.integration.p2_factories import (
    seed_automation,
    seed_conversation,
    seed_notification,
    seed_task,
    seed_triage,
)
from tests.scenarios.m05_drive import M05Scenario, run_queued

pytestmark = pytest.mark.integration


async def test_deleting_a_conversation_removes_its_tasks_triage_and_notifications(
    m05: M05Scenario,
) -> None:
    async with transaction(m05.env.factory) as uow:
        other = await seed_conversation(uow.session, account_id=m05.account_id)
        for conv in (m05.conversation_id, other.id):
            await seed_task(uow.session, account_id=m05.account_id, conversation_id=conv)
            await seed_triage(uow.session, account_id=m05.account_id, conversation_id=conv)
            await seed_notification(
                uow.session,
                account_id=m05.account_id,
                conversation_id=conv,
                refs={"conversation_ids": [conv]},
            )
            await seed_automation(
                uow.session, account_id=m05.account_id, refs={"conversation_ids": [conv]}
            )
    await m05.owner(
        "delete_conversation_data", conversation_id=m05.conversation_id, contact_id=None
    )
    assert len(await m05.rows(Task)) == 2
    card = await live_card(m05.env, "tool_confirm")
    await deliver(
        m05.env, callback(f"tool_confirm:{card.token}:confirm", message_id=card.prompt_message_id)
    )
    assert await run_queued(m05, "agent.apply_confirmation") == 1
    await run_queued(m05, "privacy.delete_data")
    for model in (Task, ConversationTriage, Notification):
        [survivor] = await m05.rows(model)
        assert survivor.conversation_id == other.id
    [monitor] = await m05.rows(Automation)
    assert monitor.refs["conversation_ids"] == [other.id]
