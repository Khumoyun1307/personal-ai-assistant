"""R1 #9: durable send identity, including echo-before-result and zero ids."""

import pytest
from sqlalchemy import select

from assistant.conversations.models import Message
from tests.integration.business_env import BOT_ID, CONTACT, OWNER, business_message, deliver
from tests.scenarios.p2_drive import Scenario

pytestmark = pytest.mark.integration


async def sent_rows(p2: Scenario) -> list[Message]:
    async with p2.env.factory() as session:
        return list(
            await session.scalars(
                select(Message).where(
                    Message.account_id == p2.account_id,
                    Message.conversation_id == p2.conversation_id,
                    Message.direction == "assistant_sent",
                )
            )
        )


async def test_each_send_is_recorded_once_as_assistant_sent(p2: Scenario) -> None:
    row = await p2.seed(state="approved")
    await p2.execute(row.id)
    [message] = await sent_rows(p2)
    assert message.action_id == row.id
    assert message.text == row.text
    assert message.telegram_message_id == (await p2.action(row.id)).telegram_message_id
    assert message.telegram_message_id is not None
    assert len(p2.business_sends()) == 1


async def test_an_echo_processed_first_merges_into_the_sent_record(p2: Scenario) -> None:
    row = await p2.seed(state="approved")
    echoed = False

    async def echo_before_result() -> None:
        nonlocal echoed
        if echoed:
            return
        echoed = True
        assert (await p2.action(row.id)).state == "executing"
        await deliver(
            p2.env,
            business_message(
                from_id=OWNER,
                chat_id=CONTACT,
                message_id=p2.env.sender._next_message_id + 1,
                date=p2.date,
                message_text=row.text,
                extra={
                    "sender_business_bot": {"id": BOT_ID, "is_bot": True, "first_name": "Synthetic"}
                },
            ),
        )

    p2.env.sender.on_call = echo_before_result
    await p2.execute(row.id)
    p2.env.sender.on_call = None
    assert echoed
    [message] = await sent_rows(p2)
    assert message.action_id == row.id
    assert (await p2.action(row.id)).state == "succeeded"
    assert len(p2.business_sends()) == 1


async def test_two_zero_id_sends_in_one_chat_both_succeed(p2: Scenario) -> None:
    # S04.4's plain transport fake consumes these exact ids, including zero.
    p2.env.sender.plain_script = [0, 0]
    ids = []
    for _ in range(2):
        row = await p2.seed(state="approved")
        await p2.execute(row.id)
        final = await p2.action(row.id)
        assert final.state == "succeeded"
        assert final.identity_pending
        ids.append(row.id)
    rows = await sent_rows(p2)
    assert sorted(r.action_id for r in rows) == sorted(ids)
    assert len(p2.business_sends()) == 2
