"""S05.2 notifications: enqueue, delivery and the reconciler, over Postgres
and the real jobs table (S05.2 §3.2, §3.4).

Required tests, findable by name: `test_enqueue_dedupes_on_the_key`,
`test_delivery_is_at_most_once_under_two_concurrent_jobs`,
`test_an_uncertain_delivery_is_never_resent`,
`test_a_definite_failure_is_retried_then_fails`,
`test_a_deleted_source_suppresses_the_alert`,
`test_a_deferred_alert_is_delivered_after_quiet_hours`,
`test_batched_alerts_wait_for_the_digest`,
`test_the_reconciler_recovers_a_lost_delivery`,
`test_standard_alerts_wait_while_the_flag_is_off`,
`test_notifications_never_cross_accounts` (RG-21),
`test_alert_texts_carry_no_message_text`.

Synthetic text only; FakeChannel stands in for the control chat (AGENTS §11).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text

from assistant.accounts.callbacks import encode, parse
from assistant.accounts.models import Account
from assistant.accounts.service import create_card
from assistant.conversations.models import Conversation, Message
from assistant.core.jobs import NOTIFY_TASK
from assistant.db.uow import transaction
from assistant.inbox.models import ConversationTriage, Notification
from assistant.inbox.notification_types import NotificationKind, NotificationRefs, Priority
from assistant.inbox.notifications import (
    NTF_CARD,
    NotificationCounts,
    NotificationDelivery,
    enqueue,
    mark_digested,
    notifications_batched_since,
)
from assistant.jobs.reconcile_inbox import notification_reconciler
from assistant.observability.status import build_status
from assistant.telegram.control.alerts import handle_ntf_press
from assistant.telegram.control.routes import ControlRoutes
from tests.fakes.channel import FakeChannel
from tests.integration.business_env import (
    CONTACT,
    Env,
    business_message,
    callback,
    connect,
    deliver,
    environment,
    grant_managed,
)
from tests.integration.p2_factories import (
    seed_account,
    seed_conversation,
    seed_notification,
    seed_triage,
)

pytestmark = pytest.mark.integration

#: 12:00 UTC is 17:00 in Tashkent: outside the default quiet hours (23-08).
DAY = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
#: 20:00 UTC is 01:00 in Tashkent: inside them; they end at 03:00 UTC.
NIGHT = datetime(2026, 10, 18, 20, 0, tzinfo=UTC)
QUIET_END = datetime(2026, 10, 19, 3, 0, tzinfo=UTC)
CANARY = "SYNTH-CANARY message text that must never reach an alert"


@dataclass(frozen=True)
class Chat:
    account_id: int
    conversation_id: int
    contact_id: int


async def chat(env: Env, *, name: str = "Kamola", reasons: list[str] | None = None) -> Chat:
    async with env.factory() as session, session.begin():
        account = await seed_account(session, timezone="Asia/Tashkent")
        conversation = await seed_conversation(session, account_id=account.id)
        await session.execute(
            text("UPDATE app.contacts SET display_name = :n WHERE id = :c"),
            {"n": name, "c": conversation.contact_id},
        )
        await seed_triage(
            session,
            account_id=account.id,
            conversation_id=conversation.id,
            requests_decision=True,
            urgency="urgent",
            escalation_category="urgent_decision",
            reasons=reasons if reasons is not None else ["Narxni bugun tasdiqlash kerak"],
        )
        session.add(
            Message(
                account_id=account.id,
                conversation_id=conversation.id,
                direction="contact",
                observed_via="new",
                sent_at=DAY - timedelta(minutes=5),
                content_type="text",
                text=CANARY,
            )
        )
        return Chat(account.id, conversation.id, conversation.contact_id)


async def add(
    env: Env,
    where: Chat,
    *,
    kind: NotificationKind = NotificationKind.TRIAGE_URGENT,
    priority: Priority = Priority.URGENT,
    key: str = "triage:1:1:urgent",
    scheduler: bool = False,
) -> int | None:
    # Through the unit of work, like every caller: after-commit defers run there.
    async with transaction(env.factory) as uow:
        return await enqueue(
            uow.session,
            account_id=where.account_id,
            kind=kind,
            priority=priority,
            dedupe_key=key,
            refs=NotificationRefs(
                conversation_ids=(where.conversation_id,), contact_ids=(where.contact_id,)
            ),
            clock=env.clock,
            conversation_id=where.conversation_id,
            contact_id=where.contact_id,
            scheduler=env.scheduler if scheduler else None,
        )


def delivery(env: Env, channel: FakeChannel, *, enabled: bool = True) -> NotificationDelivery:
    assert env.scheduler is not None
    return NotificationDelivery(
        session_factory=env.factory,
        channel=channel,
        clock=env.clock,
        scheduler=env.scheduler,
        enabled=enabled,
    )


async def row(env: Env, notification_id: int) -> Notification:
    async with env.factory() as session:
        found = await session.get(Notification, notification_id)
        assert found is not None
        return found


async def notify_jobs(env: Env) -> list[tuple[int, str, datetime | None]]:
    async with env.factory() as session:
        rows = await session.execute(
            text(
                "SELECT (args->>'notification_id')::bigint, status::text, scheduled_at "
                "FROM procrastinate.procrastinate_jobs WHERE task_name = :t ORDER BY id"
            ),
            {"t": NOTIFY_TASK},
        )
        return [(int(r[0]), str(r[1]), r[2]) for r in rows.all()]


# ============================================================ enqueue
async def test_enqueue_dedupes_on_the_key(env: Env) -> None:
    where = await chat(env)
    first = await add(env, where)
    again = await add(env, where)
    assert first is not None
    assert again is None
    async with env.factory() as session:
        assert await session.scalar(select(Notification.id).where(Notification.id == first))
        stored = (await session.execute(select(Notification))).scalars().all()
    assert len(stored) == 1
    assert stored[0].refs == {
        "conversation_ids": [where.conversation_id],
        "contact_ids": [where.contact_id],
    }
    assert (stored[0].status, stored[0].alert_class) == ("queued", "standard")


async def test_enqueue_defers_one_delivery_after_commit(env: Env) -> None:
    where = await chat(env)
    notification = await add(env, where, scheduler=True)
    assert [(i, status) for i, status, _ in await notify_jobs(env)] == [(notification, "todo")]
    # A rolled-back enqueue leaves neither a row nor a job.

    async def enqueue_then_fail() -> None:
        async with transaction(env.factory) as uow:
            await enqueue(
                uow.session,
                account_id=where.account_id,
                kind=NotificationKind.TRIAGE_DECISION,
                priority=Priority.HIGH,
                dedupe_key="triage:1:1:decision",
                refs=NotificationRefs(),
                clock=env.clock,
                scheduler=env.scheduler,
            )
            raise RuntimeError("rollback")

    with pytest.raises(RuntimeError):
        await enqueue_then_fail()
    assert len(await notify_jobs(env)) == 1
    async with env.factory() as session:
        assert await session.scalar(select(func.count(Notification.id))) == 1


# ============================================================ delivery
async def test_an_urgent_alert_is_delivered_once_with_its_reasons(env: Env) -> None:
    env.clock.set(DAY)
    where = await chat(env, name="Kamola <b>")
    notification = await add(env, where)
    channel = FakeChannel()
    assert notification is not None
    result = await delivery(env, channel).deliver(notification)
    assert result.status == "sent"
    (sent,) = channel.sends
    assert "Kamola &lt;b&gt;" in sent  # contact-chosen name, escaped
    assert "Narxni bugun tasdiqlash kerak" in sent
    stored = await row(env, notification)
    assert (stored.status, stored.sent_at, stored.message_id) == ("sent", DAY, 501)


async def test_delivery_is_at_most_once_under_two_concurrent_jobs(env: Env) -> None:
    env.clock.set(DAY)
    where = await chat(env)
    notification = await add(env, where)
    assert notification is not None
    channel = FakeChannel()
    service = delivery(env, channel)
    results = await asyncio.gather(service.deliver(notification), service.deliver(notification))
    assert sorted(r.status for r in results) == ["not_queued", "sent"]
    assert len(channel.sends) == 1


async def test_an_uncertain_delivery_is_never_resent(env: Env) -> None:
    env.clock.set(DAY)
    where = await chat(env)
    notification = await add(env, where)
    assert notification is not None
    channel = FakeChannel(deliver=False, uncertain=True)
    service = delivery(env, channel)
    assert (await service.deliver(notification)).status == "uncertain"
    stored = await row(env, notification)
    assert (stored.status, stored.reason_code) == ("uncertain", "ambiguous")
    channel.deliver = True
    assert (await service.deliver(notification)).status == "not_queued"
    assert len(channel.sends) == 1


async def test_a_crash_after_the_claim_never_resends(env: Env) -> None:
    """The claim commits `uncertain` before the send: a worker dying between
    the two leaves an alert that is never sent again (P2-T25)."""
    env.clock.set(DAY)
    where = await chat(env)
    notification = await add(env, where)
    assert notification is not None

    class Dying(FakeChannel):
        async def send(self, **kwargs: Any) -> Any:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await delivery(env, Dying()).deliver(notification)
    stored = await row(env, notification)
    assert (stored.status, stored.reason_code) == ("uncertain", "claimed")
    channel = FakeChannel()
    assert (await delivery(env, channel).deliver(notification)).status == "not_queued"
    assert channel.sends == []


async def test_a_definite_failure_is_retried_then_fails(env: Env) -> None:
    env.clock.set(DAY)
    where = await chat(env)
    notification = await add(env, where)
    assert notification is not None
    channel = FakeChannel(deliver=False)
    service = delivery(env, channel)
    for attempt in (1, 2):
        assert (await service.deliver(notification)).status == "retry"
        stored = await row(env, notification)
        assert (stored.status, stored.reason_code) == ("queued", f"not_delivered:{attempt}")
        assert stored.deliver_after is not None
        assert stored.deliver_after > env.clock.now()
        # Not due yet: a job that runs early does nothing.
        assert (await service.deliver(notification)).status == "not_due"
        env.clock.set(stored.deliver_after)
    assert (await service.deliver(notification)).status == "failed"
    stored = await row(env, notification)
    assert (stored.status, stored.reason_code) == ("failed", "not_delivered")
    assert len(channel.sends) == 3
    # The retry is re-deferred at its due time (here the first still holds
    # the queueing lock: tests never run jobs).
    assert [(i, s) for i, s, _ in await notify_jobs(env)] == [(notification, "todo")]


async def test_a_deleted_source_suppresses_the_alert(env: Env) -> None:
    """The renderer reads the verdict from its refs; when the chat's data is
    gone, nothing is sent and the row says why."""
    env.clock.set(DAY)
    where = await chat(env)
    notification = await add(env, where)
    assert notification is not None
    async with env.factory() as session, session.begin():
        await session.execute(
            ConversationTriage.__table__.delete().where(
                ConversationTriage.conversation_id == where.conversation_id
            )
        )
    channel = FakeChannel()
    assert (await delivery(env, channel).deliver(notification)).status == "suppressed"
    stored = await row(env, notification)
    assert (stored.status, stored.reason_code) == ("suppressed", "source_deleted")
    assert channel.sends == []


async def test_a_deferred_alert_is_delivered_after_quiet_hours(env: Env) -> None:
    env.clock.set(NIGHT)
    where = await chat(env)
    notification = await add(env, where)
    assert notification is not None
    channel = FakeChannel()
    service = delivery(env, channel)
    assert (await service.deliver(notification)).status == "deferred"
    stored = await row(env, notification)
    assert (stored.status, stored.deliver_after, stored.reason_code) == (
        "queued",
        QUIET_END,
        "quiet_hours",
    )
    assert channel.sends == []
    assert [(i, at) for i, _, at in await notify_jobs(env)] == [(notification, QUIET_END)]
    env.clock.set(QUIET_END)
    assert (await service.deliver(notification)).status == "sent"
    assert len(channel.sends) == 1


async def test_batched_alerts_wait_for_the_digest(env: Env) -> None:
    env.clock.set(DAY)
    where = await chat(env)
    notification = await add(
        env,
        where,
        kind=NotificationKind.TRIAGE_NEEDS_REPLY,
        priority=Priority.NORMAL,
        key="triage:1:1:needs_reply",
    )
    assert notification is not None
    channel = FakeChannel()
    assert (await delivery(env, channel).deliver(notification)).status == "batched"
    stored = await row(env, notification)
    assert (stored.status, stored.reason_code) == ("batched", "quiet_by_default")
    assert channel.sends == []
    async with env.factory() as session, session.begin():
        batched = await notifications_batched_since(
            session, account_id=where.account_id, since=DAY - timedelta(hours=1)
        )
        assert [b.notification_id for b in batched] == [notification]
        await session.execute(
            text(
                "INSERT INTO app.digests (account_id, period_start, "
                "period_end, status) VALUES (:a, :s, :e, 'uncertain')"
            ),
            {"a": where.account_id, "s": DAY - timedelta(days=1), "e": DAY},
        )
        digest_id = await session.scalar(text("SELECT max(id) FROM app.digests"))
        assert (
            await mark_digested(
                session, account_id=where.account_id, ids=[notification], digest_id=digest_id
            )
            == 1
        )
    async with env.factory() as session:
        assert (
            await notifications_batched_since(
                session, account_id=where.account_id, since=DAY - timedelta(hours=1)
            )
            == []
        )


async def test_the_hourly_cap_counts_claimed_alerts(env: Env) -> None:
    """TL ruling (#53): rows claimed `uncertain` count toward the cap like
    sent ones, so concurrent claims cannot exceed it."""
    env.clock.set(DAY)
    where = await chat(env)
    async with env.factory() as session, session.begin():
        for i in range(6):
            session.add(
                Notification(
                    account_id=where.account_id,
                    kind="triage_urgent",
                    priority="urgent",
                    status="uncertain" if i % 2 else "sent",
                    reason_code="claimed" if i % 2 else None,
                    dedupe_key=f"earlier:{i}",
                    refs={},
                    sent_at=None if i % 2 else DAY - timedelta(minutes=10),
                    created_at=DAY - timedelta(minutes=20),
                    updated_at=DAY - timedelta(minutes=10),
                )
            )
    notification = await add(env, where)
    assert notification is not None
    channel = FakeChannel()
    assert (await delivery(env, channel).deliver(notification)).status == "batched"
    assert (await row(env, notification)).reason_code == "cap"


async def test_standard_alerts_wait_while_the_flag_is_off(env: Env) -> None:
    env.clock.set(DAY)
    where = await chat(env)
    notification = await add(env, where)
    assert notification is not None
    channel = FakeChannel()
    assert (await delivery(env, channel, enabled=False).deliver(notification)).status == (
        "flag_off"
    )
    assert (await row(env, notification)).status == "queued"
    assert channel.sends == []
    env.clock.advance(minutes=5)
    async with env.factory() as session:
        assert await notification_reconciler(enabled=False)(session, env.clock.now()) == []


async def test_the_reconciler_recovers_a_lost_delivery(env: Env) -> None:
    """Enqueued, committed, but the defer was lost (a crash in between): the
    reconciler re-creates the job once the row is 30 s old and due."""
    env.clock.set(DAY)
    where = await chat(env)
    lost = await add(env, where)
    assert lost is not None
    reconcile = notification_reconciler(enabled=True)
    async with env.factory() as session:
        assert await reconcile(session, env.clock.now()) == []  # too young
    env.clock.advance(seconds=31)
    async with env.factory() as session:
        (request,) = await reconcile(session, env.clock.now())
    assert (request.task, dict(request.args), request.queueing_lock) == (
        NOTIFY_TASK,
        {"notification_id": lost},
        f"notify:{lost}",
    )
    # With its job queued, nothing more.
    assert env.scheduler is not None
    await env.scheduler.defer(request.task, args=request.args, queueing_lock=request.queueing_lock)
    async with env.factory() as session:
        assert await reconcile(session, env.clock.now()) == []


async def test_notifications_never_cross_accounts(env: Env) -> None:
    """RG-21: dedupe keys, the cap's load and the batched list are per
    account."""
    env.clock.set(DAY)
    mine, theirs = await chat(env), await chat(env, name="Bobur")
    assert await add(env, mine) is not None
    assert await add(env, theirs) is not None  # the same key in another account
    async with env.factory() as session, session.begin():
        for i in range(10):  # the other account is far over its cap
            session.add(
                Notification(
                    account_id=theirs.account_id,
                    kind="triage_urgent",
                    priority="urgent",
                    status="sent",
                    dedupe_key=f"other:{i}",
                    refs={},
                    sent_at=DAY - timedelta(minutes=5),
                    created_at=DAY - timedelta(minutes=5),
                    updated_at=DAY - timedelta(minutes=5),
                )
            )
    async with env.factory() as session:
        mine_id = await session.scalar(
            select(Notification.id).where(
                Notification.account_id == mine.account_id,
                Notification.dedupe_key == "triage:1:1:urgent",
            )
        )
    assert mine_id is not None
    channel = FakeChannel()
    assert (await delivery(env, channel).deliver(mine_id)).status == "sent"
    assert "Bobur" not in channel.sends[0]
    assert channel.recipients == [mine.account_id]
    async with env.factory() as session:
        assert (
            await notifications_batched_since(
                session, account_id=theirs.account_id, since=DAY - timedelta(days=1)
            )
            == []
        )
        assert (
            await mark_digested(
                session, account_id=theirs.account_id, ids=[mine_id], digest_id=None
            )
            == 0
        )


async def test_alert_texts_carry_no_message_text(env: Env) -> None:
    env.clock.set(DAY)
    where = await chat(env)
    channel = FakeChannel()
    service = delivery(env, channel)
    for kind, priority, key in (
        (NotificationKind.TRIAGE_URGENT, Priority.URGENT, "k:urgent"),
        (NotificationKind.TRIAGE_DECISION, Priority.HIGH, "k:decision"),
        (NotificationKind.TRIAGE_NEEDS_REPLY, Priority.HIGH, "k:reply"),
        (NotificationKind.TRIAGE_SUSPICIOUS, Priority.HIGH, "k:suspicious"),
    ):
        notification = await add(env, where, kind=kind, priority=priority, key=key)
        assert notification is not None
        assert (await service.deliver(notification)).status == "sent"
    assert len(channel.sends) == 4
    assert all(CANARY not in sent for sent in channel.sends)
    async with env.factory() as session:
        stored = (await session.execute(select(Notification))).scalars().all()
    assert all(CANARY not in str(n.refs) and CANARY not in n.dedupe_key for n in stored)


async def test_status_counts_alerts_by_state(env: Env) -> None:
    """S05.2 §3.8: the /status block and `get_status` carry counts only."""
    env.clock.set(DAY)
    where = await chat(env)
    async with env.factory() as session, session.begin():
        for i, (status, reason, after, updated) in enumerate(
            (
                ("queued", None, None, DAY),
                ("queued", "quiet_hours", DAY + timedelta(hours=8), DAY),
                ("batched", "quiet_by_default", None, DAY),
                ("batched", "quiet_by_default", None, DAY),
                ("uncertain", "ambiguous", None, DAY - timedelta(hours=2)),
                ("uncertain", "ambiguous", None, DAY - timedelta(days=2)),  # too old
                ("sent", None, None, DAY),
            )
        ):
            session.add(
                Notification(
                    account_id=where.account_id,
                    kind="triage_needs_reply",
                    priority="normal",
                    status=status,
                    reason_code=reason,
                    deliver_after=after,
                    dedupe_key=f"status:{i}",
                    refs={},
                    created_at=updated,
                    updated_at=updated,
                )
            )
    async with env.factory() as session:
        account = await session.get(Account, where.account_id)
        assert account is not None
        report = await build_status(session, account, DAY, monthly_budget_usd=None)
    assert report.notifications == NotificationCounts(
        queued=1, deferred=1, batched=2, uncertain_24h=1
    )
    assert report.notifications_line == (
        "Alerts: 1 queued, 1 held for quiet hours, 2 waiting for the digest, 1 uncertain (24 h)"
    )
    assert report.as_dict()["notifications"]["batched"] == 2


# ============================================================ buttons (`ntf`)
@pytest.fixture
async def routed(migrated_url: str) -> AsyncIterator[Env]:
    """The environment with the `ntf` callback kind routed, as `_wire_m05` does."""
    async with environment(
        migrated_url, routes=ControlRoutes(callbacks={NTF_CARD: handle_ntf_press})
    ) as built:
        yield built


async def owner_chat(env: Env) -> Chat:
    """The owner's real business chat with Kamola, triaged."""
    await connect(env)
    await grant_managed(env)
    await deliver(
        env,
        business_message(message_id=11, message_text="SYNTH Narx?", first_name="Kamola"),
    )
    async with env.factory() as session, session.begin():
        conversation = await session.scalar(
            select(Conversation).where(Conversation.business_chat_id == CONTACT)
        )
        assert conversation is not None
        await seed_triage(
            session,
            account_id=conversation.account_id,
            conversation_id=conversation.id,
            requests_decision=True,
            urgency="urgent",
            escalation_category="urgent_decision",
        )
        return Chat(conversation.account_id, conversation.id, conversation.contact_id)


async def delivered_card(env: Env) -> tuple[int, dict[str, str]]:
    """Deliver one urgent alert; its id and its buttons' callback data by op."""
    env.clock.set(DAY)
    where = await owner_chat(env)
    notification = await add(env, where)
    assert notification is not None
    channel = FakeChannel()
    assert (await delivery(env, channel).deliver(notification)).status == "sent"
    (buttons,) = channel.buttons
    assert [b.label for b in buttons] == ["Open chat", "Useful", "Not useful"]
    return notification, {parse(b.callback_data).op: b.callback_data for b in buttons}


async def test_feedback_buttons_record_and_answer(routed: Env) -> None:
    notification, buttons = await delivered_card(routed)
    await deliver(routed, callback(buttons["nu"]))
    assert (await row(routed, notification)).feedback == "not_useful"
    await deliver(routed, callback(buttons["us"]))  # the card stays: the last press wins
    assert (await row(routed, notification)).feedback == "useful"
    assert [a["text"] for a in routed.sender.answered_callbacks][-2:] == [
        "Noted — fewer alerts like this",
        "Noted",
    ]


async def test_open_chat_shows_the_panel_only_for_the_owners_chat(routed: Env) -> None:
    """RG-21 style: a press resolves only this owner's card; another
    account's token is "no longer active" and shows nothing of its chat."""
    _, buttons = await delivered_card(routed)
    before = len(routed.sender.texts)
    await deliver(routed, callback(buttons["op"]))
    assert len(routed.sender.texts) == before + 1
    assert "Kamola" in routed.sender.texts[-1]

    async with routed.factory() as session, session.begin():
        other = await seed_account(session)
        chat = await seed_conversation(session, account_id=other.id)
        foreign = await seed_notification(
            session, account_id=other.id, conversation_id=chat.id, status="sent"
        )
        card = await create_card(
            session, account_id=other.id, kind=NTF_CARD, clock=routed.clock, ref_id=foreign.id
        )
        assert card.token is not None
        token = card.token
    sent = len(routed.sender.texts)
    await deliver(routed, callback(encode(NTF_CARD, token, "op")))
    assert len(routed.sender.texts) == sent
    assert routed.sender.answered_callbacks[-1]["text"] == "This button is no longer active."
