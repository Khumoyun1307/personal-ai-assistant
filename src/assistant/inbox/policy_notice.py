"""The owner notice for a privacy-policy update (AGENTS §16, policy v4).

A policy version that widens what is processed or kept needs an owner notice
with an opt-out before it takes effect. Consents stay valid across the bump
(nothing is re-asked); instead, every owner with a consent recorded under an
older version — active or revoked: a revoked owner's data is still stored and
backed up — is told once what changed, where to read it, and how to opt out.

Runs with `system.ingestion_watchdog`, so it goes out on the first minute of
the deploy that bumps `POLICY_VERSION`. "Once" is durable:
`accounts.policy_notice_version` records the version whose notice is settled
— delivered, or sent with an unknown outcome (it may be on screen, so it is
never resent, §6 invariant 6). The `audit_log` rows written by `OwnerAlerts`
are kept as the record, but they are swept after a year, so they cannot be
the dedupe. An undelivered attempt is retried at most once per
`RETRY_AFTER`, and at most `MAX_FAILED_ATTEMPTS` times per version.
Content-free: the version's fixed text and the policy link.
"""

from __future__ import annotations

import html
from datetime import timedelta

from sqlalchemy import ColumnElement, and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, AuditLogEntry, Consent
from assistant.accounts.service import POLICY_VERSION
from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.inbox.notify import EVENT_ALERT, AlertKind, OwnerAlerts

#: The notice for each version that needs one: what that version changes and
#: how to opt out of it, in plain words. `{link}` is the policy link (or
#: `/privacy` when no URL is configured). A version bump that widens
#: processing adds its entry here (a unit test keeps the current one listed).
POLICY_CHANGES: dict[str, str] = {
    "v4": (
        "Alviro's privacy policy was updated (version 4). It now says where your "
        "data is kept — a server in the US — and that it is backed up daily, "
        "encrypted, with a copy in the EU. Your settings stay as they are. Read it "
        "here: {link}. If you don't agree: <code>/privacy revoke</code> stops all AI "
        "processing, and to stop Alviro saving your chats, remove it under "
        "Settings → Chat Automation."
    ),
}

#: An undelivered notice (bot blocked, network) is tried again after this.
RETRY_AFTER = timedelta(hours=1)
#: Then it stops: a day of hourly refusals, so a permanently unreachable owner
#: does not add an audit row every hour forever.
MAX_FAILED_ATTEMPTS = 24


def dedupe_key(version: str = POLICY_VERSION) -> str:
    return f"policy_update:{version}"


def _unsettled() -> ColumnElement[bool]:
    return Account.policy_notice_version.is_distinct_from(POLICY_VERSION)


async def _settle(session: AsyncSession, account_ids: list[int]) -> None:
    if account_ids:
        await session.execute(
            update(Account)
            .where(Account.id.in_(account_ids))
            .values(policy_notice_version=POLICY_VERSION)
        )


async def _settle_recorded_outcomes(session: AsyncSession, key: str) -> None:
    """Mark accounts whose audit trail already shows a delivered or uncertain
    notice (the process stopped between the send and the marker)."""
    settled = exists().where(
        AuditLogEntry.account_id == Account.id,
        AuditLogEntry.event == EVENT_ALERT,
        AuditLogEntry.details["key"].astext == key,
        or_(
            AuditLogEntry.details["delivered"].astext == "true",
            AuditLogEntry.details["uncertain"].astext == "true",
        ),
    )
    await session.execute(
        update(Account).where(_unsettled(), settled).values(policy_notice_version=POLICY_VERSION)
    )


async def _accounts_to_notify(session: AsyncSession, clock: Clock, key: str) -> list[int]:
    """Reachable owners with any consent recorded against an older version,
    whose notice for this version is not settled, not attempted within
    `RETRY_AFTER`, and not given up on."""
    older_consent = exists().where(
        Consent.account_id == Account.id,
        Consent.policy_version != POLICY_VERSION,
    )
    attempts = (
        AuditLogEntry.account_id == Account.id,
        AuditLogEntry.event == EVENT_ALERT,
        AuditLogEntry.details["key"].astext == key,
    )
    recent_attempt = exists().where(*attempts, AuditLogEntry.at >= clock.now() - RETRY_AFTER)
    failed_attempts = (
        select(func.count(AuditLogEntry.id))
        .where(*attempts, AuditLogEntry.details["delivered"].astext == "false")
        .scalar_subquery()
    )
    result = await session.execute(
        select(Account.id)
        .where(Account.control_chat_id.is_not(None), Account.status != "deleted")
        .where(_unsettled())
        .where(and_(older_consent, ~recent_attempt, failed_attempts < MAX_FAILED_ATTEMPTS))
        .order_by(Account.id)
    )
    return list(result.scalars().all())


async def deliver_policy_update_notices(
    session_factory: async_sessionmaker[AsyncSession],
    clock: Clock,
    alerts: OwnerAlerts,
    *,
    policy_url: str | None,
) -> int:
    """Send the notice to every owner who has not had it. Returns how many
    were delivered."""
    template = POLICY_CHANGES.get(POLICY_VERSION)
    if template is None:
        return 0
    key = dedupe_key()
    async with transaction(session_factory) as uow:
        await _settle_recorded_outcomes(uow.session, key)
        account_ids = await _accounts_to_notify(uow.session, clock, key)
    if policy_url:
        url = html.escape(policy_url)
        link = f'<a href="{url}">{url}</a>'
    else:
        link = "/privacy"
    body = template.format(link=link)
    delivered = 0
    for account_id in account_ids:
        result = await alerts.notify_owner(
            account_id, AlertKind.POLICY_UPDATED, dedupe_key=key, body=body
        )
        delivered += int(result.sent)
        # "duplicate": an earlier run delivered it but stopped before the
        # marker. Uncertain: it may be on screen; never resend it.
        if result.sent or result.uncertain or result.reason == "duplicate":
            async with transaction(session_factory) as uow:
                await _settle(uow.session, [account_id])
    return delivered
