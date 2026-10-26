"""The privacy-policy update notice (AGENTS §16; policy v4, M02 D59).

v4 widens what is kept (US hosting, encrypted backups with an EU copy), so
an owner with a consent recorded under an older version is told once, with
the link and the opt-out; the consent itself stays valid (nothing is
re-asked). "Once" is durable (`accounts.policy_notice_version`), not tied to
an audit row that the retention sweep deletes after a year.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import func, select, text

from assistant.accounts import service
from assistant.accounts.models import Account, AuditLogEntry
from assistant.agent.confirmations import ApplyOutcome
from assistant.agent.runtime import RunOutcome
from assistant.inbox.notify import EVENT_ALERT, OwnerAlerts
from assistant.inbox.policy_notice import (
    MAX_FAILED_ATTEMPTS,
    POLICY_CHANGES,
    dedupe_key,
    deliver_policy_update_notices,
)
from assistant.jobs.tasks import JobHandlers
from assistant.privacy.retention import sweep_all
from tests.fakes.channel import FakeChannel
from tests.fakes.summaries import RecordingRefresher
from tests.integration.business_env import OWNER, Env, deliver, onboard, private_message

pytestmark = pytest.mark.integration

POLICY_URL = "https://example.invalid/policy?a=1&b=2"


async def _account_id(env: Env) -> int:
    async with env.factory() as session:
        return (
            await session.execute(select(Account.id).where(Account.telegram_user_id == OWNER))
        ).scalar_one()


async def _marker(env: Env) -> str | None:
    async with env.factory() as session:
        return (
            await session.execute(
                select(Account.policy_notice_version).where(Account.telegram_user_id == OWNER)
            )
        ).scalar_one()


async def _notice_audit_rows(env: Env) -> int:
    async with env.factory() as session:
        return (
            await session.execute(
                select(func.count(AuditLogEntry.id)).where(
                    AuditLogEntry.event == EVENT_ALERT,
                    AuditLogEntry.details["key"].astext == dedupe_key(),
                )
            )
        ).scalar_one()


async def _consents_recorded_under(env: Env, version: str) -> None:
    async with env.factory() as session, session.begin():
        await session.execute(text("UPDATE app.consents SET policy_version = :v"), {"v": version})


async def _deliver(env: Env, channel: FakeChannel, *, url: str | None = POLICY_URL) -> int:
    alerts = OwnerAlerts(env.factory, channel, env.clock)
    return await deliver_policy_update_notices(env.factory, env.clock, alerts, policy_url=url)


def test_the_current_policy_version_has_a_notice() -> None:
    """A bump that forgets its notice entry would skip the owner notice."""
    assert service.POLICY_VERSION == "v4"
    assert service.POLICY_VERSION in POLICY_CHANGES
    assert "{link}" in POLICY_CHANGES[service.POLICY_VERSION]


async def test_an_owner_who_consented_under_an_older_policy_is_told_once(env: Env) -> None:
    await onboard(env)
    await _consents_recorded_under(env, "v3")
    channel = FakeChannel()

    assert await _deliver(env, channel) == 1
    env.clock.advance(days=2)
    assert await _deliver(env, channel) == 0

    [notice] = channel.sends
    assert "privacy policy was updated (version 4)" in notice
    # It names what v4 changes: storage, not processing.
    assert "a server in the US" in notice
    assert "backed up daily, encrypted, with a copy in the EU" in notice
    assert (
        'Read it here: <a href="https://example.invalid/policy?a=1&amp;b=2">'
        "https://example.invalid/policy?a=1&amp;b=2</a>." in notice
    )
    # Both opt-outs: AI processing, and storage itself.
    assert "<code>/privacy revoke</code> stops all AI processing" in notice
    assert "remove it under Settings → Chat Automation" in notice
    assert await _marker(env) == "v4"
    # Consent is not forced again: it stays granted, under the version agreed to.
    async with env.factory() as session:
        state = await service.active_consent(
            session, await _account_id(env), kind=service.CONSENT_AI_CONTROL
        )
    assert (state.granted, state.policy_version) == (True, "v3")


async def test_the_notice_is_not_repeated_after_its_audit_row_is_swept(env: Env) -> None:
    """Review finding C1: the audit row goes after a year; the notice must not
    come back with it."""
    await onboard(env)
    await _consents_recorded_under(env, "v3")
    channel = FakeChannel()
    assert await _deliver(env, channel) == 1

    env.clock.advance(days=366)
    await sweep_all(env.factory, env.clock)
    assert await _notice_audit_rows(env) == 0

    assert await _deliver(env, channel) == 0
    assert len(channel.sends) == 1


async def test_no_notice_for_a_consent_given_under_the_current_policy(env: Env) -> None:
    await onboard(env)
    channel = FakeChannel()
    assert await _deliver(env, channel) == 0
    assert channel.sends == []
    assert await _marker(env) is None


async def test_an_owner_who_revoked_the_older_consent_is_still_told(env: Env) -> None:
    """Review finding C2: revoking AI does not delete what is stored, and v4
    is about where stored data and its backups are kept."""
    await onboard(env)
    await _consents_recorded_under(env, "v3")
    await deliver(env, private_message(OWNER, "/privacy revoke"))
    channel = FakeChannel()
    assert await _deliver(env, channel) == 1
    assert "privacy policy was updated (version 4)" in channel.sends[0]


async def test_without_a_policy_url_the_notice_points_to_privacy(env: Env) -> None:
    await onboard(env)
    await _consents_recorded_under(env, "v3")
    channel = FakeChannel()
    assert await _deliver(env, channel, url=None) == 1
    assert "Read it here: /privacy." in channel.sends[0]
    assert "<a href" not in channel.sends[0]


async def test_an_undelivered_notice_is_retried_hourly_not_every_minute(env: Env) -> None:
    await onboard(env)
    await _consents_recorded_under(env, "v3")
    channel = FakeChannel(deliver=False)

    assert await _deliver(env, channel) == 0
    assert await _marker(env) is None
    env.clock.advance(minutes=1)
    assert await _deliver(env, channel) == 0
    assert len(channel.sends) == 1  # one attempt, not one per watchdog run

    env.clock.advance(hours=1)
    channel.deliver = True
    assert await _deliver(env, channel) == 1
    env.clock.advance(hours=2)
    assert await _deliver(env, channel) == 0
    assert len(channel.sends) == 2


async def test_an_uncertain_notice_is_never_resent(env: Env) -> None:
    """It may be on screen (timeout): counted as settled (§6 invariant 6)."""
    await onboard(env)
    await _consents_recorded_under(env, "v3")
    channel = FakeChannel(deliver=False, uncertain=True)

    assert await _deliver(env, channel) == 0
    assert await _marker(env) == "v4"

    channel.deliver = True
    env.clock.advance(hours=2)
    assert await _deliver(env, channel) == 0
    assert len(channel.sends) == 1


async def test_a_notice_recorded_but_not_marked_is_settled_without_a_resend(env: Env) -> None:
    """The process stopped between the send and the marker: the delivered
    (or uncertain) audit row settles it."""
    await onboard(env)
    await _consents_recorded_under(env, "v3")
    async with env.factory() as session, session.begin():
        session.add(
            AuditLogEntry(
                account_id=await _account_id(env),
                at=env.clock.now(),
                actor="system",
                event=EVENT_ALERT,
                details={
                    "kind": "policy_updated",
                    "key": dedupe_key(),
                    "delivered": False,
                    "uncertain": True,
                },
            )
        )
    channel = FakeChannel()
    env.clock.advance(hours=2)
    assert await _deliver(env, channel) == 0
    assert channel.sends == []
    assert await _marker(env) == "v4"


async def test_failed_attempts_stop_at_the_cap(env: Env) -> None:
    """A permanently unreachable owner does not get an audit row every hour
    forever."""
    await onboard(env)
    await _consents_recorded_under(env, "v3")
    channel = FakeChannel(deliver=False)

    for _ in range(MAX_FAILED_ATTEMPTS + 6):
        await _deliver(env, channel)
        env.clock.advance(hours=1, minutes=1)

    assert len(channel.sends) == MAX_FAILED_ATTEMPTS
    assert await _notice_audit_rows(env) == MAX_FAILED_ATTEMPTS
    assert await _marker(env) is None


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
        summary_refresher=RecordingRefresher(),
        privacy_policy_url=POLICY_URL,
    )

    await handlers.ingestion_watchdog()
    await handlers.ingestion_watchdog()

    notices = [s for s in channel.sends if "privacy policy was updated" in s]
    assert len(notices) == 1
