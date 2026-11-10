"""I9 suppression inspection/reset is content-free and tenant-scoped."""

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.service import create_card
from assistant.inbox.notification_feedback import list_mutes, reset_mutes
from assistant.inbox.notification_policy import MUTE_WINDOW
from assistant.inbox.notification_types import NotificationKind
from assistant.inbox.notifications import NotificationDelivery
from assistant.telegram.control.panels import MUTES_CARD, mutes_screen
from tests.fakes.channel import FakeChannel
from tests.integration.business_env import Env, callback, deliver
from tests.integration.p2_factories import seed_account, seed_notification
from tests.integration.test_notifications import DAY, delivery, owner_chat

pytestmark = pytest.mark.integration


async def mute(session: AsyncSession, account_id: int, *, monitor: int | None = None) -> int:
    ids = []
    for _ in range(2):
        row = await seed_notification(
            session,
            account_id=account_id,
            feedback="not_useful",
            updated_at=DAY,
            kind="monitor_match" if monitor is not None else "triage_needs_reply",
            refs={"automation_ids": [monitor]} if monitor is not None else {},
        )
        ids.append(row.id)
    return ids[0]


async def test_mutes_are_visible_scoped_and_resettable(env: Env) -> None:
    async with env.factory() as session, session.begin():
        owner = await seed_account(session)
        other = await seed_account(session)
        first = await mute(session, owner.id)
        monitor = await mute(session, owner.id, monitor=11)
        foreign = await mute(session, other.id)
        env.clock.set(DAY)
        shown = await mutes_screen(session, env.clock, owner)
        assert "triage needs reply" in shown.text
        assert "monitor #11" in shown.text
        assert "Not useful feedback" in shown.text
        assert DAY.isoformat() in shown.text
        assert shown.rows is not None
        assert any(op == f"r{first}" for row in shown.rows for _, op in row)
        assert any(op == f"r{monitor}" for row in shown.rows for _, op in row)
        assert not any(op == f"r{foreign}" for row in shown.rows for _, op in row)
        assert not await reset_mutes(session, account_id=owner.id, notification_id=foreign)
        assert len(await list_mutes(session, account_id=owner.id, now=DAY)) == 2
        assert await reset_mutes(session, account_id=owner.id, notification_id=monitor)
        [remaining] = await list_mutes(session, account_id=owner.id, now=DAY)
        assert remaining.notification_id == first
        assert await reset_mutes(session, account_id=owner.id)
        assert await list_mutes(session, account_id=owner.id, now=DAY) == []
        assert len(await list_mutes(session, account_id=other.id, now=DAY)) == 1


async def test_delivery_reads_monitor_scoped_feedback(env: Env) -> None:
    env.clock.set(DAY)
    async with env.factory() as session, session.begin():
        account = await seed_account(session)
        await mute(session, account.id, monitor=11)
        original = await seed_notification(
            session,
            account_id=account.id,
            kind="monitor_match",
            priority="high",
            refs={"automation_ids": [11]},
        )
        unrelated = await seed_notification(
            session,
            account_id=account.id,
            kind="monitor_match",
            priority="high",
            refs={"automation_ids": [12]},
        )
        sender: NotificationDelivery = delivery(env, FakeChannel())
        settings = await sender._settings(session, account, DAY)
        old_facts = await sender._facts(session, original)
        new_facts = await sender._facts(session, unrelated)
        assert old_facts.kind == NotificationKind.MONITOR_MATCH
        assert old_facts.monitor_id == 11
        assert new_facts.monitor_id == 12
        assert ("monitor_match:11", None) in settings.muted
        assert ("monitor_match:12", None) not in settings.muted


async def test_mute_list_expires_and_paginate_without_losing_scopes(env: Env) -> None:
    env.clock.set(DAY)
    async with env.factory() as session, session.begin():
        account = await seed_account(session)
        for monitor in range(1, 11):
            await mute(session, account.id, monitor=monitor)
        first = await mutes_screen(session, env.clock, account)
        last = await mutes_screen(session, env.clock, account, page=1)
        assert first.rows is not None
        assert last.rows is not None
        first_ids = {op for row in first.rows for _, op in row if op.startswith("r")}
        last_ids = {op for row in last.rows for _, op in row if op.startswith("r")}
        assert len(first_ids) == 8
        assert len(last_ids) == 2
        assert not first_ids.intersection(last_ids)
        expired = await list_mutes(
            session, account_id=account.id, now=DAY + MUTE_WINDOW + timedelta(seconds=1)
        )
        assert expired == []


async def test_mute_reset_callback_cannot_target_another_account(env: Env) -> None:
    env.clock.set(DAY)
    where = await owner_chat(env)
    async with env.factory() as session, session.begin():
        other = await seed_account(session)
        foreign = await mute(session, other.id)
        own = await mute(session, where.account_id)
        card = await create_card(
            session, account_id=where.account_id, kind=MUTES_CARD, clock=env.clock
        )
        token = card.token
        other_id = other.id
    before = len(env.sender.edits)
    await deliver(env, callback(f"{MUTES_CARD}:{token}:r{foreign}"))
    assert len(env.sender.edits) == before
    assert env.sender.answered_callbacks[-1]["text"] == "This button is no longer active."
    async with env.factory() as session:
        [retained] = await list_mutes(session, account_id=where.account_id, now=DAY)
        assert retained.notification_id == own
        assert len(await list_mutes(session, account_id=other_id, now=DAY)) == 1
