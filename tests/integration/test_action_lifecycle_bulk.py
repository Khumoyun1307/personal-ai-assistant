"""F0 bulk transitions preserve send claims, tenant boundaries and audit facts."""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from assistant.accounts.models import Account, AuditLogEntry
from assistant.actions.lifecycle import cancel_pending, clear_texts, outdate_pending
from assistant.actions.models import Action
from assistant.actions.states import ActionState, StateReason
from assistant.conversations.models import Contact, Conversation
from tests.integration.business_env import Env

pytestmark = pytest.mark.integration


async def seed_actions(env: Env, user_id: int = 7000080001) -> tuple[int, int, dict[str, int]]:
    async with env.factory() as session, session.begin():
        account = Account(telegram_user_id=user_id, status="active")
        session.add(account)
        await session.flush()
        contact = Contact(account_id=account.id, telegram_user_id=user_id + 100)
        session.add(contact)
        await session.flush()
        conversation = Conversation(
            account_id=account.id, contact_id=contact.id, business_chat_id=contact.telegram_user_id
        )
        session.add(conversation)
        await session.flush()
        ids = {}
        for state in ActionState:
            # The unique live-card index permits one of these states per conversation.
            if state is ActionState.REVISING:
                continue
            action = Action(
                account_id=account.id,
                conversation_id=conversation.id,
                kind="send_message",
                origin="agent_proposal",
                state=state.value,
                text="synthetic action text",
                approval_token=f"synthetic-{user_id}-{state}"
                if state.value == "awaiting_approval"
                else None,
                version=1,
                created_at=env.clock.now(),
                updated_at=env.clock.now(),
            )
            session.add(action)
            await session.flush()
            ids[state.value] = action.id
        return account.id, conversation.id, ids


async def rows(env: Env, account_id: int) -> dict[int, Action]:
    async with env.factory() as session:
        return {
            row.id: row
            for row in (
                await session.scalars(select(Action).where(Action.account_id == account_id))
            )
        }


async def test_outdate_pending_moves_only_pending_states_and_audits_each(env: Env) -> None:
    account_id, conversation_id, ids = await seed_actions(env)
    async with env.factory() as session, session.begin():
        changed = await outdate_pending(
            session,
            account_id=account_id,
            conversation_id=conversation_id,
            reason=StateReason.NEW_CONTACT_MESSAGE,
            include_approved=False,
            now=env.clock.now(),
        )
    assert changed == [ids["awaiting_approval"]]
    stored = await rows(env, account_id)
    assert stored[ids["awaiting_approval"]].state == "outdated"
    for state, action_id in ids.items():
        if state != "awaiting_approval":
            assert stored[action_id].state == state
    async with env.factory() as session:
        audit = (await session.scalars(select(AuditLogEntry))).one()
        assert audit.event == "action_outdated"
        assert audit.details["from"] == "awaiting_approval"
        assert "synthetic action text" not in str(audit.details)


async def test_outdate_pending_includes_approved_only_when_asked(env: Env) -> None:
    account_id, conversation_id, ids = await seed_actions(env)
    async with env.factory() as session, session.begin():
        changed = await outdate_pending(
            session,
            account_id=account_id,
            conversation_id=conversation_id,
            reason=StateReason.TAKEOVER,
            include_approved=True,
            now=env.clock.now(),
        )
    assert set(changed) == {ids["awaiting_approval"], ids["approved"]}


async def test_cancel_pending_never_touches_executing_uncertain_or_terminal(env: Env) -> None:
    account_id, conversation_id, ids = await seed_actions(env)
    async with env.factory() as session, session.begin():
        changed = await cancel_pending(
            session,
            account_id=account_id,
            conversation_ids=[conversation_id],
            reason=StateReason.DATA_DELETED,
            now=env.clock.now(),
        )
        assert (
            await cancel_pending(
                session,
                account_id=account_id,
                conversation_ids=[conversation_id],
                reason=StateReason.DATA_DELETED,
                now=env.clock.now(),
            )
            == []
        )
    expected = {ids[state] for state in ("proposed", "awaiting_approval", "approved", "scheduled")}
    assert set(changed) == expected
    stored = await rows(env, account_id)
    for state, action_id in ids.items():
        assert stored[action_id].state == ("cancelled" if action_id in expected else state)


async def test_clear_texts_never_crosses_accounts(env: Env) -> None:
    account_id, conversation_id, ids = await seed_actions(env)
    other_account, _, other_ids = await seed_actions(env, user_id=7000080002)
    async with env.factory() as session, session.begin():
        assert (
            await clear_texts(session, account_id=other_account, conversation_ids=[conversation_id])
            == 0
        )
        assert await clear_texts(
            session, account_id=account_id, conversation_ids=[conversation_id]
        ) == len(ids)
    assert all(row.text is None for row in (await rows(env, account_id)).values())
    assert len(await rows(env, other_account)) == len(other_ids)
    assert all(
        row.text == "synthetic action text" for row in (await rows(env, other_account)).values()
    )


async def test_bulk_helpers_clear_the_approval_token(env: Env) -> None:
    account_id, _, ids = await seed_actions(env)
    async with env.factory() as session, session.begin():
        await cancel_pending(
            session,
            account_id=account_id,
            conversation_ids=None,
            reason=StateReason.DATA_DELETED,
            now=env.clock.now(),
        )
    assert (await rows(env, account_id))[ids["awaiting_approval"]].approval_token is None


@pytest.mark.parametrize("operation", ["cancel", "outdate"])
async def test_bulk_helpers_handle_a_revising_card_without_restoring_its_token(
    env: Env,
    operation: str,
) -> None:
    account_id, conversation_id, ids = await seed_actions(env)
    async with env.factory() as session, session.begin():
        await session.execute(
            update(Action)
            .where(
                Action.account_id == account_id,
                Action.id == ids["awaiting_approval"],
            )
            .values(state="revising", approval_token=None)
        )
        if operation == "cancel":
            await cancel_pending(
                session,
                account_id=account_id,
                conversation_ids=[conversation_id],
                reason=StateReason.DATA_DELETED,
                now=env.clock.now(),
            )
        else:
            await outdate_pending(
                session,
                account_id=account_id,
                conversation_id=conversation_id,
                reason=StateReason.NEW_CONTACT_MESSAGE,
                include_approved=False,
                now=env.clock.now(),
            )
    action = (await rows(env, account_id))[ids["awaiting_approval"]]
    assert action.state == ("cancelled" if operation == "cancel" else "outdated")
    assert action.approval_token is None
