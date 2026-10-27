"""Integration tests for S04.5: activity read model, /activity command, and get_activity tool."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import AuditLogEntry
from assistant.actions.activity import Authorization, activity
from assistant.actions.states import ActionState
from assistant.agent.tools.activity import (
    STATUS_RULE,
    GetActivityArgs,
    get_activity_tool,
)
from assistant.agent.tools.registry import (
    CHAT_ACCESS_OFF,
    CHAT_ACCESS_OFF_REASON,
    CapabilityTier,
    ToolContext,
    ToolServices,
    default_registry,
)
from assistant.conversations.models import Contact, Message
from assistant.core.errors import NotInAccountError
from assistant.telegram.control.activity import handle_activity
from assistant.telegram.control.handlers import IncomingMessage
from assistant.telegram.gateway import OutboundGateway
from tests.fakes.clock import FakeClock
from tests.fakes.sender import FakeTelegramSender
from tests.integration.business_env import Env
from tests.integration.p2_factories import (
    NOW,
    seed_account,
    seed_action,
    seed_conversation,
)

pytestmark = pytest.mark.integration


async def test_read_model_lists_every_seeded_action_in_the_period(
    db_session: AsyncSession,
) -> None:
    account = await seed_account(db_session)

    # Seed one action for each state (each in its own conversation for ux_actions_live_card)
    states = list(ActionState)
    actions = []
    base_time = NOW - timedelta(hours=len(states))
    for i, state in enumerate(states):
        conv = await seed_conversation(db_session, account_id=account.id)
        t = base_time + timedelta(hours=i)
        action = await seed_action(
            db_session,
            account_id=account.id,
            conversation_id=conv.id,
            state=state.value,
            text=f"Reply for state {state.value}",
            created_at=t,
            decided_at=t if state in (ActionState.APPROVED, ActionState.REJECTED) else None,
            executed_at=t if state == ActionState.SUCCEEDED else None,
        )
        actions.append(action)

    # Also seed an action outside the period (before since)
    conv_out = await seed_conversation(db_session, account_id=account.id)
    await seed_action(
        db_session,
        account_id=account.id,
        conversation_id=conv_out.id,
        state=ActionState.PROPOSED.value,
        text="Outside period",
        created_at=base_time - timedelta(days=2),
    )

    since = base_time - timedelta(minutes=10)
    until = NOW + timedelta(minutes=10)

    entries = await activity(db_session, account_id=account.id, since=since, until=until)

    assert len(entries) == len(states)
    returned_action_ids = [e.action_id for e in entries]
    assert returned_action_ids == [a.id for a in actions]
    for entry in entries:
        assert entry.contact_display_name == "Synthetic contact"


async def test_read_model_groups_drafts_and_sent_messages_differently(
    db_session: AsyncSession,
) -> None:
    account = await seed_account(db_session)
    conv = await seed_conversation(db_session, account_id=account.id)

    sent_act = await seed_action(
        db_session,
        account_id=account.id,
        conversation_id=conv.id,
        state=ActionState.SUCCEEDED.value,
        text="Sent message text",
        created_at=NOW - timedelta(minutes=20),
        executed_at=NOW - timedelta(minutes=15),
    )
    draft_act = await seed_action(
        db_session,
        account_id=account.id,
        conversation_id=conv.id,
        state=ActionState.AWAITING_APPROVAL.value,
        text="Draft awaiting approval",
        created_at=NOW - timedelta(minutes=10),
    )
    not_sent_act = await seed_action(
        db_session,
        account_id=account.id,
        conversation_id=conv.id,
        state=ActionState.REJECTED.value,
        state_reason="owner_rejected",
        text="Rejected draft",
        created_at=NOW - timedelta(minutes=5),
        decided_at=NOW - timedelta(minutes=5),
    )

    entries = await activity(
        db_session,
        account_id=account.id,
        since=NOW - timedelta(hours=1),
        until=NOW + timedelta(hours=1),
    )

    by_id = {e.action_id: e for e in entries}
    assert by_id[sent_act.id].group == "sent"
    assert by_id[draft_act.id].group == "awaiting"
    assert by_id[not_sent_act.id].group == "not_sent"
    assert by_id[not_sent_act.id].state_reason == "owner_rejected"


async def test_read_model_names_who_approved_which_version_and_when(
    db_session: AsyncSession,
) -> None:
    account = await seed_account(db_session)
    conv = await seed_conversation(db_session, account_id=account.id)

    act = await seed_action(
        db_session,
        account_id=account.id,
        conversation_id=conv.id,
        state=ActionState.SUCCEEDED.value,
        text="Approved and sent reply",
        version=2,
        created_at=NOW - timedelta(minutes=30),
        executed_at=NOW - timedelta(minutes=10),
    )

    approved_time = NOW - timedelta(minutes=12)
    audit = AuditLogEntry(
        account_id=account.id,
        actor="owner",
        event="action_approved",
        subject_type="action",
        subject_id=act.id,
        details={"version": 2},
        at=approved_time,
    )
    db_session.add(audit)
    await db_session.flush()

    entries = await activity(
        db_session,
        account_id=account.id,
        since=NOW - timedelta(hours=1),
        until=NOW + timedelta(hours=1),
    )

    [entry] = entries
    assert entry.action_id == act.id
    assert entry.authorization == Authorization(
        kind="owner_approval",
        approved_at=approved_time,
        version=2,
    )


async def test_rg21_read_model_never_returns_another_accounts_actions(
    db_session: AsyncSession,
) -> None:
    acc1 = await seed_account(db_session)
    acc2 = await seed_account(db_session)

    conv1 = await seed_conversation(db_session, account_id=acc1.id)
    conv2 = await seed_conversation(db_session, account_id=acc2.id)

    await seed_action(
        db_session,
        account_id=acc1.id,
        conversation_id=conv1.id,
        state=ActionState.SUCCEEDED.value,
        text="Account 1 send",
        created_at=NOW,
    )
    await seed_action(
        db_session,
        account_id=acc2.id,
        conversation_id=conv2.id,
        state=ActionState.SUCCEEDED.value,
        text="Account 2 send",
        created_at=NOW,
    )

    entries1 = await activity(
        db_session,
        account_id=acc1.id,
        since=NOW - timedelta(hours=1),
        until=NOW + timedelta(hours=1),
    )
    assert len(entries1) == 1
    assert entries1[0].conversation_id == conv1.id

    entries2 = await activity(
        db_session,
        account_id=acc2.id,
        since=NOW - timedelta(hours=1),
        until=NOW + timedelta(hours=1),
    )
    assert len(entries2) == 1
    assert entries2[0].conversation_id == conv2.id


async def test_read_model_shows_no_excerpt_for_a_purged_text(db_session: AsyncSession) -> None:
    account = await seed_account(db_session)
    conv = await seed_conversation(db_session, account_id=account.id)

    act_with_text = await seed_action(
        db_session,
        account_id=account.id,
        conversation_id=conv.id,
        state=ActionState.SUCCEEDED.value,
        text="Hello " * 30,  # > 120 chars
        created_at=NOW - timedelta(minutes=10),
    )
    act_purged = await seed_action(
        db_session,
        account_id=account.id,
        conversation_id=conv.id,
        state=ActionState.SUCCEEDED.value,
        text=None,
        created_at=NOW - timedelta(minutes=5),
    )

    entries = await activity(
        db_session,
        account_id=account.id,
        since=NOW - timedelta(hours=1),
        until=NOW + timedelta(hours=1),
    )

    by_id = {e.action_id: e for e in entries}
    assert by_id[act_purged.id].excerpt is None
    assert by_id[act_with_text.id].excerpt == ("Hello " * 30)[:120]
    assert len(by_id[act_with_text.id].excerpt) == 120


async def test_a_sent_message_deleted_in_the_chat_is_listed_with_its_deletion_time(
    db_session: AsyncSession,
) -> None:
    account = await seed_account(db_session)
    conv = await seed_conversation(db_session, account_id=account.id)

    act = await seed_action(
        db_session,
        account_id=account.id,
        conversation_id=conv.id,
        state=ActionState.SUCCEEDED.value,
        text="Sent then deleted in chat",
        created_at=NOW - timedelta(minutes=20),
        executed_at=NOW - timedelta(minutes=15),
    )

    deletion_time = NOW - timedelta(minutes=5)
    msg = Message(
        account_id=account.id,
        conversation_id=conv.id,
        action_id=act.id,
        direction="assistant_sent",
        content_type="text",
        text="Sent then deleted in chat",
        sent_at=NOW - timedelta(minutes=15),
        deleted_at=deletion_time,
        created_at=NOW - timedelta(minutes=15),
        updated_at=NOW - timedelta(minutes=5),
    )
    db_session.add(msg)
    await db_session.flush()

    entries = await activity(
        db_session,
        account_id=account.id,
        since=NOW - timedelta(hours=1),
        until=NOW + timedelta(hours=1),
    )

    [entry] = entries
    assert entry.action_id == act.id
    assert entry.deleted_in_chat_at == deletion_time


async def test_command_today_uses_the_owners_timezone(db_session: AsyncSession) -> None:
    # Account in Asia/Tashkent (UTC+5)
    account = await seed_account(db_session, timezone="Asia/Tashkent")
    conv = await seed_conversation(db_session, account_id=account.id)

    # 2026-10-27 20:00 UTC = 2026-10-28 01:00 Tashkent
    clock = FakeClock(datetime(2026, 10, 27, 20, 0, tzinfo=UTC))
    sender = FakeTelegramSender()
    gateway = OutboundGateway(sender, clock)

    # Action 1 at 2026-10-27 18:00 UTC = 2026-10-27 23:00 Tashkent (yesterday in Tashkent!)
    await seed_action(
        db_session,
        account_id=account.id,
        conversation_id=conv.id,
        state=ActionState.SUCCEEDED.value,
        text="Yesterday action in Tashkent",
        created_at=datetime(2026, 10, 27, 18, 0, tzinfo=UTC),
        executed_at=datetime(2026, 10, 27, 18, 0, tzinfo=UTC),
    )

    # Action 2 at 2026-10-27 19:30 UTC = 2026-10-28 00:30 Tashkent (today in Tashkent!)
    conv2 = await seed_conversation(db_session, account_id=account.id)
    await seed_action(
        db_session,
        account_id=account.id,
        conversation_id=conv2.id,
        state=ActionState.SUCCEEDED.value,
        text="Today action in Tashkent",
        created_at=datetime(2026, 10, 27, 19, 30, tzinfo=UTC),
        executed_at=datetime(2026, 10, 27, 19, 30, tzinfo=UTC),
    )

    msg = IncomingMessage(
        account_id=account.id,
        chat_id=account.control_chat_id,
        telegram_message_id=10,
        text="/activity",
        from_id=account.telegram_user_id,
    )
    result = await handle_activity(db_session, gateway, clock, None, account, msg, "")

    assert result.handled is True
    assert len(sender.sends) == 1
    sent_text = sender.sends[0]["text"]
    assert "Today action in Tashkent" in sent_text
    assert "Yesterday action in Tashkent" not in sent_text
    assert "00:30" in sent_text


async def test_command_with_an_unknown_argument_replies_with_usage(
    db_session: AsyncSession,
) -> None:
    account = await seed_account(db_session)
    clock = FakeClock(NOW)
    sender = FakeTelegramSender()
    gateway = OutboundGateway(sender, clock)

    msg = IncomingMessage(
        account_id=account.id,
        chat_id=account.control_chat_id,
        telegram_message_id=11,
        text="/activity invalid",
        from_id=account.telegram_user_id,
    )
    result = await handle_activity(db_session, gateway, clock, None, account, msg, "invalid")

    assert result.handled is False
    assert result.note == "usage"
    assert len(sender.sends) == 1
    assert "Usage: /activity [today|yesterday|7d]" in sender.sends[0]["text"]


async def test_command_escapes_names_and_excerpts(db_session: AsyncSession) -> None:
    account = await seed_account(db_session)
    contact = Contact(
        account_id=account.id,
        telegram_user_id=8_888_888_888,
        display_name="<Evil & Co>",
        first_seen_at=NOW,
        updated_at=NOW,
    )
    db_session.add(contact)
    await db_session.flush()

    conv = await seed_conversation(db_session, account_id=account.id, contact_id=contact.id)
    await seed_action(
        db_session,
        account_id=account.id,
        conversation_id=conv.id,
        state=ActionState.SUCCEEDED.value,
        text="Danger <script>alert(1)</script> & 'quotes'",
        created_at=NOW - timedelta(minutes=10),
        executed_at=NOW - timedelta(minutes=5),
    )

    clock = FakeClock(NOW)
    sender = FakeTelegramSender()
    gateway = OutboundGateway(sender, clock)

    msg = IncomingMessage(
        account_id=account.id,
        chat_id=account.control_chat_id,
        telegram_message_id=12,
        text="/activity today",
        from_id=account.telegram_user_id,
    )
    result = await handle_activity(db_session, gateway, clock, None, account, msg, "today")

    assert result.handled is True
    sent_text = sender.sends[0]["text"]
    assert "<Evil & Co>" not in sent_text
    assert "<script>" not in sent_text
    assert "alert(1)" in sent_text


async def test_tool_wraps_names_and_excerpts_and_taints_the_run(env: Env) -> None:
    async with env.factory() as session:
        account = await seed_account(session)
        contact = Contact(
            account_id=account.id,
            telegram_user_id=8_888_888_888,
            display_name="Ali Example",
            first_seen_at=NOW,
            updated_at=NOW,
        )
        session.add(contact)
        await session.flush()
        conv = await seed_conversation(session, account_id=account.id, contact_id=contact.id)
        await seed_action(
            session,
            account_id=account.id,
            conversation_id=conv.id,
            state=ActionState.SUCCEEDED.value,
            text="Here is a sensitive message excerpt",
            created_at=NOW - timedelta(minutes=10),
            executed_at=NOW - timedelta(minutes=5),
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
    outcome = await get_activity_tool(
        ctx,
        GetActivityArgs(
            since=None,
            until=None,
            conversation_id=None,
            contact_id=None,
            show=None,
            limit=None,
        ),
    )
    assert outcome.status == "ok"
    assert outcome.contains_managed is True
    assert outcome.untrusted is True
    assert outcome.refs.conversation_ids == frozenset({conv.id})
    assert outcome.refs.contact_ids == frozenset({contact.id})
    [entry] = outcome.data["entries"]
    assert entry["contact_name"].startswith('<untrusted source="contact_name"')
    assert "Ali Example" in entry["contact_name"]
    assert entry["excerpt"].startswith('<untrusted source="owner_business_chat"')
    assert "sensitive message excerpt" in entry["excerpt"]


async def test_tool_refuses_a_foreign_contact_or_conversation(env: Env) -> None:
    async with env.factory() as session:
        acc1 = await seed_account(session)
        acc2 = await seed_account(session)
        conv2 = await seed_conversation(session, account_id=acc2.id)
        contact2 = Contact(
            account_id=acc2.id,
            telegram_user_id=7_777_777_777,
            display_name="Foreign Contact",
            first_seen_at=NOW,
            updated_at=NOW,
        )
        session.add(contact2)
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
    with pytest.raises(NotInAccountError):
        await get_activity_tool(
            ctx,
            GetActivityArgs(
                since=None,
                until=None,
                conversation_id=conv2.id,
                contact_id=None,
                show=None,
                limit=None,
            ),
        )
    with pytest.raises(NotInAccountError):
        await get_activity_tool(
            ctx,
            GetActivityArgs(
                since=None,
                until=None,
                conversation_id=None,
                contact_id=contact2.id,
                show=None,
                limit=None,
            ),
        )


def test_tool_rejects_schema_valid_but_invalid_arguments() -> None:
    # Both ids
    with pytest.raises(ValidationError, match="at most one"):
        GetActivityArgs(
            since=None,
            until=None,
            conversation_id=1,
            contact_id=2,
            show=None,
            limit=None,
        )

    # Period over 31 days
    with pytest.raises(ValidationError, match="at most 31 days"):
        GetActivityArgs(
            since="2026-10-01",
            until="2026-11-05",
            conversation_id=None,
            contact_id=None,
            show=None,
            limit=None,
        )

    # until before since
    with pytest.raises(ValidationError, match="since must be before until"):
        GetActivityArgs(
            since="2026-10-10",
            until="2026-10-05",
            conversation_id=None,
            contact_id=None,
            show=None,
            limit=None,
        )


async def test_tool_is_absent_without_its_flag_and_a_blocked_stub_without_managed_consent() -> None:
    registry = default_registry()

    # Absent without flag
    off = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=frozenset(),
        managed_consent=True,
    )
    assert "get_activity" not in off

    # Blocked stub without consent
    stubbed = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=frozenset({"tool.get_activity"}),
        managed_consent=False,
        stub_managed_tools=True,
    )
    assert "get_activity" in stubbed
    stub = stubbed["get_activity"]
    assert stub.returns_managed is False
    fake_ctx = ToolContext(
        account_id=1,
        run_id=1,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(
            session_factory=None,  # type: ignore[arg-type]
            clock=FakeClock(NOW),
            monthly_budget_usd=None,
        ),
    )
    outcome = await stub.handler(fake_ctx, None)
    assert outcome.status == CHAT_ACCESS_OFF
    assert outcome.data["reason"] == CHAT_ACCESS_OFF_REASON

    # Real tool with consent
    on = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=frozenset({"tool.get_activity"}),
        managed_consent=True,
    )
    assert "get_activity" in on
    assert on["get_activity"].handler is get_activity_tool


async def test_rg20_tool_reports_uncertain_and_failed_as_they_are(env: Env) -> None:
    async with env.factory() as session:
        account = await seed_account(session)
        c1 = await seed_conversation(session, account_id=account.id)
        c2 = await seed_conversation(session, account_id=account.id)
        c3 = await seed_conversation(session, account_id=account.id)
        c4 = await seed_conversation(session, account_id=account.id)

        await seed_action(
            session,
            account_id=account.id,
            conversation_id=c1.id,
            state=ActionState.DELIVERY_UNCERTAIN.value,
            text="Uncertain delivery",
            created_at=NOW - timedelta(minutes=40),
        )
        await seed_action(
            session,
            account_id=account.id,
            conversation_id=c2.id,
            state=ActionState.FAILED.value,
            text="Failed delivery",
            created_at=NOW - timedelta(minutes=30),
        )
        await seed_action(
            session,
            account_id=account.id,
            conversation_id=c3.id,
            state=ActionState.BLOCKED.value,
            text="Blocked delivery",
            created_at=NOW - timedelta(minutes=20),
        )
        await seed_action(
            session,
            account_id=account.id,
            conversation_id=c4.id,
            state=ActionState.SUCCEEDED.value,
            text="Sent delivery",
            created_at=NOW - timedelta(minutes=10),
            executed_at=NOW - timedelta(minutes=10),
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
    outcome = await get_activity_tool(
        ctx,
        GetActivityArgs(
            since=None,
            until=None,
            conversation_id=None,
            contact_id=None,
            show="problems",
            limit=None,
        ),
    )
    assert outcome.status == "ok"
    assert outcome.data["status_rule"] == STATUS_RULE
    groups = [e["group"] for e in outcome.data["entries"]]
    assert set(groups) == {"uncertain", "failed", "blocked"}
    assert "sent" not in groups


async def test_command_7d_over_50_actions_reports_accurate_omitted_count(
    db_session: AsyncSession,
) -> None:
    account = await seed_account(db_session)
    conv = await seed_conversation(db_session, account_id=account.id)
    # Seed 60 actions in the last 3 days
    for i in range(60):
        t = NOW - timedelta(days=2) + timedelta(minutes=i * 10)
        await seed_action(
            db_session,
            account_id=account.id,
            conversation_id=conv.id,
            state=ActionState.SUCCEEDED.value,
            text=f"Action {i}",
            created_at=t,
            executed_at=t,
        )
    await db_session.flush()

    clock = FakeClock(NOW)
    sender = FakeTelegramSender()
    gateway = OutboundGateway(sender, clock)

    msg = IncomingMessage(
        account_id=account.id,
        chat_id=account.control_chat_id,
        telegram_message_id=20,
        text="/activity 7d",
        from_id=account.telegram_user_id,
    )
    result = await handle_activity(db_session, gateway, clock, None, account, msg, "7d")

    assert result.handled is True
    assert len(sender.sends) == 1
    sent_text = sender.sends[0]["text"]
    assert "…and 30 more — ask me for a narrower period." in sent_text
