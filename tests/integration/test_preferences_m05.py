"""New preferences share the existing transactional audit and Undo path."""

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts import preferences
from assistant.accounts.models import Account, AuditLogEntry
from tests.fakes.clock import FakeClock


async def test_m05_preferences_are_audited_and_undoable(db_session: AsyncSession) -> None:
    account = Account(telegram_user_id=771, status="active", timezone="UTC")
    db_session.add(account)
    await db_session.flush()
    change = await preferences.update(
        db_session,
        account,
        quiet_hours="off",
        digest_time="07:00",
        urgent_bypass=True,
        alert_cap_per_hour=12,
        actor="owner",
        clock=FakeClock(),
    )
    assert change.audit_id is not None
    audit = await db_session.get(AuditLogEntry, change.audit_id)
    assert audit is not None
    assert audit.account_id == account.id
    assert set(audit.details["changes"]) == set(preferences.M05_DEFAULTS)
    assert preferences.current(account)["urgent_bypass"] is True
    reverted = await preferences.revert(db_session, account, audit.details)
    assert set(reverted.restored) == set(preferences.M05_DEFAULTS)
    current = preferences.current(account)
    assert {key: current[key] for key in preferences.M05_DEFAULTS} == preferences.M05_DEFAULTS
