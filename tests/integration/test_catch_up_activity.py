"""Integration tests for S04.5 catch-up assistant activity (PRD J3, FR-ACT-04)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from assistant.actions.states import ActionState
from assistant.agent.tools.registry import ToolContext, ToolServices
from assistant.agent.tools.summaries import SummarizePeriodArgs, summarize_period_tool
from assistant.conversations.models import Contact, Message
from tests.fakes.clock import FakeClock
from tests.integration.business_env import Env
from tests.integration.p2_factories import (
    NOW,
    seed_account,
    seed_action,
    seed_conversation,
)

pytestmark = pytest.mark.integration


async def test_catch_up_lists_what_the_assistant_sent_or_drafted_in_the_period(env: Env) -> None:
    async with env.factory() as session:
        account = await seed_account(session)
        contact = Contact(
            account_id=account.id,
            telegram_user_id=1_111_111_111,
            display_name="Dilnoza",
            first_seen_at=NOW,
            updated_at=NOW,
        )
        session.add(contact)
        await session.flush()
        conv = await seed_conversation(session, account_id=account.id, contact_id=contact.id)

        # Message in period so conversation is active
        msg = Message(
            account_id=account.id,
            conversation_id=conv.id,
            direction="contact",
            content_type="text",
            text="Salom!",
            sent_at=NOW - timedelta(hours=2),
            created_at=NOW - timedelta(hours=2),
            updated_at=NOW - timedelta(hours=2),
        )
        session.add(msg)

        # 1 sent action
        await seed_action(
            session,
            account_id=account.id,
            conversation_id=conv.id,
            state=ActionState.SUCCEEDED.value,
            text="Assalomu alaykum!",
            created_at=NOW - timedelta(hours=1, minutes=50),
            executed_at=NOW - timedelta(hours=1, minutes=49),
        )

        # 1 awaiting draft action
        await seed_action(
            session,
            account_id=account.id,
            conversation_id=conv.id,
            state=ActionState.AWAITING_APPROVAL.value,
            text="Draft awaiting approval",
            created_at=NOW - timedelta(minutes=30),
        )
        await session.commit()

    ctx = ToolContext(
        account_id=account.id,
        run_id=1,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(
            session_factory=env.factory,
            clock=FakeClock(NOW),
            monthly_budget_usd=None,
        ),
    )
    outcome = await summarize_period_tool(
        ctx,
        SummarizePeriodArgs(
            since=(NOW - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"),
            until=None,
            conversation_id=None,
            contact_id=None,
        ),
    )
    assert outcome.status == "ok"
    activity = outcome.data["assistant_activity"]
    assert len(activity) == 1
    entry = activity[0]
    assert entry["conversation_id"] == conv.id
    assert entry["contact_id"] == contact.id
    assert entry["contact_name"].startswith('<untrusted source="contact_name"')
    assert "Dilnoza" in entry["contact_name"]
    assert entry["sent"] == 1
    assert entry["awaiting"] == 1
    assert entry["not_sent"] == 0
    assert entry["problems"] == 0
    assert entry["deleted_after_send"] == 0


async def test_catch_up_activity_is_empty_when_nothing_happened(env: Env) -> None:
    async with env.factory() as session:
        account = await seed_account(session)
        conv = await seed_conversation(session, account_id=account.id)
        msg = Message(
            account_id=account.id,
            conversation_id=conv.id,
            direction="contact",
            content_type="text",
            text="Any news?",
            sent_at=NOW - timedelta(hours=2),
            created_at=NOW - timedelta(hours=2),
            updated_at=NOW - timedelta(hours=2),
        )
        session.add(msg)
        await session.commit()

    ctx = ToolContext(
        account_id=account.id,
        run_id=1,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(
            session_factory=env.factory,
            clock=FakeClock(NOW),
            monthly_budget_usd=None,
        ),
    )
    outcome = await summarize_period_tool(
        ctx,
        SummarizePeriodArgs(
            since=(NOW - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"),
            until=None,
            conversation_id=None,
            contact_id=None,
        ),
    )
    assert outcome.status == "ok"
    assert outcome.data["assistant_activity"] == []


async def test_rg21_catch_up_activity_never_includes_another_account(env: Env) -> None:
    async with env.factory() as session:
        acc1 = await seed_account(session)
        acc2 = await seed_account(session)
        conv2 = await seed_conversation(session, account_id=acc2.id)

        # Acc 2 has an action in the period
        await seed_action(
            session,
            account_id=acc2.id,
            conversation_id=conv2.id,
            state=ActionState.SUCCEEDED.value,
            text="Acc 2 action",
            created_at=NOW - timedelta(hours=1),
            executed_at=NOW - timedelta(hours=1),
        )
        await session.commit()

    ctx = ToolContext(
        account_id=acc1.id,
        run_id=1,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(
            session_factory=env.factory,
            clock=FakeClock(NOW),
            monthly_budget_usd=None,
        ),
    )
    outcome = await summarize_period_tool(
        ctx,
        SummarizePeriodArgs(
            since=(NOW - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"),
            until=None,
            conversation_id=None,
            contact_id=None,
        ),
    )
    assert outcome.status == "ok"
    assert outcome.data["assistant_activity"] == []


async def test_catch_up_activity_counts_sent_messages_deleted_later(env: Env) -> None:
    async with env.factory() as session:
        account = await seed_account(session)
        conv = await seed_conversation(session, account_id=account.id)

        act = await seed_action(
            session,
            account_id=account.id,
            conversation_id=conv.id,
            state=ActionState.SUCCEEDED.value,
            text="Sent then deleted",
            created_at=NOW - timedelta(hours=1),
            executed_at=NOW - timedelta(hours=1),
        )
        msg = Message(
            account_id=account.id,
            conversation_id=conv.id,
            action_id=act.id,
            direction="assistant_sent",
            content_type="text",
            text="Sent then deleted",
            sent_at=NOW - timedelta(hours=1),
            deleted_at=NOW - timedelta(minutes=10),
            created_at=NOW - timedelta(hours=1),
            updated_at=NOW - timedelta(minutes=10),
        )
        session.add(msg)
        await session.commit()

    ctx = ToolContext(
        account_id=account.id,
        run_id=1,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(
            session_factory=env.factory,
            clock=FakeClock(NOW),
            monthly_budget_usd=None,
        ),
    )
    outcome = await summarize_period_tool(
        ctx,
        SummarizePeriodArgs(
            since=(NOW - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M"),
            until=None,
            conversation_id=None,
            contact_id=None,
        ),
    )
    assert outcome.status == "ok"
    activity = outcome.data["assistant_activity"]
    assert len(activity) == 1
    entry = activity[0]
    assert entry["sent"] == 1
    assert entry["deleted_after_send"] == 1
