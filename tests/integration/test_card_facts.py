"""Q3: card reads explain terminal states without guessing or leaking text."""

from __future__ import annotations

import pytest

from assistant.accounts.models import AuditLogEntry
from assistant.actions.activity import live_cards
from assistant.actions.authorship import VersionAuthorship
from assistant.actions.card_facts import presentation_facts
from assistant.agent.tools.activity import GetActivityArgs, get_activity_tool
from assistant.agent.tools.drafts import GetDraftArgs, get_draft_tool
from assistant.agent.tools.registry import (
    CHAT_ACCESS_OFF,
    CapabilityTier,
    ToolContext,
    ToolServices,
    default_registry,
)
from assistant.core.errors import NotInAccountError
from tests.fakes.clock import FakeClock
from tests.integration.business_env import Env
from tests.integration.p2_factories import NOW, seed_account, seed_action, seed_conversation

pytestmark = pytest.mark.integration


async def test_current_authorship_is_shared_by_draft_reads_and_live_card_facts(env: Env) -> None:
    proof = VersionAuthorship(
        version=2,
        kind="owner_literal_edit",
        source_ids=[],
        parent_version=1,
        retains_generated_text=True,
    )
    async with env.factory() as session, session.begin():
        account = await seed_account(session)
        conv = await seed_conversation(session, account_id=account.id)
        row = await seed_action(
            session,
            account_id=account.id,
            conversation_id=conv.id,
            version=2,
            origin="owner_dictated",
            state="awaiting_approval",
            authorship=proof.model_dump(mode="json"),
        )
    result = await get_draft_tool(
        context(env, account.id), GetDraftArgs(action_id=row.id, conversation_id=None)
    )
    assert result.data["written_by"] == "owner_literal_edit"
    assert result.data["ai_generated"] is True
    async with env.factory() as session:
        [fact] = await live_cards(session, account_id=account.id)
    assert fact.authorship_kind == "owner_literal_edit"
    assert fact.ai_generated is True


def context(env: Env, account_id: int) -> ToolContext:
    return ToolContext(
        account_id=account_id,
        run_id=1,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(
            session_factory=env.factory, clock=FakeClock(NOW), monthly_budget_usd=None
        ),
    )


@pytest.mark.parametrize(
    ("state", "reason", "explanation"),
    [
        ("outdated", "new_contact_message", "A newer message arrived after this draft was made."),
        ("outdated", "takeover", "You replied in this chat after this draft was made."),
        ("rejected", "owner_rejected", "You rejected this draft."),
        ("failed", "not_delivered", "Telegram did not send this reply."),
        ("expired", "max_age", "This draft's approval period has expired."),
    ],
)
async def test_terminal_card_facts_in_draft_activity_and_working_context(
    env: Env, state: str, reason: str, explanation: str
) -> None:
    async with env.factory() as session:
        account = await seed_account(session, timezone="Asia/Tashkent")
        conv = await seed_conversation(session, account_id=account.id)
        action = await seed_action(
            session,
            account_id=account.id,
            conversation_id=conv.id,
            state=state,
            state_reason=reason,
            version=3,
            draft_flags={"mode": "reply"},
            text="Synthetic card words",
        )
        session.add(
            AuditLogEntry(
                account_id=account.id,
                at=NOW,
                actor="system",
                event="approval_card_presentation_resolved",
                subject_type="action",
                subject_id=action.id,
                details={"version": 2, "claim_id": 123, "outcome": "shown"},
            )
        )
        await session.commit()
    ctx = context(env, account.id)
    draft = await get_draft_tool(ctx, GetDraftArgs(conversation_id=conv.id, action_id=None))
    activity = await get_activity_tool(
        ctx,
        GetActivityArgs(
            since=None, until=None, conversation_id=conv.id, contact_id=None, show=None, limit=5
        ),
    )
    [entry] = activity.data["entries"]
    for facts in (draft.data, entry):
        assert facts["state"] == state
        assert facts["state_reason"] == reason
        assert facts["state_reason_explanation"] == explanation
        assert facts["version"] == 3
        assert facts["origin"] == action.origin
        assert facts["mode"] == "reply"
        assert facts["created_at"] == "2026-10-27 17:00"
        assert facts["updated_at"] == "2026-10-27 17:00"
        assert facts["presentation_outcome"] == "shown"
        assert facts["presentation_version"] == 2
    assert draft.contains_managed is True
    assert draft.untrusted is True
    assert '<untrusted source="draft"' in draft.data["text"]
    assert "text" not in entry
    async with env.factory() as session:
        [fact] = await live_cards(session, account_id=account.id)
    assert fact.action_id == action.id
    assert fact.state_reason == reason
    assert fact.state_reason_explanation == explanation
    assert fact.presentation_outcome == "shown"


async def test_missing_text_and_foreign_ids(env: Env) -> None:
    async with env.factory() as session:
        account = await seed_account(session)
        foreign = await seed_account(session)
        conv = await seed_conversation(session, account_id=foreign.id)
        action = await seed_action(
            session, account_id=foreign.id, conversation_id=conv.id, state="expired", text=None
        )
        await session.commit()
    own = context(env, foreign.id)
    result = await get_draft_tool(own, GetDraftArgs(action_id=action.id, conversation_id=None))
    assert result.data["text"] is None
    assert result.data["text_removed"] is True
    assert result.data["word_count"] is None
    assert result.data["presentation_outcome"] is None
    activity = await get_activity_tool(
        own,
        GetActivityArgs(
            since=None, until=None, conversation_id=conv.id, contact_id=None, show=None, limit=5
        ),
    )
    [entry] = activity.data["entries"]
    assert entry["excerpt"] is None
    for args in (
        GetDraftArgs(action_id=action.id, conversation_id=None),
        GetDraftArgs(action_id=None, conversation_id=conv.id),
    ):
        with pytest.raises(NotInAccountError):
            await get_draft_tool(context(env, account.id), args)
    async with env.factory() as session:
        assert await live_cards(session, account_id=account.id) == []
    with pytest.raises(NotInAccountError):
        await get_activity_tool(
            context(env, account.id),
            GetActivityArgs(
                since=None,
                until=None,
                conversation_id=conv.id,
                contact_id=None,
                show=None,
                limit=5,
            ),
        )


async def test_terminal_history_is_bounded_and_keeps_live_cards_first(env: Env) -> None:
    async with env.factory() as session:
        account = await seed_account(session)
        conv = await seed_conversation(session, account_id=account.id)
        live = await seed_action(
            session, account_id=account.id, conversation_id=conv.id, state="awaiting_approval"
        )
        terminal = [
            await seed_action(
                session, account_id=account.id, conversation_id=conv.id, state="rejected"
            )
            for _ in range(55)
        ]
        cards = await live_cards(session, account_id=account.id, limit=500)
        assert len(cards) == 50
        assert cards[0].action_id == live.id
        assert cards[1].action_id == terminal[-1].id
        assert await live_cards(session, account_id=account.id, limit=0) == []
        await session.commit()
    activity = await get_activity_tool(
        context(env, account.id),
        GetActivityArgs(
            since=None, until=None, conversation_id=conv.id, contact_id=None, show=None, limit=3
        ),
    )
    assert activity.data["count"] == 3
    assert activity.data["total"] == 56


async def test_card_read_tools_withheld_without_managed_consent(env: Env) -> None:
    offered = default_registry().for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=frozenset({"tool.get_draft", "tool.get_activity"}),
        managed_consent=False,
        stub_managed_tools=True,
    )
    ctx = context(env, 1)
    for name in ("get_draft", "get_activity"):
        result = await offered[name].handler(ctx, None)
        assert result.status == CHAT_ACCESS_OFF
        assert result.contains_managed is False
        assert "entries" not in result.data
        assert "text" not in result.data


async def test_presentation_read_is_scoped_versioned_and_does_not_infer_success(env: Env) -> None:
    async with env.factory() as session:
        account = await seed_account(session)
        foreign = await seed_account(session)
        conv = await seed_conversation(session, account_id=account.id)
        action = await seed_action(session, account_id=account.id, conversation_id=conv.id)
        for event, details in (
            ("approval_card_presentation_resolved", {"version": 1, "outcome": "shown"}),
            ("approval_card_presentation_claimed", {"version": 2}),
        ):
            session.add(
                AuditLogEntry(
                    account_id=account.id,
                    at=NOW,
                    actor="system",
                    event=event,
                    subject_type="action",
                    subject_id=action.id,
                    details=details,
                )
            )
            await session.flush()
        facts = await presentation_facts(session, account_id=account.id, action_ids=[action.id])
        assert facts[action.id].outcome == "claimed"
        assert facts[action.id].version == 2
        foreign_facts = await presentation_facts(
            session, account_id=foreign.id, action_ids=[action.id]
        )
        assert foreign_facts == {}
        session.add(
            AuditLogEntry(
                account_id=account.id,
                at=NOW,
                actor="system",
                event="approval_card_presentation_resolved",
                subject_type="action",
                subject_id=action.id,
                details={"version": 2, "outcome": "not_delivered"},
            )
        )
        await session.flush()
        facts = await presentation_facts(session, account_id=account.id, action_ids=[action.id])
        assert facts[action.id].outcome == "not_delivered"
