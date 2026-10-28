"""Assist debounce and negative triggers, driven through business ingestion."""

import pytest
from sqlalchemy import text

from assistant.conversations.modes import set_mode
from assistant.db.uow import transaction
from tests.integration.business_env import CONTACT, OWNER, business_message, deliver
from tests.scenarios.p2_drive import Scenario, advance, draft_result, run_queued

pytestmark = pytest.mark.integration


async def assist(p2: Scenario) -> None:
    # The public mode setter must support Assist; no raw mode update substitutes for it.
    from assistant.conversations.modes import SETTABLE_MODES

    assert "assist" in SETTABLE_MODES, "Assist not implemented: awaiting S04.2"
    async with transaction(p2.env.factory) as uow:
        await set_mode(
            uow.session,
            account_id=p2.account_id,
            conversation_id=p2.conversation_id,
            mode="assist",
            actor="owner",
            clock=p2.env.clock,
        )


async def queued_drafts(p2: Scenario) -> int:
    async with p2.env.factory() as session:
        return int(
            await session.scalar(
                text(
                    "SELECT count(*) FROM procrastinate.procrastinate_jobs "
                    "WHERE task_name = 'replies.draft' AND status = 'todo'"
                )
            )
        )


async def test_a_burst_in_an_assist_chat_produces_one_card(p2: Scenario) -> None:
    await assist(p2)
    p2.fake.parsed.append(draft_result())
    for number in range(3):
        await deliver(p2.env, business_message(message_id=102 + number, date=p2.date))
        await advance(p2, 1)
    assert await queued_drafts(p2) == 1
    await advance(p2, 20)
    await run_queued(p2, "replies.draft")
    [row] = await p2.rows()
    assert row.state == "awaiting_approval"
    assert row.card_message_id is not None
    assert len([r for r in p2.fake.requests if r.purpose == "draft_reply"]) == 1
    assert p2.business_sends() == []


async def test_no_card_when_the_draft_says_no_reply_is_needed(p2: Scenario) -> None:
    await assist(p2)
    p2.fake.parsed.append(draft_result(needs_reply=False, skip_reason="no_reply_needed", text=""))
    await deliver(p2.env, business_message(message_id=102, date=p2.date))
    assert await queued_drafts(p2) == 1
    await advance(p2, 20)
    assert await run_queued(p2, "replies.draft") == 1
    assert await p2.rows() == []
    from sqlalchemy import select

    from assistant.accounts.models import AuditLogEntry

    async with p2.env.factory() as session:
        events = list(
            await session.scalars(
                select(AuditLogEntry.event).where(AuditLogEntry.account_id == p2.account_id)
            )
        )
    assert "draft_skipped" in events
    assert p2.business_sends() == []


async def test_owner_messages_never_trigger_drafts(p2: Scenario) -> None:
    await assist(p2)
    await deliver(
        p2.env,
        business_message(from_id=OWNER, chat_id=CONTACT, message_id=102, date=p2.date),
        business_message(
            from_id=OWNER,
            chat_id=CONTACT,
            message_id=103,
            date=p2.date,
            extra={"is_from_offline": True},
        ),
    )
    await advance(p2, 20)
    assert await queued_drafts(p2) == 0
    assert await run_queued(p2, "replies.draft") == 0
    assert p2.fake.requests == []
    assert await p2.rows() == []


async def test_observe_chats_get_no_proactive_drafts(p2: Scenario) -> None:
    await deliver(p2.env, business_message(message_id=102, date=p2.date))
    await advance(p2, 20)
    assert await queued_drafts(p2) == 0
    assert await run_queued(p2, "replies.draft") == 0
    assert p2.fake.requests == []
    assert await p2.rows() == []
