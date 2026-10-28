"""S05.3b: the daily digest — the schedule that follows the preference, the
deterministic content, at-most-once delivery, `/digest` and the "Open"
buttons (S05.3b §3.2 step 1, §3.4-§3.6, §3.8; P2-T13, P2-T20).

Required tests, findable by name:
`test_the_digest_lists_deletions_per_chat_without_saying_who`,
`test_the_digest_automation_follows_the_preference`,
`test_turning_the_digest_off_pauses_it_and_on_resumes_it`,
`test_the_digest_lists_batched_alerts_due_commitments_and_closed_tasks`,
`test_an_open_contact_commitment_due_today_appears_in_the_digest` (J7),
`test_a_quiet_day_digest_says_so`,
`test_the_digest_carries_coverage_caveats_for_its_period`,
`test_a_digest_built_twice_for_one_period_is_sent_once`,
`test_an_uncertain_digest_is_never_resent`,
`test_a_failed_digest_is_covered_by_the_next_one`,
`test_batched_alerts_appear_in_one_digest_only`,
`test_slash_digest_sends_one_digest_now`,
`test_the_digest_never_includes_another_accounts_rows` (RG-21),
`test_the_digest_automation_survives_delete_everything`,
`test_a_deleted_chats_tasks_never_appear_in_the_digest`,
`test_the_digest_carries_at_most_five_open_chat_buttons`,
`test_an_open_chat_button_for_a_foreign_chat_answers_no_longer_active`.

Synthetic text only; FakeChannel stands in for the control chat (AGENTS §11).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.callbacks import encode, parse
from assistant.accounts.models import Account, AuditLogEntry, PendingInput
from assistant.accounts.service import create_card
from assistant.automation.models import Automation
from assistant.automation.schedules import ensure_digest_schedule, reconcile_due
from assistant.conversations.models import CoverageGap, Message
from assistant.core.jobs import DIGEST_TASK
from assistant.inbox.digest import DIGEST_CARD, DigestDelivery, DigestResult
from assistant.inbox.models import Digest, Notification
from assistant.privacy.deletion import delete_conversation_data, delete_everything
from assistant.telegram.control.digest import (
    handle_digest_command,
    handle_digest_press,
    render_digest,
)
from assistant.telegram.control.routes import ControlRoutes
from tests.fakes.channel import FakeChannel
from tests.integration.business_env import (
    CONTACT,
    OWNER,
    Env,
    business_message,
    callback,
    connect,
    deliver,
    environment,
    private_message,
)
from tests.integration.p2_factories import (
    seed_account,
    seed_automation,
    seed_conversation,
    seed_notification,
    seed_task,
    seed_triage,
)

pytestmark = pytest.mark.integration

#: 21:00 in Tashkent (UTC+5) on 2026-10-18: the default digest time.
EVENING = datetime(2026, 10, 18, 16, 0, tzinfo=UTC)
CANARY = "SYNTH-CANARY message text that must never reach a digest"


@dataclass(frozen=True)
class Chat:
    account_id: int
    conversation_id: int
    contact_id: int


async def owner(env: Env, *, timezone: str = "Asia/Tashkent", **fields: Any) -> int:
    async with env.factory() as session, session.begin():
        return (await seed_account(session, timezone=timezone, **fields)).id


async def chat(
    env: Env,
    account_id: int,
    name: str,
    *,
    reasons: list[str] | None = None,
    verdict: dict[str, Any] | None = None,
    **conversation: Any,
) -> Chat:
    """A chat with `name`; with `reasons`, a triage verdict covering its
    (canary) contact message."""
    async with env.factory() as session, session.begin():
        conversation.setdefault("last_contact_msg_at", EVENING - timedelta(hours=4))
        row = await seed_conversation(session, account_id=account_id, **conversation)
        await session.execute(
            text("UPDATE app.contacts SET display_name = :n WHERE id = :c"),
            {"n": name, "c": row.contact_id},
        )
        message = Message(
            account_id=account_id,
            conversation_id=row.id,
            direction="contact",
            observed_via="new",
            sent_at=EVENING - timedelta(hours=4),
            content_type="text",
            text=CANARY,
        )
        session.add(message)
        await session.flush()
        if reasons is not None:
            await seed_triage(
                session,
                account_id=account_id,
                conversation_id=row.id,
                covers_through_message_id=message.id,
                reasons=reasons,
                **(verdict or {}),
            )
        return Chat(account_id, row.id, row.contact_id)


async def batched(
    env: Env, where: Chat, kind: str = "triage_decision", *, created: datetime | None = None
) -> int:
    async with env.factory() as session, session.begin():
        return (
            await seed_notification(
                session,
                account_id=where.account_id,
                conversation_id=where.conversation_id,
                contact_id=where.contact_id,
                kind=kind,
                priority="high",
                status="batched",
                reason_code="quiet_hours",
                created_at=created or EVENING - timedelta(hours=3),
            )
        ).id


def deliverer(env: Env, channel: FakeChannel) -> DigestDelivery:
    return DigestDelivery(
        session_factory=env.factory, channel=channel, clock=env.clock, render=render_digest
    )


async def build(
    env: Env, channel: FakeChannel, account_id: int, period_end: datetime = EVENING
) -> DigestResult:
    env.clock.set(max(env.clock.now(), period_end + timedelta(seconds=20)))
    return await deliverer(env, channel).build(account_id, period_end)


async def digests(env: Env, account_id: int) -> list[Digest]:
    async with env.factory() as session:
        return list(
            (
                await session.execute(
                    select(Digest).where(Digest.account_id == account_id).order_by(Digest.id)
                )
            )
            .scalars()
            .all()
        )


async def notification(env: Env, notification_id: int) -> Notification:
    async with env.factory() as session:
        found = await session.get(Notification, notification_id)
        assert found is not None
        return found


# ============================================================ the schedule
async def test_the_digest_automation_follows_the_preference(db_session: AsyncSession) -> None:
    account = await seed_account(db_session, timezone="Asia/Tashkent")
    now = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)  # 14:00 in Tashkent
    # Without flag `digest.daily` nothing is created.
    assert (await reconcile_due(db_session, now)).digests == 0
    assert await _digest_automations(db_session, account.id) == []

    report = await reconcile_due(db_session, now, digest_enabled=True)
    assert report.digests == 1
    [digest] = await _digest_automations(db_session, account.id)
    assert (digest.status, digest.rrule, digest.tz_policy, digest.timezone) == (
        "active",
        "FREQ=DAILY",
        "follow_owner",
        "Asia/Tashkent",
    )
    assert digest.spec == {
        "schema_version": 1,
        "max_lateness_minutes": 120,
        "dtstart_local": "2026-10-18T21:00",
        "kind": "digest",
    }
    assert digest.refs == {}
    assert digest.confirmed_at == now
    assert digest.next_run_at == EVENING

    # The owner picks 08:30: the next run moves to tomorrow 08:30 local.
    account.settings = {**(account.settings or {}), "digest_time": "08:30"}
    await db_session.flush()
    assert (await reconcile_due(db_session, now, digest_enabled=True)).digests == 1
    await db_session.refresh(digest)
    assert digest.spec["dtstart_local"] == "2026-10-18T08:30"
    assert digest.next_run_at == datetime(2026, 10, 19, 3, 30, tzinfo=UTC)
    # Nothing changed: nothing is rewritten.
    assert (await reconcile_due(db_session, now, digest_enabled=True)).digests == 0
    # A new timezone moves it too (to 08:30 in Moscow, UTC+3).
    account.timezone = "Europe/Moscow"
    await db_session.flush()
    assert (await reconcile_due(db_session, now, digest_enabled=True)).digests == 1
    await db_session.refresh(digest)
    assert (digest.timezone, digest.next_run_at) == (
        "Europe/Moscow",
        datetime(2026, 10, 19, 5, 30, tzinfo=UTC),
    )
    assert len(await _digest_automations(db_session, account.id)) == 1


async def _digest_automations(session: AsyncSession, account_id: int) -> list[Automation]:
    return list(
        (
            await session.execute(
                select(Automation)
                .where(Automation.account_id == account_id)
                .where(Automation.kind == "digest")
            )
        )
        .scalars()
        .all()
    )


async def test_turning_the_digest_off_pauses_it_and_on_resumes_it(
    db_session: AsyncSession,
) -> None:
    account = await seed_account(db_session, timezone="Asia/Tashkent")
    now = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
    await reconcile_due(db_session, now, digest_enabled=True)
    [digest] = await _digest_automations(db_session, account.id)

    account.settings = {**(account.settings or {}), "digest_time": "off"}
    await db_session.flush()
    later = now + timedelta(days=1)
    report = await reconcile_due(db_session, later, digest_enabled=True)
    await db_session.refresh(digest)
    assert digest.status == "paused"
    assert report.claimed == ()  # a paused digest never fires, even when due

    account.settings = {**(account.settings or {}), "digest_time": "21:00"}
    await db_session.flush()
    await reconcile_due(db_session, later, digest_enabled=True)
    await db_session.refresh(digest)
    assert digest.status == "active"
    # Resumed from now: the evening of `later`, with no backlog of skips.
    assert digest.next_run_at == EVENING + timedelta(days=1)
    assert len(await _digest_automations(db_session, account.id)) == 1


# ============================================================ content
async def test_the_digest_lists_batched_alerts_due_commitments_and_closed_tasks(
    env: Env,
) -> None:
    account_id = await owner(env)
    kamola = await chat(env, account_id, "Kamola", reasons=["Narxni bugun tasdiqlash kerak"])
    bobur = await chat(env, account_id, "Bobur <b>")
    decision = await batched(env, kamola, "triage_decision")
    others = [
        await batched(env, bobur, "triage_needs_reply"),
        await batched(env, bobur, "task_closed"),
    ]
    async with env.factory() as session, session.begin():
        await seed_task(
            session,
            account_id=account_id,
            conversation_id=bobur.conversation_id,
            kind="contact_commitment",
            title="Hujjatlarni yuborish",
            due_at=EVENING + timedelta(hours=5),
        )
        await seed_task(
            session,
            account_id=account_id,
            conversation_id=kamola.conversation_id,
            kind="owner_commitment",
            title="Send the synthetic quote",
            due_at=EVENING + timedelta(hours=20),
        )
        await seed_task(  # due beyond tomorrow evening: not yet
            session,
            account_id=account_id,
            conversation_id=kamola.conversation_id,
            kind="owner_commitment",
            title="Later synthetic promise",
            due_at=EVENING + timedelta(hours=30),
        )
        await seed_task(
            session,
            account_id=account_id,
            conversation_id=bobur.conversation_id,
            kind="open_question",
            title="Synthetic venue question",
            status="done",
            closed_reason="resolved_by_triage",
            closed_at=EVENING - timedelta(hours=2),
        )
    channel = FakeChannel()

    result = await build(env, channel, account_id)

    assert result.status == "sent"
    [sent] = channel.sends
    assert "<b>Needs you</b>\n• Kamola — Narxni bugun tasdiqlash kerak" in sent
    assert "Hujjatlarni yuborish — Bobur &lt;b&gt; promised, due 19 Oct 02:00" in sent
    assert "Send the synthetic quote — you promised Kamola, due 19 Oct 17:00" in sent
    assert "Later synthetic promise" not in sent
    assert "<b>Closed automatically</b>\n• Synthetic venue question (Bobur &lt;b&gt;)" in sent
    assert "• 1 chat(s) waiting for a reply" in sent
    assert "• 1 task(s) closed automatically" in sent
    assert "Quiet day" not in sent
    assert CANARY not in sent
    assert "Bobur <b>" not in sent
    [row] = await digests(env, account_id)
    assert (row.id, row.status) == (result.digest_id, "sent")
    assert row.message_id is not None
    assert row.period_start == EVENING - timedelta(hours=24)
    for notification_id in (decision, *others):
        assert (await notification(env, notification_id)).digest_id == row.id
    async with env.factory() as session:
        audit = (
            await session.execute(select(AuditLogEntry).where(AuditLogEntry.event == "digest_sent"))
        ).scalar_one()
    assert audit.details == {
        "needs_you": 1,
        "waiting": 1,  # Kamola has a reason; Bobur waits unchecked
        "commitments": 2,
        "closed": 1,
        "other_alerts": 2,
        "deletions": 0,
        "caveats": 1,
        "quiet": False,
    }


async def test_an_open_contact_commitment_due_today_appears_in_the_digest(env: Env) -> None:
    """J7 step 2: a contact commitment whose due time passed today and is
    still open appears in the next digest."""
    account_id = await owner(env)
    sardor = await chat(env, account_id, "Sardor")
    async with env.factory() as session, session.begin():
        await seed_task(
            session,
            account_id=account_id,
            conversation_id=sardor.conversation_id,
            kind="contact_commitment",
            title="Hujjatlarni yuboradi",
            due_at=EVENING - timedelta(hours=6),
        )
    channel = FakeChannel()
    await build(env, channel, account_id)
    assert (
        "• Hujjatlarni yuboradi — Sardor promised, due 18 Oct 15:00 (overdue)" in channel.sends[0]
    )


async def test_a_quiet_day_digest_says_so(env: Env) -> None:
    account_id = await owner(env)
    channel = FakeChannel()
    result = await build(env, channel, account_id)
    assert result.status == "sent"
    [sent] = channel.sends
    assert sent.endswith("Quiet day — nothing needs you.")
    assert "<b>Needs you</b>" not in sent
    assert channel.buttons == [()]


async def test_the_digest_carries_coverage_caveats_for_its_period(env: Env) -> None:
    account_id = await owner(env)
    async with env.factory() as session, session.begin():
        for start in (EVENING - timedelta(hours=10), EVENING - timedelta(days=5)):
            session.add(
                CoverageGap(
                    account_id=account_id,
                    conversation_id=None,
                    kind="outage",
                    certainty="possible",
                    at_risk_from=start,
                    at_risk_to=start + timedelta(hours=1),
                    created_at=start,
                )
            )
    channel = FakeChannel()
    await build(env, channel, account_id)
    [sent] = channel.sends
    coverage = sent.split("<b>Coverage</b>\n", 1)[1]
    assert "Possible gap: messages sent between 2026-10-18 11:00 and 2026-10-18 12:00" in coverage
    assert "2026-10-13" not in coverage  # before the period


async def test_the_digest_lists_deletions_per_chat_without_saying_who(env: Env) -> None:
    """Owner decision E2: every deletion is noted; Telegram does not say who
    deleted a message (TF §12 LV-07), so the digest never does."""
    account_id = await owner(env)
    kamola = await chat(env, account_id, "Kamola")
    async with env.factory() as session, session.begin():
        for direction, deleted_at in (
            ("contact", EVENING - timedelta(hours=1)),
            ("contact", EVENING - timedelta(hours=2)),
            ("owner_manual", EVENING - timedelta(hours=3)),
            ("assistant_sent", EVENING - timedelta(hours=4)),
            ("contact", EVENING - timedelta(days=3)),  # before the period
        ):
            session.add(
                Message(
                    account_id=account_id,
                    conversation_id=kamola.conversation_id,
                    direction=direction,
                    observed_via="new",
                    sent_at=deleted_at - timedelta(minutes=5),
                    content_type="text",
                    text=None,
                    deleted_at=deleted_at,
                )
            )
        session.add(
            AuditLogEntry(
                account_id=account_id,
                at=EVENING - timedelta(hours=5),
                actor="owner",
                event="data_deleted",
                subject_type="conversation",
                subject_id=kamola.conversation_id,
                details={"scope": "conversation", "messages": 3},
            )
        )
    channel = FakeChannel()
    await build(env, channel, account_id)
    [sent] = channel.sends
    section = sent.split("<b>Deletions</b>\n", 1)[1]
    assert section.startswith(
        "• Kamola: 4 message(s) deleted (2 of theirs, 1 of yours, 1 sent by the assistant)\n"
        "• At your request, at 18 Oct 16:00: one chat, 3 message(s)\n"
        "Telegram does not say who deleted a message."
    )
    lowered = sent.lower()
    assert "deleted by" not in lowered
    assert "kamola deleted" not in lowered
    assert "you deleted" not in lowered


# ============================================================ at most once
async def test_a_digest_built_twice_for_one_period_is_sent_once(env: Env) -> None:
    account_id = await owner(env)
    await batched(env, await chat(env, account_id, "Kamola", reasons=["Synthetic reason"]))
    channel = FakeChannel()
    first, second = await asyncio.gather(
        build(env, channel, account_id), build(env, channel, account_id)
    )
    again = await build(env, channel, account_id)
    assert sorted([first.status, second.status, again.status]) == [
        "duplicate",
        "duplicate",
        "sent",
    ]
    assert len(channel.sends) == 1
    assert [d.status for d in await digests(env, account_id)] == ["sent"]


async def test_an_uncertain_digest_is_never_resent(env: Env) -> None:
    account_id = await owner(env)
    alert = await batched(env, await chat(env, account_id, "Kamola", reasons=["Synthetic"]))
    channel = FakeChannel(deliver=False, uncertain=True)
    assert (await build(env, channel, account_id)).status == "uncertain"
    channel.deliver = True
    assert (await build(env, channel, account_id)).status == "duplicate"
    assert len(channel.sends) == 1
    [row] = await digests(env, account_id)
    assert row.status == "uncertain"
    # Its alerts stay with it: the next digest does not repeat them.
    assert (await notification(env, alert)).digest_id == row.id
    await build(env, channel, account_id, EVENING + timedelta(days=1))
    assert "Kamola" not in channel.sends[-1]


async def test_a_failed_digest_is_covered_by_the_next_one(env: Env) -> None:
    account_id = await owner(env)
    kamola = await chat(env, account_id, "Kamola", reasons=["Synthetic decision"])
    alert = await batched(env, kamola)
    failing = FakeChannel(deliver=False)
    assert (await build(env, failing, account_id)).status == "failed"
    assert (await notification(env, alert)).digest_id is None
    assert await _cards(env) == []

    channel = FakeChannel()
    tomorrow = EVENING + timedelta(days=1)
    assert (await build(env, channel, account_id, tomorrow)).status == "sent"
    assert "• Kamola — Synthetic decision" in channel.sends[0]
    failed, sent = await digests(env, account_id)
    assert (failed.status, sent.status) == ("failed", "sent")
    # No sent digest before it: the period is the day before it.
    assert sent.period_start == EVENING
    assert (await notification(env, alert)).digest_id == sent.id


async def test_a_new_digest_starts_where_the_last_sent_one_ended(env: Env) -> None:
    account_id = await owner(env)
    channel = FakeChannel()
    await build(env, channel, account_id, EVENING)
    await build(env, channel, account_id, EVENING + timedelta(hours=1, minutes=17))
    await build(env, channel, account_id, EVENING + timedelta(days=4))
    periods = [(d.period_start, d.period_end) for d in await digests(env, account_id)]
    assert periods == [
        (EVENING - timedelta(days=1), EVENING),
        (EVENING, EVENING + timedelta(hours=1, minutes=17)),
        # Never more than 48 hours back.
        (EVENING + timedelta(days=2), EVENING + timedelta(days=4)),
    ]
    # A late build for an older period than a sent one sends nothing.
    assert (await build(env, channel, account_id, EVENING + timedelta(days=3))).status == (
        "covered"
    )
    assert len(channel.sends) == 3


async def test_batched_alerts_appear_in_one_digest_only(env: Env) -> None:
    account_id = await owner(env)
    await batched(env, await chat(env, account_id, "Kamola", reasons=["Synthetic decision"]))
    channel = FakeChannel()
    await build(env, channel, account_id)
    await build(env, channel, account_id, EVENING + timedelta(hours=2))
    first, second = channel.sends
    assert "Kamola" in first
    assert "Kamola" not in second
    assert second.endswith("Quiet day — nothing needs you.")


# ============================================================ /digest
@pytest.fixture
async def routed(migrated_url: str) -> AsyncIterator[Env]:
    """The environment with `/digest` and the `dg` buttons routed, as
    `_wire_m05` does."""
    async with environment(
        migrated_url,
        routes=ControlRoutes(
            callbacks={DIGEST_CARD: handle_digest_press},
            commands={"digest": handle_digest_command},
        ),
    ) as built:
        yield built


async def digest_jobs(env: Env) -> list[tuple[int, int]]:
    async with env.factory() as session:
        rows = await session.execute(
            text(
                "SELECT (args->>'account_id')::bigint, (args->>'period_end')::bigint "
                "FROM procrastinate.procrastinate_jobs WHERE task_name = :t ORDER BY id"
            ),
            {"t": DIGEST_TASK},
        )
        return [(int(a), int(p)) for a, p in rows.all()]


async def test_slash_digest_sends_one_digest_now(routed: Env) -> None:
    await connect(routed)
    async with routed.factory() as session:
        account_id = await session.scalar(
            select(Account.id).where(Account.telegram_user_id == OWNER)
        )
    assert account_id is not None
    routed.clock.set(datetime(2026, 10, 18, 12, 30, 42, tzinfo=UTC))
    # A double tap within the minute is one digest.
    await deliver(routed, private_message(OWNER, "/digest"))
    routed.clock.advance(seconds=10)
    await deliver(routed, private_message(OWNER, "/digest"))
    period_end = int(datetime(2026, 10, 18, 12, 30, tzinfo=UTC).timestamp())
    assert await digest_jobs(routed) == [(account_id, period_end)]

    channel = FakeChannel()
    digests_job = deliverer(routed, channel)
    results = [
        await digests_job.build(account_id, datetime.fromtimestamp(period_end, UTC))
        for _ in range(2)
    ]
    assert [r.status for r in results] == ["sent", "duplicate"]
    assert len(channel.sends) == 1
    assert channel.sends[0].startswith("📋 <b>Digest</b> · ")
    [row] = await digests(routed, account_id)
    assert (row.period_start, row.period_end) == (
        datetime(2026, 10, 17, 12, 30, tzinfo=UTC),
        datetime(2026, 10, 18, 12, 30, tzinfo=UTC),
    )


async def test_slash_digest_asks_an_unfinished_owner_to_start(routed: Env) -> None:
    async with routed.factory() as session, session.begin():
        await seed_account(session, telegram_user_id=OWNER, status="onboarding")
    await deliver(routed, private_message(OWNER, "/digest"))
    assert await digest_jobs(routed) == []
    assert routed.sender.texts[-1] == "Send /start to finish setting me up first."


async def test_help_lists_digest(routed: Env) -> None:
    await connect(routed)
    await deliver(routed, private_message(OWNER, "/help"))
    assert "/digest — your digest now" in routed.sender.texts[-1]


# ============================================================ isolation, privacy
async def test_the_digest_never_includes_another_accounts_rows(env: Env) -> None:
    """RG-21: alerts, tasks, verdicts, deletions and names are read for the
    digest's own account only, and only its own alerts are marked."""
    mine = await owner(env)
    theirs = await owner(env)
    own_chat = await chat(env, mine, "Kamola", reasons=["Mine synthetic reason"])
    their_chat = await chat(env, theirs, "Foreign-Name", reasons=["Foreign synthetic reason"])
    own_alert = await batched(env, own_chat)
    their_alert = await batched(env, their_chat)
    async with env.factory() as session, session.begin():
        await seed_task(
            session,
            account_id=theirs,
            conversation_id=their_chat.conversation_id,
            kind="contact_commitment",
            title="Foreign synthetic commitment",
            due_at=EVENING,
        )
    channel = FakeChannel()
    await build(env, channel, mine)
    [sent] = channel.sends
    assert "Kamola" in sent
    assert "Foreign" not in sent
    assert channel.recipients == [mine]
    assert (await notification(env, own_alert)).digest_id is not None
    assert (await notification(env, their_alert)).digest_id is None
    assert await digests(env, theirs) == []


async def test_the_digest_automation_survives_delete_everything(env: Env) -> None:
    account_id = await owner(env)
    async with env.factory() as session, session.begin():
        account = await session.get(Account, account_id)
        assert account is not None
        digest = await ensure_digest_schedule(session, account, env.clock)
        assert digest is not None
        await seed_automation(session, account_id=account_id, kind="monitor")
    async with env.factory() as session, session.begin():
        report = await delete_everything(
            session, account_id=account_id, actor="owner", clock=env.clock
        )
    assert report.automations == 1
    async with env.factory() as session:
        kinds = (
            (
                await session.execute(
                    select(Automation.kind).where(Automation.account_id == account_id)
                )
            )
            .scalars()
            .all()
        )
    assert kinds == ["digest"]


async def test_a_deleted_chats_tasks_never_appear_in_the_digest(env: Env) -> None:
    account_id = await owner(env)
    gone = await chat(env, account_id, "Kamola", reasons=["Synthetic decision"])
    await batched(env, gone)
    async with env.factory() as session, session.begin():
        await seed_task(
            session,
            account_id=account_id,
            conversation_id=gone.conversation_id,
            kind="contact_commitment",
            title="Deleted synthetic commitment",
            due_at=EVENING,
        )
    async with env.factory() as session, session.begin():
        await delete_conversation_data(
            session,
            account_id=account_id,
            conversation_id=gone.conversation_id,
            actor="owner",
            clock=env.clock,
        )
    channel = FakeChannel()
    await build(env, channel, account_id)
    [sent] = channel.sends
    assert "Deleted synthetic commitment" not in sent
    assert "Synthetic decision" not in sent
    assert "Kamola" not in sent


# ============================================================ "Open" buttons
async def _cards(env: Env) -> list[PendingInput]:
    async with env.factory() as session:
        return list(
            (
                await session.execute(
                    select(PendingInput)
                    .where(PendingInput.kind == DIGEST_CARD)
                    .order_by(PendingInput.id)
                )
            )
            .scalars()
            .all()
        )


async def test_the_digest_carries_at_most_five_open_chat_buttons(env: Env) -> None:
    account_id = await owner(env)
    chats = [
        await chat(env, account_id, f"Synthetic contact {index}", reasons=["Synthetic"])
        for index in range(7)
    ]
    for index, where in enumerate(chats):
        await batched(env, where, created=EVENING - timedelta(hours=7 - index))
    channel = FakeChannel()
    await build(env, channel, account_id)
    [buttons] = channel.buttons
    assert [b.label for b in buttons] == [f"Open: Synthetic contact {i}" for i in range(5)]
    cards = await _cards(env)
    assert [c.ref_id for c in cards] == [c.conversation_id for c in chats[:5]]
    assert {parse(b.callback_data).op for b in buttons} == {"op"}
    assert [parse(b.callback_data).token for b in buttons] == [c.token for c in cards]
    [row] = await digests(env, account_id)
    assert {c.prompt_message_id for c in cards} == {row.message_id}
    # All seven chats are listed; only the buttons stop at five.
    assert channel.sends[0].count("• Synthetic contact") == 7


async def test_an_open_chat_button_for_a_foreign_chat_answers_no_longer_active(
    routed: Env,
) -> None:
    """RG-21 style: a press resolves only this owner's card; another
    account's token shows nothing of its chat."""
    await connect(routed)
    await deliver(
        routed, business_message(message_id=11, message_text="SYNTH Narx?", first_name="Kamola")
    )
    async with routed.factory() as session:
        account_id = await session.scalar(
            select(Account.id).where(Account.telegram_user_id == OWNER)
        )
        conversation_id, contact_id = (
            await session.execute(
                text("SELECT id, contact_id FROM app.conversations WHERE business_chat_id = :c"),
                {"c": CONTACT},
            )
        ).one()
    assert account_id is not None
    await batched(routed, Chat(account_id, int(conversation_id), int(contact_id)))
    channel = FakeChannel()
    await build(routed, channel, account_id)
    [buttons] = channel.buttons
    [button] = buttons

    before = len(routed.sender.texts)
    await deliver(routed, callback(button.callback_data))
    assert len(routed.sender.texts) == before + 1
    assert "Kamola" in routed.sender.texts[-1]
    assert routed.sender.answered_callbacks[-1]["text"] == "Opening the chat"

    async with routed.factory() as session, session.begin():
        other = await seed_account(session)
        foreign_chat = await seed_conversation(session, account_id=other.id)
        card = await create_card(
            session,
            account_id=other.id,
            kind=DIGEST_CARD,
            clock=routed.clock,
            ref_id=foreign_chat.id,
        )
        assert card.token is not None
        token = card.token
    sent = len(routed.sender.texts)
    await deliver(routed, callback(encode(DIGEST_CARD, token, "op")))
    assert len(routed.sender.texts) == sent
    assert routed.sender.answered_callbacks[-1]["text"] == "This button is no longer active."

    # The owner's own card for a chat that no longer exists answers the same.
    async with routed.factory() as session, session.begin():
        await session.execute(
            text("DELETE FROM app.conversations WHERE id = :c"), {"c": conversation_id}
        )
    await deliver(routed, callback(button.callback_data))
    assert len(routed.sender.texts) == sent
    assert routed.sender.answered_callbacks[-1]["text"] == "This button is no longer active."
