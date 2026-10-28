"""J8: a manual reply wins, even when its update arrives late."""

import pytest

from tests.integration.business_env import CONTACT, OWNER, business_message, deliver
from tests.scenarios.p2_drive import Scenario

pytestmark = pytest.mark.integration


async def test_an_owner_message_dated_after_the_base_but_ingested_late_blocks_the_send(
    p2: Scenario,
) -> None:
    row = await p2.seed(state="approved")
    await deliver(
        p2.env, business_message(from_id=OWNER, chat_id=CONTACT, message_id=102, date=p2.date + 1)
    )
    await p2.execute(row.id)
    changed = await p2.action(row.id)
    assert (changed.state, changed.state_reason) == ("outdated", "takeover")
    assert p2.business_sends() == []


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S04.2")
async def test_takeover_outdates_the_card_and_audits_without_messaging_the_contact(
    p2: Scenario,
) -> None:
    row = await p2.seed()
    await deliver(
        p2.env, business_message(from_id=OWNER, chat_id=CONTACT, message_id=102, date=p2.date + 1)
    )
    result = await p2.action(row.id)
    assert (result.state, result.state_reason) == ("outdated", "takeover")
    assert "action_outdated" in await p2.events(row.id)
    from sqlalchemy import select

    from assistant.accounts.models import AuditLogEntry

    async with p2.env.factory() as session:
        events = list(
            await session.scalars(
                select(AuditLogEntry.event).where(AuditLogEntry.account_id == p2.account_id)
            )
        )
    assert "takeover_detected" in events
    assert p2.business_sends() == []
