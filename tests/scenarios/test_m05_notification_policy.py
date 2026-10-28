"""Notification policy through the durable queue and control gateway."""

import pytest

from assistant.inbox.models import Digest, Notification
from tests.fakes.sender import network_error
from tests.integration.business_env import callback, deliver
from tests.scenarios.m05_drive import M05Scenario, advance, run_queued

pytestmark = pytest.mark.integration


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S05.2/S05.3b")
async def test_quiet_hours_move_non_urgent_alerts_to_the_digest(m05: M05Scenario) -> None:
    await m05.preferences(quiet_hours="00:00-23:59")
    await m05.alert()
    await run_queued(m05, "notify.deliver")
    [row] = await m05.rows(Notification)
    assert (row.status, row.reason_code) == ("batched", "quiet_hours")
    assert m05.env.sender.sends == []
    await advance(m05, 60)
    await m05.job("digest.build", account_id=m05.account_id, period_end=m05.date)
    [row] = await m05.rows(Notification)
    [digest] = await m05.rows(Digest)
    assert row.digest_id == digest.id
    assert digest.status == "sent"
    assert len(m05.env.sender.sends) == 1


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S05.2")
async def test_urgent_bypasses_quiet_hours_only_when_enabled(m05: M05Scenario) -> None:
    await m05.preferences(quiet_hours="00:00-23:59", urgent_bypass=False)
    await m05.alert(priority="urgent")
    await run_queued(m05, "notify.deliver")
    [held] = await m05.rows(Notification)
    assert held.status == "queued"
    assert held.deliver_after is not None
    assert held.deliver_after > m05.env.clock.now()
    assert m05.env.sender.sends == []
    await m05.preferences(urgent_bypass=True)
    await m05.alert(key="bypass-on", priority="urgent")
    await run_queued(m05, "notify.deliver")
    rows = await m05.rows(Notification)
    assert [r.status for r in rows] == ["queued", "sent"]
    assert len(m05.env.sender.sends) == 1


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S05.2")
async def test_the_hourly_cap_batches_extra_standard_alerts(m05: M05Scenario) -> None:
    await m05.preferences(quiet_hours="off", alert_cap_per_hour=2)
    for i in range(3):
        await m05.alert(key=f"cap:{i}")
        await run_queued(m05, "notify.deliver")
    rows = await m05.rows(Notification)
    assert [r.status for r in rows] == ["sent", "sent", "batched"]
    assert rows[-1].reason_code == "cap"
    assert len(m05.env.sender.sends) == 2


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="awaiting S05.2 system-alert migration (CP4)"
)
async def test_a_system_alert_is_delivered_during_quiet_hours_with_the_cap_reached(
    m05: M05Scenario,
) -> None:
    await m05.preferences(quiet_hours="off", alert_cap_per_hour=1)
    await m05.alert(key="fill-cap")
    await run_queued(m05, "notify.deliver")
    await m05.preferences(quiet_hours="00:00-23:59")
    await m05.alert(key="system", kind="system", priority="urgent", alert_class="system")
    await run_queued(m05, "notify.deliver")
    assert [r.status for r in await m05.rows(Notification)] == ["sent", "sent"]
    assert len(m05.env.sender.sends) == 2


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S05.2")
async def test_a_duplicate_alert_is_sent_once(m05: M05Scenario) -> None:
    await m05.preferences(quiet_hours="off")
    await m05.alert(key="same")
    await m05.alert(key="same")
    await run_queued(m05, "notify.deliver")
    [row] = await m05.rows(Notification)
    assert row.status == "sent"
    assert len(m05.env.sender.sends) == 1


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S05.2")
async def test_an_uncertain_alert_is_never_resent_after_reconciliation(m05: M05Scenario) -> None:
    await m05.preferences(quiet_hours="off")
    identity = await m05.alert()
    m05.env.sender.fail_with = network_error()
    await run_queued(m05, "notify.deliver")
    m05.env.sender.fail_with = None
    assert (await m05.rows(Notification))[0].status == "uncertain"
    await advance(m05, 60)
    await m05.handlers.reconcile_pending()
    await run_queued(m05, "notify.deliver")
    await m05.job("notify.deliver", notification_id=identity)
    assert (await m05.rows(Notification))[0].status == "uncertain"
    assert len(m05.env.sender.sends) == 1


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S05.2")
async def test_not_useful_feedback_is_recorded(m05: M05Scenario) -> None:
    from tests.integration.business_env import live_card

    await m05.preferences(quiet_hours="off")
    await m05.alert()
    await run_queued(m05, "notify.deliver")
    card = await live_card(m05.env, "ntf")
    await deliver(m05.env, callback(f"ntf:{card.token}:nu", message_id=card.prompt_message_id))
    assert (await m05.rows(Notification))[0].feedback == "not_useful"
