"""Replaying the observed connection lifecycle through the registry (S02.1).

The 2026-10-18 probe session (TF §12 LV-01/05/12) produced, in order: a
connection with full rights, repeats, rights edits, all rights off, rights
back, a disconnect, and a reconnect with a new id and a later `date` that
starts at no rights. The registry must turn exactly that into the history
below — sanitised real payloads, real Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts import connections
from assistant.accounts.connections import Change, Source
from assistant.accounts.models import Account, BusinessConnection
from assistant.telegram.connections import parse_connection
from tests.contract.test_telegram_fixtures import connection_updates, lookups
from tests.fakes.clock import FakeClock

pytestmark = pytest.mark.integration

EXPECTED = [
    Change.CONNECTED,
    Change.UNCHANGED,
    Change.UNCHANGED,
    Change.UNCHANGED,
    Change.RIGHTS_CHANGED,  # one delete right off
    Change.RIGHTS_CHANGED,  # back on
    Change.RIGHTS_CHANGED,  # all rights off (LV-12): still connected
    Change.UNCHANGED,
    Change.RIGHTS_CHANGED,  # all back
    Change.DISABLED,  # disconnect: same id, is_enabled=false (LV-01)
    Change.RECONNECTED,  # new id, later date, rights=[] (LV-05)
    Change.UNCHANGED,
    Change.RIGHTS_CHANGED,
]


async def onboarded_owner(session: AsyncSession, telegram_user_id: int) -> Account:
    account = Account(telegram_user_id=telegram_user_id, status="active", timezone="UTC")
    session.add(account)
    await session.flush()
    return account


async def test_the_observed_lifecycle_replays_into_the_expected_history(
    db_session: AsyncSession,
) -> None:
    updates = connection_updates()
    owner_id = updates[0]["user"]["id"]
    await onboarded_owner(db_session, owner_id)
    clock = FakeClock(datetime(2026, 10, 18, 20, 0, tzinfo=UTC))

    changes = [
        (
            await connections.apply_snapshot(
                db_session,
                parse_connection(update),
                owner_ids=frozenset({owner_id}),
                source=Source.UPDATE,
                clock=clock,
            )
        ).change
        for update in updates
    ]

    assert changes == EXPECTED
    current = (await db_session.execute(select(BusinessConnection))).scalar_one()
    assert current.connection_id == updates[-1]["id"]
    assert (current.status, current.capability_tier) == ("active", "extended")


async def test_bootstrapping_the_replaced_id_does_not_undo_the_reconnect(
    db_session: AsyncSession,
) -> None:
    """After the reconnect, `getBusinessConnection(old id)` returned the old
    connection as disabled with its old date. Seen by a bootstrap, that must
    stay history."""
    updates = connection_updates()
    owner_id = updates[0]["user"]["id"]
    await onboarded_owner(db_session, owner_id)
    clock = FakeClock(datetime(2026, 10, 18, 20, 0, tzinfo=UTC))
    owners = frozenset({owner_id})
    for update in updates:
        await connections.apply_snapshot(
            db_session,
            parse_connection(update),
            owner_ids=owners,
            source=Source.UPDATE,
            clock=clock,
        )

    old_lookup = lookups()[-1]
    assert old_lookup["is_enabled"] is False
    result = await connections.apply_snapshot(
        db_session,
        parse_connection(old_lookup),
        owner_ids=owners,
        source=Source.BOOTSTRAP,
        clock=clock,
    )

    assert result.change is Change.STALE_EVENT
    current = (await db_session.execute(select(BusinessConnection))).scalar_one()
    assert current.connection_id == updates[-1]["id"]
    assert current.status == "active"
