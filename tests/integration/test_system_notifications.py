"""CP4 synchronous system alerts: durable claims, no content persistence."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import timedelta

import pytest
from sqlalchemy import delete, select, text

from assistant.accounts.models import Account, AuditLogEntry
from assistant.core.channel import Button, Delivery
from assistant.inbox.models import Notification
from assistant.inbox.notify import AlertKind, AlertResult, OwnerAlerts
from assistant.inbox.policy_notice import deliver_policy_update_notices
from assistant.inbox.system_notifications import (
    SYSTEM_LOCK_NAMESPACE,
    SystemAlertClaim,
    SystemNotificationDelivery,
    system_lock_key,
)
from tests.fakes.channel import FakeChannel
from tests.integration.business_env import Env, onboard

pytestmark = pytest.mark.integration


async def account_id(env: Env) -> int:
    await onboard(env)
    async with env.factory() as session:
        return (await session.execute(select(Account.id))).scalar_one()


async def records(env: Env) -> tuple[list[Notification], list[AuditLogEntry]]:
    async with env.factory() as session:
        return (
            list((await session.scalars(select(Notification).order_by(Notification.id))).all()),
            list(
                (
                    await session.scalars(
                        select(AuditLogEntry)
                        .where(AuditLogEntry.event.in_(["system_alert", "system_alert_claimed"]))
                        .order_by(AuditLogEntry.id)
                    )
                ).all()
            ),
        )


async def notify(alerts: OwnerAlerts, account: int, key: str = "synthetic:system") -> AlertResult:
    return await alerts.notify_owner(
        account,
        AlertKind.BUDGET_THRESHOLD,
        dedupe_key=key,
        percent=80,
        buttons=(Button("Synthetic button", "synthetic:callback"),),
    )


async def test_system_delivery_claims_before_send_and_keeps_only_structural_refs(env: Env) -> None:
    account = await account_id(env)
    channel = FakeChannel()
    result = await notify(OwnerAlerts(env.factory, channel, env.clock), account)
    rows, audits = await records(env)
    assert result.sent
    assert result.message_id is not None
    assert len(rows) == 1
    [row] = rows
    assert (row.kind, row.alert_class, row.priority, row.status) == (
        "system",
        "system",
        "urgent",
        "sent",
    )
    assert row.refs == {"alert_kind": "budget_threshold"}
    assert row.message_id == result.message_id
    assert [a.event for a in audits] == ["system_alert_claimed", "system_alert"]
    assert audits[1].details["claim_id"] == audits[0].id
    assert audits[1].details["notification_id"] == row.id
    assert channel.buttons == [(Button("Synthetic button", "synthetic:callback"),)]
    assert "Synthetic button" not in str([a.details for a in audits])


class HeldChannel(FakeChannel):
    def __init__(self, *, succeeds: bool = True, crash: bool = False) -> None:
        super().__init__(deliver=succeeds)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.crash = crash

    async def send(
        self, *, account_id: int, chat_id: int, text: str, buttons: Sequence[Button] = ()
    ) -> Delivery:
        self.started.set()
        await self.release.wait()
        if self.crash:
            raise RuntimeError("synthetic process interruption")
        return await super().send(
            account_id=account_id, chat_id=chat_id, text=text, buttons=buttons
        )


async def test_concurrent_same_key_returns_in_progress_without_a_second_send(env: Env) -> None:
    account = await account_id(env)
    channel = HeldChannel()
    alerts = OwnerAlerts(env.factory, channel, env.clock)
    first = asyncio.create_task(notify(alerts, account))
    try:
        await channel.started.wait()
        rows, audits = await records(env)
        assert rows[0].status == "uncertain"
        assert [a.event for a in audits] == ["system_alert_claimed"]
        # Both transaction locks have gone before the channel is entered.
        async with env.factory() as session, session.begin():
            await session.execute(
                select(Account).where(Account.id == account).with_for_update(nowait=True)
            )
            assert await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(:namespace, :key)"),
                {
                    "namespace": SYSTEM_LOCK_NAMESPACE,
                    "key": system_lock_key(account, "synthetic:system"),
                },
            )
        async with asyncio.timeout(5):
            second = await notify(alerts, account)
        assert (second.sent, second.reason, second.uncertain) == (False, "in_progress", False)
    finally:
        channel.release.set()
        await first
    assert len(channel.sends) == 1


async def test_repeated_resolution_appends_one_immutable_outcome(env: Env) -> None:
    account = await account_id(env)
    service = SystemNotificationDelivery(env.factory, FakeChannel(), env.clock)
    claim = await service._claim(account, "budget_threshold", "synthetic:resolution", None)
    assert isinstance(claim, SystemAlertClaim)
    first = await service._record(claim, Delivery(ok=True, message_id=555))
    assert first.sent
    second = await service._record(claim, Delivery(ok=False, error="forbidden"))
    assert second.sent
    rows, audits = await records(env)
    assert rows[0].status == "sent"
    assert rows[0].message_id == 555
    assert [a.event for a in audits] == ["system_alert_claimed", "system_alert"]


async def test_a_crash_after_send_before_resolution_never_replays(
    env: Env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = await account_id(env)
    channel = FakeChannel()
    alerts = OwnerAlerts(env.factory, channel, env.clock)

    async def interrupted(
        self: SystemNotificationDelivery,
        claim: SystemAlertClaim,
        delivery: Delivery,
    ) -> None:
        raise RuntimeError("synthetic interruption before outcome commit")

    with monkeypatch.context() as patch:
        patch.setattr(SystemNotificationDelivery, "_record", interrupted)
        with pytest.raises(RuntimeError, match="synthetic interruption before outcome commit"):
            await notify(alerts, account)
    assert (await notify(alerts, account)).reason == "in_progress"
    assert len(channel.sends) == 1
    rows, audits = await records(env)
    assert rows[0].status == "uncertain"
    assert [a.event for a in audits] == ["system_alert_claimed"]


async def test_an_existing_attempt_key_collision_is_skipped(env: Env) -> None:
    account = await account_id(env)
    now = env.clock.now()
    epoch_ms = int(now.timestamp() * 1000)
    async with env.factory() as session, session.begin():
        session.add(
            Notification(
                account_id=account,
                kind="system",
                alert_class="system",
                priority="urgent",
                status="failed",
                refs={},
                dedupe_key=f"sys:synthetic:system:{epoch_ms}",
                created_at=now,
                updated_at=now,
            )
        )
    assert (await notify(OwnerAlerts(env.factory, FakeChannel(), env.clock), account)).sent
    rows, audits = await records(env)
    assert len({row.dedupe_key for row in rows}) == 2
    assert audits[0].details["attempt_ms"] == epoch_ms + 1


async def test_a_once_ever_claim_survives_crash_notification_sweep_and_any_clock_advance(
    env: Env,
) -> None:
    """A `cooldown=None` key (incl. `uncertain:{action_id}`) whose send was cut
    off after the claim committed is never sent again (ICR #132, P2-T24)."""
    account = await account_id(env)
    crashing = HeldChannel(crash=True)
    crashing.release.set()
    with pytest.raises(RuntimeError, match="synthetic process interruption"):
        await notify(OwnerAlerts(env.factory, crashing, env.clock), account)
    async with env.factory() as session, session.begin():
        await session.execute(delete(Notification))
    channel = FakeChannel()
    alerts = OwnerAlerts(env.factory, channel, env.clock)
    for days in (1, 30, 300):
        env.clock.advance(days=days)
        assert (await notify(alerts, account)).reason == "in_progress"
    assert channel.sends == []
    assert [a.event for a in (await records(env))[1]] == ["system_alert_claimed"]


async def test_an_interrupted_cooldown_claim_blocks_like_an_uncertain_alert_then_resends_once(
    env: Env,
) -> None:
    """TL amendment to ICR #132: with a cooldown, an unresolved claim older than
    the cooldown counts as an uncertain resolution at its claim time."""
    account = await account_id(env)
    cooldown = timedelta(hours=1)

    async def stall(alerts: OwnerAlerts) -> AlertResult:
        return await alerts.notify_owner(
            account, AlertKind.INGESTION_STALL, dedupe_key="synthetic:stall", cooldown=cooldown
        )

    crashing = HeldChannel(crash=True)
    crashing.release.set()
    with pytest.raises(RuntimeError, match="synthetic process interruption"):
        await stall(OwnerAlerts(env.factory, crashing, env.clock))
    async with env.factory() as session, session.begin():
        await session.execute(delete(Notification))
    channel = FakeChannel()
    alerts = OwnerAlerts(env.factory, channel, env.clock)
    env.clock.advance(minutes=59)
    assert (await stall(alerts)).reason == "in_progress"
    env.clock.advance(minutes=1)  # the same boundary a recorded uncertain alert blocks to
    assert (await stall(alerts)).reason == "in_progress"
    assert channel.sends == []
    env.clock.advance(seconds=1)
    assert (await stall(alerts)).sent
    assert (await stall(alerts)).reason == "duplicate"
    assert len(channel.sends) == 1
    audits = (await records(env))[1]
    assert [a.event for a in audits] == [
        "system_alert_claimed",
        "system_alert_claimed",
        "system_alert",
    ]
    # The interrupted claim stays unresolved; no history is rewritten.
    assert audits[2].details["claim_id"] == audits[1].id
    assert audits[2].details["delivered"] is True


async def test_a_resolution_for_a_deleted_account_is_explicitly_not_recorded(env: Env) -> None:
    account = await account_id(env)
    service = SystemNotificationDelivery(env.factory, FakeChannel(), env.clock)
    claim = await service._claim(account, "budget_threshold", "synthetic:deleted", None)
    assert isinstance(claim, SystemAlertClaim)
    async with env.factory() as session, session.begin():
        await session.execute(delete(Account).where(Account.id == account))
    outcome = await service._record(claim, Delivery(ok=True, message_id=777))
    assert (outcome.sent, outcome.reason, outcome.message_id, outcome.uncertain) == (
        True,
        "not_recorded",
        777,
        False,
    )
    rows, audits = await records(env)
    assert (rows, audits) == ([], [])


@pytest.mark.parametrize("uncertain", [False, True])
async def test_historical_success_or_uncertainty_dedupes_without_notifications(
    env: Env,
    uncertain: bool,
) -> None:
    account = await account_id(env)
    async with env.factory() as session, session.begin():
        session.add(
            AuditLogEntry(
                account_id=account,
                at=env.clock.now(),
                actor="system",
                event="system_alert",
                details={
                    "kind": "budget_threshold",
                    "key": "synthetic:system",
                    "delivered": not uncertain,
                    **({"uncertain": True} if uncertain else {}),
                },
            )
        )
    channel = FakeChannel()
    result = await notify(OwnerAlerts(env.factory, channel, env.clock), account)
    assert result.reason == "duplicate"
    assert channel.sends == []


@pytest.mark.parametrize("rewind", [False, True])
async def test_definite_failure_releases_claim_with_monotonic_attempts_on_frozen_clock(
    env: Env,
    rewind: bool,
) -> None:
    account = await account_id(env)
    channel = FakeChannel(deliver=False)
    alerts = OwnerAlerts(env.factory, channel, env.clock)
    assert (await notify(alerts, account)).reason == "not_delivered"
    if rewind:
        env.clock.advance(seconds=-2)
    channel.deliver = True
    assert (await notify(alerts, account)).sent
    rows, audits = await records(env)
    assert [r.status for r in rows] == ["failed", "sent"]
    claims = [a for a in audits if a.event == "system_alert_claimed"]
    assert claims[1].details["attempt_ms"] == claims[0].details["attempt_ms"] + 1
    assert len({r.dedupe_key for r in rows}) == 2


async def test_system_key_share_does_not_wait_for_standard_cap_claim(env: Env) -> None:
    account = await account_id(env)
    async with env.factory() as session, session.begin():
        await session.execute(
            select(Account).where(Account.id == account).with_for_update(key_share=True)
        )
        async with asyncio.timeout(5):
            result = await notify(OwnerAlerts(env.factory, FakeChannel(), env.clock), account)
    assert result.sent


async def test_different_keys_and_accounts_do_not_wait_for_an_inflight_send(env: Env) -> None:
    from tests.integration.p2_factories import seed_account

    account = await account_id(env)
    async with env.factory() as session, session.begin():
        other = (await seed_account(session)).id
    held = HeldChannel()
    alerts = OwnerAlerts(env.factory, held, env.clock)
    first = asyncio.create_task(notify(alerts, account))
    try:
        await held.started.wait()
        fast = FakeChannel()
        separate = OwnerAlerts(env.factory, fast, env.clock)
        async with asyncio.timeout(5):
            assert (await notify(separate, account, "synthetic:different")).sent
            assert (await notify(separate, other)).sent
        assert fast.recipients == [account, other]
    finally:
        held.release.set()
        await first


async def test_uncertain_system_outcome_is_not_replayed_after_notification_sweep(env: Env) -> None:
    account = await account_id(env)
    channel = FakeChannel(deliver=False, uncertain=True)
    alerts = OwnerAlerts(env.factory, channel, env.clock)
    result = await notify(alerts, account)
    assert result.uncertain
    assert result.reason == "uncertain"
    async with env.factory() as session, session.begin():
        await session.execute(delete(Notification))
    assert (await notify(alerts, account)).reason == "duplicate"
    assert len(channel.sends) == 1


async def test_a_resolution_is_written_when_notification_is_deleted_during_send(env: Env) -> None:
    account = await account_id(env)
    channel = HeldChannel()
    alerts = OwnerAlerts(env.factory, channel, env.clock)
    first = asyncio.create_task(notify(alerts, account))
    try:
        await channel.started.wait()
        async with env.factory() as session, session.begin():
            await session.execute(delete(Notification))
    finally:
        channel.release.set()
        result = await first
    assert result.sent
    rows, audits = await records(env)
    assert rows == []
    assert [a.event for a in audits] == ["system_alert_claimed", "system_alert"]
    assert (await notify(alerts, account)).reason == "duplicate"


async def test_policy_notice_in_progress_then_definite_failure_does_not_settle(env: Env) -> None:
    account = await account_id(env)
    async with env.factory() as session, session.begin():
        await session.execute(text("UPDATE app.consents SET policy_version = 'v4'"))
    channel = HeldChannel(succeeds=False)
    alerts = OwnerAlerts(env.factory, channel, env.clock)

    async def deliver() -> int:
        return await deliver_policy_update_notices(env.factory, env.clock, alerts, policy_url=None)

    first = asyncio.create_task(deliver())
    try:
        await channel.started.wait()
        assert await deliver() == 0
        async with env.factory() as session:
            assert (await session.get(Account, account)).policy_notice_version != "v5"
    finally:
        channel.release.set()
        assert await first == 0
    async with env.factory() as session:
        assert (await session.get(Account, account)).policy_notice_version != "v5"
    env.clock.advance(hours=1, seconds=1)
    channel.deliver = True
    assert await deliver() == 1
