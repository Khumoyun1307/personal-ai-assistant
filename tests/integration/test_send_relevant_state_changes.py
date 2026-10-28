"""Call sites that change send-relevant state (S04.4 §3.8).

Each runs in the triggering change's own transaction, through the real
inbox: a disabled connection cancels every unsent action, changed rights
outdate drafts and approvals, revoking `ai_managed` cancels AI drafts only,
and a dead-lettered update outdates its chat's pending actions. Tx A's
re-check is the guarantee either way; these keep the cards honest early.
Synthetic text.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from assistant.actions.proposals import propose
from assistant.actions.states import ActionOrigin
from assistant.conversations.models import Conversation, Message
from tests.integration.actions_env import CONFIG, Chat, request, seed_turn
from tests.integration.business_env import (
    OTHER_CONTACT,
    OWNER,
    T_BASE,
    Env,
    business_message,
    connection_update,
    deliver,
    inbound,
    private_message,
)
from tests.integration.execution_env import action, approved, owner_chat

pytestmark = pytest.mark.integration

TEXT = "Synthetic reply"


async def card(env: Env, chat: Chat, *, dictated: bool = False) -> int:
    """A live approval card: an AI draft, or the owner's dictated text."""
    async with env.factory() as session, session.begin():
        fields: dict[str, object] = {"text": TEXT}
        if dictated:
            fields |= {
                "origin": ActionOrigin.OWNER_DICTATED,
                "trigger_turn_id": await seed_turn(session, chat.account_id),
            }
        result = await propose(session, request(chat, **fields), clock=env.clock, config=CONFIG)
        return result.action_id


async def other_chat(env: Env, chat: Chat) -> Chat:
    """The same owner's chat with a second contact, through the real inbox."""
    await deliver(
        env,
        business_message(from_id=OTHER_CONTACT, chat_id=OTHER_CONTACT, message_id=21, date=T_BASE),
    )
    async with env.factory() as session:
        conversation = (
            await session.execute(
                select(Conversation).where(Conversation.business_chat_id == OTHER_CONTACT)
            )
        ).scalar_one()
        message_id = await session.scalar(
            select(Message.id).where(Message.conversation_id == conversation.id)
        )
    assert message_id is not None
    return Chat(chat.account_id, conversation.id, message_id)


async def test_connection_disabled_cancels_pending_actions(env: Env) -> None:
    chat = await owner_chat(env)
    sending = await approved(env, chat, text=TEXT)
    live = await card(env, chat)

    await deliver(env, connection_update(enabled=False))

    for action_id in (sending, live):
        row = await action(env, action_id)
        assert (row.state, row.state_reason) == ("cancelled", "connection_disabled")


async def test_rights_change_outdates_pending_and_approved_actions(env: Env) -> None:
    chat = await owner_chat(env)
    sending = await approved(env, chat, text=TEXT)
    live = await card(env, chat)

    await deliver(env, connection_update(rights=("can_reply", "can_read_messages")))

    for action_id in (sending, live):
        row = await action(env, action_id)
        assert (row.state, row.state_reason) == ("outdated", "rights_changed")


async def test_managed_consent_revocation_cancels_pending_ai_drafts_and_keeps_dictated_ones(
    env: Env,
) -> None:
    chat = await owner_chat(env)
    second = await other_chat(env, chat)
    ai_approved = await approved(env, chat, text=TEXT)
    dictated = await card(env, chat, dictated=True)
    ai_card = await card(env, second)

    await deliver(env, private_message(OWNER, "/privacy revoke"))

    for action_id in (ai_approved, ai_card):
        row = await action(env, action_id)
        assert (row.state, row.state_reason) == ("cancelled", "consent_revoked")
    assert (await action(env, dictated)).state == "awaiting_approval"


async def test_a_dead_lettered_update_outdates_with_reason_ingest_error(env: Env) -> None:
    """ARCHITECTURE §6.2: the failed update may hold a takeover or a newer
    contact message; the chat's pending actions (approved included) are
    outdated with `ingest_error`."""
    chat = await owner_chat(env)
    sending = await approved(env, chat, text=TEXT)
    live = await card(env, chat)
    poison = business_message(message_text="synthetic poison", extra={"message_id": None})
    for _ in range(3):
        await deliver(env, poison)
    assert (await inbound(env, poison)).status == "failed"

    for action_id in (sending, live):
        row = await action(env, action_id)
        assert (row.state, row.state_reason) == ("outdated", "ingest_error")
