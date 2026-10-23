"""The owner notice for a privacy-policy update (AGENTS §16, policy v4).

A policy version that widens what is processed or kept needs an owner notice
with an opt-out before it takes effect. Consents stay valid across the bump
(nothing is re-asked); instead, every owner who still holds a consent given
under an older version is told once that the policy changed, where to read
it, and how to opt out (`/privacy revoke`).

Runs with `system.ingestion_watchdog`, so it goes out on the first minute of
the deploy that bumps `POLICY_VERSION`. Deduplicated by
`policy_update:<version>` through `OwnerAlerts` (delivered once ever); an
undelivered attempt is retried at most once per `RETRY_AFTER`, never every
minute. Content-free: version, a summary of the change and the policy link.
"""

from __future__ import annotations

import html
from datetime import timedelta

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, AuditLogEntry, Consent
from assistant.accounts.service import POLICY_VERSION
from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.inbox.notify import EVENT_ALERT, AlertKind, OwnerAlerts

#: What changed in each version that needs an owner notice, in one plain
#: phrase. A version bump that widens processing adds its entry here (a unit
#: test keeps the current version listed).
POLICY_CHANGES: dict[str, str] = {
    "v4": "it now says where your data is hosted and adds encrypted backups",
}

#: An undelivered notice (bot blocked, network) is tried again after this.
RETRY_AFTER = timedelta(hours=1)


def dedupe_key(version: str = POLICY_VERSION) -> str:
    return f"policy_update:{version}"


async def _accounts_to_notify(session: AsyncSession, clock: Clock, key: str) -> list[int]:
    """Reachable owners with an active consent recorded against an older
    version, and no delivered (or recent) notice for this version."""
    older_consent = exists().where(
        Consent.account_id == Account.id,
        Consent.revoked_at.is_(None),
        Consent.policy_version != POLICY_VERSION,
    )
    already = exists().where(
        AuditLogEntry.account_id == Account.id,
        AuditLogEntry.event == EVENT_ALERT,
        AuditLogEntry.details["key"].astext == key,
        or_(
            AuditLogEntry.details["delivered"].astext == "true",
            AuditLogEntry.at >= clock.now() - RETRY_AFTER,
        ),
    )
    result = await session.execute(
        select(Account.id)
        .where(Account.control_chat_id.is_not(None), Account.status != "deleted")
        .where(and_(older_consent, ~already))
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
    change = POLICY_CHANGES.get(POLICY_VERSION)
    if change is None:
        return 0
    key = dedupe_key()
    async with transaction(session_factory) as uow:
        account_ids = await _accounts_to_notify(uow.session, clock, key)
    if policy_url:
        url = html.escape(policy_url)
        link = f'Read it here: <a href="{url}">{url}</a>'
    else:
        link = "See /privacy for what is stored."
    delivered = 0
    for account_id in account_ids:
        result = await alerts.notify_owner(
            account_id,
            AlertKind.POLICY_UPDATED,
            dedupe_key=key,
            version=POLICY_VERSION.removeprefix("v"),
            change=change,
            link=link,
        )
        delivered += int(result.sent)
    return delivered
