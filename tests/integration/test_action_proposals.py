"""`propose`: every owner-voice reply starts as a card or a blocked draft (S04.1 §3.5).

Real PostgreSQL; synthetic chats from `actions_env`. Concurrency cases hold
one transaction open until the other session is seen waiting on the lock
(an explicit barrier, never a timing guess).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from assistant.actions.config import ActionsConfig
from assistant.actions.models import Action
from assistant.actions.proposals import ProposalRefused, ProposalRequest, ProposalResult, propose
from assistant.actions.states import LIVE_CARD, ActionKind, ActionOrigin, ActionState
from assistant.conversations.models import Conversation
from assistant.core.errors import NotInAccountError
from assistant.core.reply_window import REPLY_WINDOW, SAFETY_MARGIN
from assistant.policy.context import Outcome
from tests.integration.actions_env import (
    CONFIG,
    Chat,
    action,
    add_message,
    audit,
    request,
    seed_chat,
    seed_run,
    seed_turn,
    wait_for_a_lock_wait,
)
from tests.integration.business_env import Env

pytestmark = pytest.mark.integration


async def new_chat(env: Env, **fields: Any) -> Chat:
    async with env.factory() as session, session.begin():
        return await seed_chat(session, env.clock.now(), **fields)


async def propose_in(
    env: Env, proposal: ProposalRequest, config: ActionsConfig = CONFIG
) -> ProposalResult:
    async with env.factory() as session, session.begin():
        return await propose(session, proposal, clock=env.clock, config=config)


async def run_and_turn(env: Env, chat: Chat) -> tuple[int, int]:
    async with env.factory() as session, session.begin():
        return await seed_run(session, chat.account_id), await seed_turn(session, chat.account_id)


async def actions_of(env: Env, chat: Chat) -> list[Action]:
    async with env.factory() as session:
        rows = await session.scalars(
            select(Action).where(Action.conversation_id == chat.conversation_id).order_by(Action.id)
        )
        return list(rows)


async def test_propose_creates_an_awaiting_action_with_token_and_expiry(env: Env) -> None:
    chat = await new_chat(env)
    result = await propose_in(env, request(chat))

    assert result.state is ActionState.AWAITING_APPROVAL
    assert result.reused is False
    assert result.superseded_action_id is None
    assert result.decision.outcome is Outcome.REQUIRE_APPROVAL
    row = await action(env, result.action_id)
    assert row.state == "awaiting_approval"
    assert row.version == 1
    assert row.approval_token
    assert row.expires_at == env.clock.now() + CONFIG.approval_max_age
    assert row.shown_through_message_id == chat.contact_message_id
    assert row.base_message_id == chat.contact_message_id
    assert row.policy_decision is not None
    assert row.policy_decision["outcome"] == "require_approval"
    assert row.policy_decision["phase"] == "proposal"
    assert [entry.event for entry in await audit(env, result.action_id)] == ["action_proposed"]


async def test_expiry_is_the_earlier_of_max_age_and_window_close(env: Env) -> None:
    late = await new_chat(env, contact_wrote=timedelta(hours=20))
    result = await propose_in(env, request(late))

    anchor = env.clock.now() - timedelta(hours=20)
    row = await action(env, result.action_id)
    assert row.expires_at == anchor + REPLY_WINDOW - SAFETY_MARGIN
    assert row.expires_at < env.clock.now() + CONFIG.approval_max_age


async def test_propose_is_idempotent_on_run_and_tool_call(env: Env) -> None:
    chat = await new_chat(env)
    run_id, _ = await run_and_turn(env, chat)
    call = request(chat, run_id=run_id, tool_call_id="call-1")

    first = await propose_in(env, call)
    again = await propose_in(env, call)

    assert again.reused is True
    assert again.action_id == first.action_id
    assert again.state is ActionState.AWAITING_APPROVAL
    assert again.decision == first.decision
    assert len(await actions_of(env, chat)) == 1
    assert [entry.event for entry in await audit(env, first.action_id)] == ["action_proposed"]


async def test_a_retried_turn_reuses_its_live_action(env: Env) -> None:
    chat = await new_chat(env)
    run_id, turn_id = await run_and_turn(env, chat)
    async with env.factory() as session, session.begin():
        retry_run = await seed_run(session, chat.account_id)

    first = await propose_in(
        env,
        request(
            chat,
            origin=ActionOrigin.OWNER_DICTATED,
            run_id=run_id,
            tool_call_id="a",
            trigger_turn_id=turn_id,
        ),
    )
    retried = await propose_in(
        env,
        request(
            chat,
            origin=ActionOrigin.OWNER_DICTATED,
            run_id=retry_run,
            tool_call_id="b",
            trigger_turn_id=turn_id,
        ),
    )

    assert retried.reused is True
    assert retried.action_id == first.action_id
    assert [row.state for row in await actions_of(env, chat)] == ["awaiting_approval"]


async def test_a_new_proposal_supersedes_the_live_card(env: Env) -> None:
    chat = await new_chat(env)
    first = await propose_in(env, request(chat))
    second = await propose_in(env, request(chat, text="Synthetic second draft"))

    assert second.state is ActionState.AWAITING_APPROVAL
    assert second.superseded_action_id == first.action_id
    old = await action(env, first.action_id)
    assert old.state == "outdated"
    assert old.state_reason == "superseded"
    assert old.approval_token is None
    superseded = (await audit(env, first.action_id))[-1]
    assert superseded.event == "action_superseded"
    assert superseded.details["superseded_by"] == second.action_id


async def test_a_blocked_proposal_does_not_supersede_the_live_card(env: Env) -> None:
    chat = await new_chat(env)
    live = await propose_in(env, request(chat))
    blocked = await propose_in(env, request(chat, text="   "))

    assert blocked.state is ActionState.BLOCKED
    assert blocked.superseded_action_id is None
    assert (await action(env, live.action_id)).state == "awaiting_approval"
    assert (await action(env, blocked.action_id)).state_reason == "too_long"


async def test_propose_blocks_with_a_reason_when_the_window_is_closed(env: Env) -> None:
    chat = await new_chat(env, contact_wrote=timedelta(hours=25))
    result = await propose_in(env, request(chat))

    assert result.state is ActionState.BLOCKED
    assert result.decision.outcome is Outcome.DENY
    row = await action(env, result.action_id)
    assert row.state_reason == "window_closed"
    assert row.approval_token is None
    assert row.text  # kept as copy-ready text for the card (FR-DRF-06)
    blocked = await audit(env, result.action_id)
    assert [entry.event for entry in blocked] == ["action_blocked"]
    assert blocked[0].details["failed_rules"] == ["window_open"]


async def test_propose_blocks_without_the_reply_right(env: Env) -> None:
    chat = await new_chat(env, reply_right=False)
    result = await propose_in(env, request(chat))

    assert result.state is ActionState.BLOCKED
    failed = [check for check in result.decision.checks if not check.passed]
    assert [(check.name, check.reason) for check in failed] == [
        ("connection_active", "no_reply_right")
    ]
    assert (await action(env, result.action_id)).state_reason == "policy_denied"


async def test_with_sending_off_a_proposal_still_becomes_an_awaiting_card(env: Env) -> None:
    chat = await new_chat(env)
    result = await propose_in(env, request(chat), replace(CONFIG, sending_enabled=False))

    assert result.state is ActionState.AWAITING_APPROVAL
    assert "sending_enabled" not in [check.name for check in result.decision.checks]


@pytest.mark.parametrize(
    ("fields", "code"),
    [
        ({"origin": ActionOrigin.DELEGATION_AUTO}, "autonomous_not_available"),
        ({"kind": ActionKind.EDIT_MESSAGE}, "kind_not_proposable"),
        ({"kind": ActionKind.DELETE_OWN}, "kind_not_proposable"),
        # A proactive draft without a base would have no takeover reference.
        ({"base_message_id": None}, "base_required"),
    ],
)
async def test_propose_refuses_delegation_auto_in_p2(
    env: Env, fields: dict[str, object], code: str
) -> None:
    chat = await new_chat(env)
    with pytest.raises(ProposalRefused) as refused:
        await propose_in(env, request(chat, **fields))

    assert refused.value.code == code
    assert await actions_of(env, chat) == []


async def test_a_dictated_message_after_the_owners_own_manual_reply_is_a_normal_card(
    env: Env,
) -> None:
    chat = await new_chat(env)
    _, turn_id = await run_and_turn(env, chat)
    async with env.factory() as session, session.begin():
        await add_message(
            session,
            chat.account_id,
            chat.conversation_id,
            direction="owner_manual",
            sent_at=env.clock.now() - timedelta(minutes=10),
        )

    dictated = await propose_in(
        env, request(chat, origin=ActionOrigin.OWNER_DICTATED, trigger_turn_id=turn_id)
    )
    # A proactive draft based on the contact message *is* overtaken by it.
    proactive = await propose_in(env, request(chat))

    assert dictated.state is ActionState.AWAITING_APPROVAL
    assert (await action(env, dictated.action_id)).base_message_id is None
    assert proactive.state is ActionState.BLOCKED
    assert (await action(env, proactive.action_id)).state_reason == "takeover"


async def test_only_dictated_text_is_marked_fresh_through_the_newest_contact_message(
    env: Env,
) -> None:
    chat = await new_chat(env)
    _, turn_id = await run_and_turn(env, chat)

    draft = await propose_in(env, request(chat, shown_through_message_id=None))
    dictated = await propose_in(
        env,
        request(
            chat,
            origin=ActionOrigin.OWNER_DICTATED,
            trigger_turn_id=turn_id,
            shown_through_message_id=None,
        ),
    )

    # A draft is fresh only through what it saw: `not_stale` fails it closed.
    assert (await action(env, draft.action_id)).shown_through_message_id is None
    assert (await action(env, dictated.action_id)).shown_through_message_id == (
        chat.contact_message_id
    )


async def test_propose_stores_draft_flags_outside_the_policy_decision(env: Env) -> None:
    chat = await new_chat(env)
    flags = {"needs_reply": True, "confidence": 0.75, "sensitive_topic": False, "language": "en"}
    result = await propose_in(env, request(chat, draft_flags=flags))

    row = await action(env, result.action_id)
    assert row.draft_flags == flags
    assert "needs_reply" not in json.dumps(row.policy_decision)


async def test_propose_refuses_another_accounts_conversation(env: Env) -> None:
    mine = await new_chat(env)
    theirs = await new_chat(env)

    with pytest.raises(NotInAccountError):
        await propose_in(env, request(mine, conversation_id=theirs.conversation_id))
    assert await actions_of(env, theirs) == []


async def test_one_live_card_per_conversation_under_concurrent_proposals(env: Env) -> None:
    chat = await new_chat(env)
    async with env.factory() as first_session:
        await first_session.begin()
        first = await propose(first_session, request(chat), clock=env.clock, config=CONFIG)
        # The first proposal holds the conversation lock until it commits.
        second_task = asyncio.create_task(
            propose_in(env, request(chat, text="Synthetic concurrent draft"))
        )
        await wait_for_a_lock_wait(env)
        await first_session.commit()
    second = await second_task

    live = [row for row in await actions_of(env, chat) if row.state in LIVE_CARD]
    assert [row.id for row in live] == [second.action_id]
    assert second.superseded_action_id == first.action_id


async def test_proposal_waits_for_a_conversation_lock_held_by_ingestion(env: Env) -> None:
    chat = await new_chat(env)
    async with env.factory() as ingestion:
        await ingestion.begin()
        await ingestion.execute(
            select(Conversation.id).where(Conversation.id == chat.conversation_id).with_for_update()
        )
        proposal = asyncio.create_task(propose_in(env, request(chat)))
        await wait_for_a_lock_wait(env)
        # Ingestion records the owner's own reply, then releases the chat.
        await add_message(
            ingestion,
            chat.account_id,
            chat.conversation_id,
            direction="owner_manual",
            sent_at=env.clock.now() - timedelta(minutes=1),
        )
        await ingestion.commit()
    result = await proposal

    # The proposal read the chat only after ingestion committed: a takeover.
    assert result.state is ActionState.BLOCKED
    assert (await action(env, result.action_id)).state_reason == "takeover"


async def test_proposal_audit_rows_are_content_free(env: Env) -> None:
    canary = "CANARY-proposal-text-7f3a"
    chat = await new_chat(env)
    first = await propose_in(env, request(chat, text=canary))
    second = await propose_in(env, request(chat, text=canary + " again"))
    blocked = await propose_in(env, request(chat, text=canary * 400))

    allowed = {"from", "to", "version", "reason", "superseded_by", "failed_rules"}
    for action_id in (first.action_id, second.action_id, blocked.action_id):
        for entry in await audit(env, action_id):
            assert canary not in json.dumps(entry.details)
            assert set(entry.details) <= allowed
    async with env.factory() as session:
        total = await session.scalar(
            select(func.count()).select_from(Action).where(Action.account_id == chat.account_id)
        )
    assert total == 3
