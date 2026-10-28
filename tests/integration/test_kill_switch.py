"""The kill switch: /pause, stop words, the poller's fast lane, /resume
(S04.4 §3.7; PRD J9; AGENTS §6 invariant 10).

In P2, /pause stops autonomous sending only (P2-T23): pending autonomous
actions are cancelled and Tx A refuses that origin while paused; drafts stay
drafts and owner approvals stand. The acknowledgement is sent only after the
pause commits. Texts are synthetic.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from assistant.accounts.models import Account, AuditLogEntry
from assistant.actions.executor import ExecuteStatus
from assistant.actions.proposals import propose
from assistant.actions.states import ActionOrigin, ActionState, StateReason
from assistant.conversations.models import Conversation
from assistant.db.uow import transaction
from assistant.telegram.control.dispatcher import PAUSED_TEXT, RESUMED_TEXT
from assistant.telegram.control.handlers import IncomingMessage
from assistant.telegram.ingress.poller import Poller
from tests.fakes.telegram import FakeUpdateSource
from tests.integration.actions_env import CONFIG, request, seed_turn
from tests.integration.business_env import (
    CONNECTION,
    CONTACT,
    OWNER,
    STRANGER,
    Env,
    business_message,
    deliver,
    private_message,
)
from tests.integration.execution_env import (
    action,
    approved,
    caught_up,
    harness,
    inserted,
    owner_account,
    owner_chat,
    seed,
    update_rows,
)

pytestmark = pytest.mark.integration

TEXT = "Synthetic approved reply"


async def test_rg19_pause_blocks_an_autonomous_action_at_claim(env: Env) -> None:
    """RG-19 (invariant 10): with the account paused, Tx A refuses an
    autonomous action whatever else would pass — and in P2 an autonomous
    action is never sent even unpaused (no delegation exists)."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    paused = await inserted(env, chat, origin=ActionOrigin.DELEGATION_AUTO)
    await update_rows(env, Account, Account.id == chat.account_id, autonomy_paused=True)

    outcome = await h.execute(paused)

    assert (outcome.state, outcome.reason) == (ActionState.CANCELLED, StateReason.AUTONOMY_PAUSED)
    await update_rows(env, Account, Account.id == chat.account_id, autonomy_paused=False)
    unpaused = await inserted(env, chat, origin=ActionOrigin.DELEGATION_AUTO)
    assert (await h.execute(unpaused)).state is ActionState.BLOCKED  # `no_delegation`
    assert h.sender.business_calls == 0


async def test_pause_cancels_pending_autonomous_actions_and_keeps_drafts(env: Env) -> None:
    chat = await owner_chat(env)
    owner_approved = await approved(env, chat, text=TEXT)
    autonomous = await inserted(env, chat, origin=ActionOrigin.DELEGATION_AUTO)
    async with env.factory() as session, session.begin():
        turn = await seed_turn(session, chat.account_id)
        card = await propose(
            session,
            request(chat, text=TEXT, origin=ActionOrigin.OWNER_DICTATED, trigger_turn_id=turn),
            clock=env.clock,
            config=CONFIG,
        )

    await deliver(env, private_message(OWNER, "/pause"))

    assert (await owner_account(env)).autonomy_paused
    row = await action(env, autonomous)
    assert (row.state, row.state_reason) == ("cancelled", "autonomy_paused")
    assert (await action(env, card.action_id)).state == "awaiting_approval"
    assert (await action(env, owner_approved)).state == "approved"
    assert env.sender.texts[-1] == PAUSED_TEXT


async def test_pause_does_not_block_an_owner_approved_send(env: Env) -> None:
    """P2-T23: an approval is the owner's explicit decision on that text."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    await update_rows(env, Account, Account.id == chat.account_id, autonomy_paused=True)

    assert (await h.execute(action_id)).status is ExecuteStatus.SENT


async def poll(env: Env, *updates: dict[str, Any]) -> Poller:
    source = FakeUpdateSource([list(updates)], bot_id=env.bot_id)
    polling = Poller(source, env.factory, env.clock, owner_ids=frozenset({OWNER}))
    await polling.startup()
    await polling.poll_once()
    return polling


@pytest.mark.parametrize("command", ["/pause", "/pause@synthetic_bot", " /PAUSE "])
async def test_the_poller_fast_lane_pauses_on_an_owner_pause_command(
    env: Env, command: str
) -> None:
    """The pause is in force once the poller stored the update — before the
    inbox processor has even read it (ARCHITECTURE §6.1)."""
    chat = await owner_chat(env)
    autonomous = await inserted(env, chat, origin=ActionOrigin.DELEGATION_AUTO)

    await poll(env, private_message(OWNER, command))

    assert (await owner_account(env)).autonomy_paused
    assert (await action(env, autonomous)).state == "cancelled"
    async with env.factory() as session:
        audit = await session.scalar(
            select(AuditLogEntry).where(AuditLogEntry.event == "autonomy_paused")
        )
    assert audit is not None
    assert audit.details["source"] == "fast_lane"
    await env.processor.process_batch()  # the inbox then acknowledges it
    assert env.sender.texts[-1] == PAUSED_TEXT


async def test_the_fast_lane_pauses_on_a_stop_word(env: Env) -> None:
    await owner_chat(env)

    await poll(env, private_message(OWNER, "стоп"))

    assert (await owner_account(env)).autonomy_paused


async def test_a_replayed_batch_never_reapplies_a_processed_pause(env: Env) -> None:
    """Antony #2 on #107: Telegram replays a batch the poller stored but had
    not yet confirmed (a crash before the next `getUpdates`). Its /pause and
    /resume were already processed; the replay stores nothing and changes
    nothing — "Resumed." stays true (invariant 11)."""
    await owner_chat(env)
    batch = [private_message(OWNER, "/pause"), private_message(OWNER, "/resume")]
    await poll(env, *batch)
    await env.processor.process_batch()
    assert not (await owner_account(env)).autonomy_paused
    assert env.sender.texts[-1] == RESUMED_TEXT

    replay = await poll(env, *batch)

    assert replay.state.updates_written == 0
    assert not (await owner_account(env)).autonomy_paused


async def test_a_mixed_replay_applies_the_fast_lane_only_to_new_updates(env: Env) -> None:
    """A replayed /pause beside a new, unrelated update changes nothing
    (a batch-wide "anything written" test would re-pause); a genuinely new
    /pause beside replayed ones still pauses at once."""
    await owner_chat(env)
    batch = [private_message(OWNER, "/pause"), private_message(OWNER, "/resume")]
    await poll(env, *batch)
    await env.processor.process_batch()

    mixed = await poll(env, *batch, private_message(OWNER, "Synthetic note"))

    assert mixed.state.updates_written == 1
    assert not (await owner_account(env)).autonomy_paused

    fresh = await poll(env, *batch, private_message(OWNER, "/pause"))

    assert fresh.state.updates_written == 1
    assert (await owner_account(env)).autonomy_paused


async def test_the_fast_lane_ignores_forwarded_text_and_non_owners(env: Env) -> None:
    """Forwarded text is someone else's words (§6 invariant 3b); a stranger
    is no owner (invariant 12); a business message is never a command, even
    the owner's (invariant 5); and only the whole text counts."""
    await owner_chat(env)
    forwarded = private_message(OWNER, "/pause")
    forwarded["message"]["forward_origin"] = {
        "type": "hidden_user",
        "date": 1,
        "sender_user_name": "x",
    }
    legacy = private_message(OWNER, "stop")
    legacy["message"]["forward_date"] = 1
    in_business_chat = business_message(
        CONNECTION, from_id=OWNER, chat_id=CONTACT, message_id=40, message_text="/pause"
    )

    await poll(
        env,
        forwarded,
        legacy,
        private_message(STRANGER, "/pause"),
        private_message(OWNER, "/pause now"),
        private_message(OWNER, "please stop it"),
        in_business_chat,
    )

    assert not (await owner_account(env)).autonomy_paused


async def test_the_pause_acknowledgement_is_sent_only_after_commit(env: Env) -> None:
    """ARCHITECTURE §9.4: "Paused" is never said for a pause a rollback undid,
    and when it is said, the pause is already committed."""
    await owner_chat(env)
    account = await owner_account(env)
    before = len(env.sender.texts)
    pause = IncomingMessage(
        account_id=account.id,
        chat_id=OWNER,
        telegram_message_id=900,
        text="/pause",
        from_id=OWNER,
    )

    async def dispatch_then_roll_back() -> None:
        async with transaction(env.factory) as uow:
            await env.dispatcher.dispatch(uow.session, pause)
            raise RuntimeError("synthetic rollback")

    with pytest.raises(RuntimeError):
        await dispatch_then_roll_back()
    assert len(env.sender.texts) == before
    assert not (await owner_account(env)).autonomy_paused

    seen: list[bool] = []

    async def committed_at_send() -> None:
        seen.append((await owner_account(env)).autonomy_paused)

    env.sender.on_call = committed_at_send
    async with transaction(env.factory) as uow:
        await env.dispatcher.dispatch(uow.session, pause)
        assert len(env.sender.texts) == before  # not inside the transaction
    env.sender.on_call = None

    assert env.sender.texts[before:] == [PAUSED_TEXT]
    assert seen == [True]


async def test_resume_clears_the_account_flag_and_keeps_conversation_takeover_pauses(
    env: Env,
) -> None:
    """R-09: /resume clears the account flag only; a chat paused by the
    owner's takeover stays paused."""
    chat = await owner_chat(env)
    await update_rows(
        env,
        Conversation,
        Conversation.id == chat.conversation_id,
        autonomy_paused_reason="takeover",
    )
    await deliver(env, private_message(OWNER, "stop"))
    assert (await owner_account(env)).autonomy_paused

    await deliver(env, private_message(OWNER, "/resume"))

    assert not (await owner_account(env)).autonomy_paused
    async with env.factory() as session:
        reason = await session.scalar(
            select(Conversation.autonomy_paused_reason).where(
                Conversation.id == chat.conversation_id
            )
        )
    assert reason == "takeover"
    assert env.sender.texts[-1] == RESUMED_TEXT
