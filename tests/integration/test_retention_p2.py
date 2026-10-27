"""F0 P2 retention periods, boundaries and account overrides (synthetic data)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from assistant.accounts.models import Account
from assistant.actions.models import Action
from assistant.automation.models import Automation, AutomationOccurrence
from assistant.inbox.models import ConversationTriage, Digest, Notification, Task
from assistant.privacy.retention import sweep_all
from tests.integration.business_env import Env
from tests.integration.test_action_lifecycle_bulk import seed_actions

pytestmark = pytest.mark.integration


async def test_action_text_retention_uses_account_period_and_keeps_executing(env: Env) -> None:
    account_id, _, ids = await seed_actions(env)
    other_account, _, _ = await seed_actions(env, user_id=7000080002)
    async with env.factory() as session, session.begin():
        account = await session.get(Account, account_id)
        assert account is not None
        account.settings = {"retention_days": {"messages": 14}}
        for action in await session.scalars(select(Action)):
            action.created_at = env.clock.now() - timedelta(days=15)
    report = await sweep_all(env.factory, env.clock)
    assert report.action_texts == len(ids) - 1
    async with env.factory() as session:
        for action in await session.scalars(select(Action).where(Action.account_id == account_id)):
            assert (action.text is not None) == (action.state == "executing")
        assert all(
            action.text is not None
            for action in await session.scalars(
                select(Action).where(Action.account_id == other_account)
            )
        )


async def test_action_rows_expire_after_a_year_only_if_settled(env: Env) -> None:
    account_id, _, ids = await seed_actions(env)
    async with env.factory() as session, session.begin():
        for action in await session.scalars(select(Action).where(Action.account_id == account_id)):
            action.created_at = env.clock.now() - timedelta(days=366)
    report = await sweep_all(env.factory, env.clock)
    assert report.actions == 8  # seven terminal states plus delivery_uncertain
    async with env.factory() as session:
        remaining = set(
            await session.scalars(select(Action.state).where(Action.account_id == account_id))
        )
    assert remaining == {"proposed", "awaiting_approval", "approved", "scheduled", "executing"}
    assert len(ids) == len(remaining) + report.actions


async def test_tasks_keep_open_rows_and_delete_old_closed_rows(env: Env) -> None:
    account_id, conversation_id, _ = await seed_actions(env)
    async with env.factory() as session, session.begin():
        for status, days in (("open", 500), ("done", 366), ("cancelled", 366), ("expired", 365)):
            session.add(
                Task(
                    account_id=account_id,
                    conversation_id=conversation_id,
                    kind="open_question",
                    title="synthetic task",
                    created_by="agent",
                    status=status,
                    closed_at=env.clock.now() - timedelta(days=days),
                )
            )
    report = await sweep_all(env.factory, env.clock)
    assert report.tasks == 2
    async with env.factory() as session:
        assert set(await session.scalars(select(Task.status))) == {"open", "expired"}


async def test_triage_retention_uses_the_accounts_message_period(env: Env) -> None:
    account_id, conversation_id, _ = await seed_actions(env)
    async with env.factory() as session, session.begin():
        account = await session.get(Account, account_id)
        assert account is not None
        account.settings = {"retention_days": {"messages": 14}}
        session.add(
            ConversationTriage(
                account_id=account_id,
                conversation_id=conversation_id,
                covers_through_message_id=1,
                needs_reply=True,
                contains_question=True,
                requests_decision=False,
                urgency="normal",
                escalation_category="none",
                reasons=["synthetic reason"],
                prompt_version="synthetic.v1",
                model="synthetic-model",
                updated_at=env.clock.now() - timedelta(days=15),
            )
        )
    assert (await sweep_all(env.factory, env.clock)).triage_verdicts == 1


async def test_automations_delete_ended_rows_after_a_year_with_occurrences(env: Env) -> None:
    account_id, _, _ = await seed_actions(env)
    async with env.factory() as session, session.begin():
        for status in ("active", "paused", "completed", "cancelled", "expired"):
            row = Automation(
                account_id=account_id,
                kind="monitor",
                spec={"schema_version": 1},
                timezone="UTC",
                tz_policy="fixed",
                status=status,
                ended_at=env.clock.now() - timedelta(days=366),
            )
            session.add(row)
            await session.flush()
            session.add(
                AutomationOccurrence(
                    account_id=account_id,
                    automation_id=row.id,
                    occurrence_at=env.clock.now(),
                    status="fired",
                    created_at=env.clock.now(),
                )
            )
    assert (await sweep_all(env.factory, env.clock)).automations == 3
    async with env.factory() as session:
        assert set(await session.scalars(select(Automation.status))) == {"active", "paused"}
        assert len(list(await session.scalars(select(AutomationOccurrence)))) == 2


async def test_occurrences_and_digests_expire_after_a_year(env: Env) -> None:
    account_id, _, _ = await seed_actions(env)
    old = env.clock.now() - timedelta(days=366)
    async with env.factory() as session, session.begin():
        row = Automation(
            account_id=account_id,
            kind="digest",
            spec={"schema_version": 1},
            timezone="UTC",
            tz_policy="follow_owner",
        )
        session.add(row)
        await session.flush()
        session.add(
            AutomationOccurrence(
                account_id=account_id,
                automation_id=row.id,
                occurrence_at=old,
                status="fired",
                created_at=old,
            )
        )
        session.add(
            Digest(
                account_id=account_id,
                period_start=old,
                period_end=old + timedelta(days=1),
                status="sent",
                created_at=old,
            )
        )
    report = await sweep_all(env.factory, env.clock)
    assert report.occurrences == report.digests == 1


async def test_notifications_expire_after_thirty_days(env: Env) -> None:
    account_id, _, _ = await seed_actions(env)
    async with env.factory() as session, session.begin():
        for days in (30, 31):
            session.add(
                Notification(
                    account_id=account_id,
                    kind="system",
                    priority="normal",
                    dedupe_key=f"synthetic:{days}",
                    created_at=env.clock.now() - timedelta(days=days),
                )
            )
    assert (await sweep_all(env.factory, env.clock)).notifications == 1
    async with env.factory() as session:
        assert list(await session.scalars(select(Notification.dedupe_key))) == ["synthetic:30"]
