"""The privacy-policy update notice (AGENTS §16; policy v4, M02 D59).

v4 widens what is kept (encrypted backups), so an owner who consented under
an older version is told once, with the link and the opt-out; the consent
itself stays valid (nothing is re-asked).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import select, text

from assistant.accounts import service
from assistant.accounts.models import Account
from assistant.agent.confirmations import ApplyOutcome
from assistant.agent.runtime import RunOutcome
from assistant.inbox.notify import OwnerAlerts
from assistant.inbox.policy_notice import POLICY_CHANGES, deliver_policy_update_notices
from assistant.jobs.tasks import JobHandlers
from tests.fakes.channel import FakeChannel
from tests.integration.business_env import OWNER, Env, deliver, onboard, private_message

pytestmark = pytest.mark.integration

POLICY_URL = "https://example.invalid/policy?a=1&b=2"


async def _account_id(env: Env) -> int:
    async with env.factory() as session:
        return (
            await session.execute(select(Account.id).where(Account.telegram_user_id == OWNER))
        ).scalar_one()


async def _consents_recorded_under(env: Env, version: str) -> None:
    async with env.factory() as session, session.begin():
        await session.execute(text("UPDATE app.consents SET policy_version = :v"), {"v": version})


async def _deliver(env: Env, channel: FakeChannel, *, url: str | None = POLICY_URL) -> int:
    alerts = OwnerAlerts(env.factory, channel, env.clock)
    return await deliver_policy_update_notices(env.factory, env.clock, alerts, policy_url=url)


def test_the_current_policy_version_has_a_change_summary() -> None:
    """A bump that forgets its notice entry would skip the owner notice."""
    assert service.POLICY_VERSION == "v4"
    assert service.POLICY_VERSION in POLICY_CHANGES


async def test_an_owner_who_consented_under_an_older_policy_is_told_once(env: Env) -> None:
    await onboard(env)
    await _consents_recorded_under(env, "v3")
    channel = FakeChannel()

    assert await _deliver(env, channel) == 1
    env.clock.advance(days=2)
    assert await _deliver(env, channel) == 0

    [notice] = channel.sends
    assert "privacy policy was updated (version 4)" in notice
    assert "where your data is hosted and adds encrypted backups" in notice
    assert (
        '<a href="https://example.invalid/policy?a=1&amp;b=2">'
        "https://example.invalid/policy?a=1&amp;b=2</a>" in notice
    )
    assert "<code>/privacy revoke</code>" in notice
    # Consent is not forced again: it stays granted, under the version agreed to.
    async with env.factory() as session:
        state = await service.active_consent(
            session, await _account_id(env), kind=service.CONSENT_AI_CONTROL
        )
    assert (state.granted, state.policy_version) == (True, "v3")


async def test_no_notice_for_a_consent_given_under_the_current_policy(env: Env) -> None:
    await onboard(env)
    channel = FakeChannel()
    assert await _deliver(env, channel) == 0
    assert channel.sends == []


async def test_no_notice_once_the_older_consent_was_revoked(env: Env) -> None:
    await onboard(env)
    await _consents_recorded_under(env, "v3")
    await deliver(env, private_message(OWNER, "/privacy revoke"))
    channel = FakeChannel()
    assert await _deliver(env, channel) == 0
    assert channel.sends == []


async def test_without_a_policy_url_the_notice_points_to_privacy(env: Env) -> None:
    await onboard(env)
    await _consents_recorded_under(env, "v3")
    channel = FakeChannel()
    assert await _deliver(env, channel, url=None) == 1
    assert "See /privacy" in channel.sends[0]
    assert "<a href" not in channel.sends[0]


async def test_an_undelivered_notice_is_retried_hourly_not_every_minute(env: Env) -> None:
    await onboard(env)
    await _consents_recorded_under(env, "v3")
    channel = FakeChannel(deliver=False)

    assert await _deliver(env, channel) == 0
    env.clock.advance(minutes=1)
    assert await _deliver(env, channel) == 0
    assert len(channel.sends) == 1  # one attempt, not one per watchdog run

    env.clock.advance(hours=1)
    channel.deliver = True
    assert await _deliver(env, channel) == 1
    env.clock.advance(hours=2)
    assert await _deliver(env, channel) == 0
    assert len(channel.sends) == 2


@dataclass
class _NoRuntime:
    async def run(self, turn_id: int) -> RunOutcome:  # pragma: no cover - not called
        raise AssertionError("not used")


@dataclass
class _NoApplier:
    async def apply(self, confirmation_id: int) -> ApplyOutcome:  # pragma: no cover
        raise AssertionError("not used")


async def test_the_ingestion_watchdog_sends_the_notice(env: Env) -> None:
    await onboard(env)
    await _consents_recorded_under(env, "v3")
    channel = FakeChannel()
    handlers = JobHandlers(
        session_factory=env.factory,
        clock=env.clock,
        scheduler=env.scheduler,
        owner_runtime=_NoRuntime(),
        alerts=OwnerAlerts(env.factory, channel, env.clock),
        monthly_budget_usd=None,
        heartbeat_url=None,
        confirmation_applier=_NoApplier(),
        privacy_policy_url=POLICY_URL,
    )

    await handlers.ingestion_watchdog()
    await handlers.ingestion_watchdog()

    notices = [s for s in channel.sends if "privacy policy was updated" in s]
    assert len(notices) == 1
