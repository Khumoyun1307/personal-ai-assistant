"""Automation persistence is account-scoped, validated and idempotent."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account
from assistant.agent.models import AgentRun
from assistant.automation import repository
from assistant.automation.rules import RuleBase
from assistant.core.errors import NotInAccountError
from tests.fakes.clock import FakeClock

NOW = datetime(2026, 10, 27, tzinfo=UTC)


async def seed_account(session: AsyncSession, telegram_id: int) -> Account:
    account = Account(telegram_user_id=telegram_id, status="active", timezone="UTC")
    session.add(account)
    await session.flush()
    return account


async def test_automation_crud_and_terminal_time_are_account_scoped(
    db_session: AsyncSession,
) -> None:
    owner = await seed_account(db_session, 701)
    other = await seed_account(db_session, 702)
    clock = FakeClock(NOW)
    row = await repository.create(
        db_session,
        account_id=owner.id,
        kind="monitor",
        spec=RuleBase(schema_version=1),
        refs={},
        timezone="UTC",
        tz_policy="fixed",
        clock=clock,
    )
    assert (await repository.get(db_session, account_id=owner.id, automation_id=row.id)) is row
    assert await repository.get(db_session, account_id=other.id, automation_id=row.id) is None
    assert await repository.list_active(db_session, account_id=other.id) == []
    assert not await repository.set_status(
        db_session, account_id=other.id, automation_id=row.id, status="cancelled", clock=clock
    )
    assert await repository.set_status(
        db_session, account_id=owner.id, automation_id=row.id, status="cancelled", clock=clock
    )
    await db_session.refresh(row)
    assert row.ended_at == NOW
    assert await repository.list_active(db_session, account_id=owner.id) == []


async def test_an_automation_rejects_foreign_reference_ids(db_session: AsyncSession) -> None:
    owner = await seed_account(db_session, 703)
    with pytest.raises(NotInAccountError):
        await repository.create(
            db_session,
            account_id=owner.id,
            kind="monitor",
            spec=RuleBase(schema_version=1),
            refs={"conversation_ids": [99999999]},
            timezone="UTC",
            tz_policy="fixed",
            clock=FakeClock(NOW),
        )


async def test_pause_and_resume_preserve_an_automation_schedule(db_session: AsyncSession) -> None:
    owner = await seed_account(db_session, 704)
    clock = FakeClock(NOW)
    row = await repository.create(
        db_session,
        account_id=owner.id,
        kind="digest",
        spec=RuleBase(schema_version=1),
        refs={},
        timezone="UTC",
        tz_policy="follow_owner",
        clock=clock,
        next_run_at=NOW + timedelta(hours=1),
    )
    for status in ("paused", "active"):
        assert await repository.set_status(
            db_session, account_id=owner.id, automation_id=row.id, status=status, clock=clock
        )
        await db_session.refresh(row)
        assert row.ended_at is None
        assert row.next_run_at == NOW + timedelta(hours=1)


async def test_automation_create_is_idempotent_for_a_tool_call(db_session: AsyncSession) -> None:
    owner = await seed_account(db_session, 705)
    run = AgentRun(account_id=owner.id, run_type="owner_request", model="synthetic-model")
    db_session.add(run)
    await db_session.flush()
    first = await repository.create(
        db_session,
        account_id=owner.id,
        kind="monitor",
        spec=RuleBase(schema_version=1),
        refs={},
        timezone="UTC",
        tz_policy="fixed",
        clock=FakeClock(NOW),
        run_id=run.id,
        tool_call_id="synthetic-call",
    )
    again = await repository.create(
        db_session,
        account_id=owner.id,
        kind="monitor",
        spec=RuleBase(schema_version=1),
        refs={},
        timezone="UTC",
        tz_policy="fixed",
        clock=FakeClock(NOW),
        run_id=run.id,
        tool_call_id="synthetic-call",
    )
    assert first.id == again.id
    other = await seed_account(db_session, 706)
    with pytest.raises(NotInAccountError):
        await repository.create(
            db_session,
            account_id=other.id,
            kind="monitor",
            spec=RuleBase(schema_version=1),
            refs={},
            timezone="UTC",
            tz_policy="fixed",
            clock=FakeClock(NOW),
            run_id=run.id,
            tool_call_id="synthetic-call",
        )
