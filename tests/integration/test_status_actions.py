"""The `/status` actions block (S04.4 §3.9): counts only, honest about
uncertain and failed replies (§6 invariant 11). Synthetic text."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from assistant.accounts.models import Account
from assistant.actions.models import Action
from assistant.actions.states import ActionOrigin, ActionState
from assistant.observability.status import ActionsStatus, StatusReport, build_status
from assistant.telegram.control.handlers import render_status
from tests.integration.business_env import Env
from tests.integration.execution_env import inserted, second_chat, seed, update_rows

pytestmark = pytest.mark.integration

CANARY = "CANARY-SYNTH-5a1c"


async def report(env: Env, account_id: int) -> StatusReport:
    async with env.factory() as session:
        account = await session.get(Account, account_id)
        assert account is not None
        return await build_status(session, account, env.clock.now(), monthly_budget_usd=None)


async def test_status_counts_pending_sending_uncertain_and_failed_actions(env: Env) -> None:
    chat = await seed(env)
    revising_chat = await second_chat(env, chat)  # one live card per chat
    other = await seed(env)  # another account's actions never count
    now = env.clock.now()
    dictated = ActionOrigin.OWNER_DICTATED
    ids = {
        "card": await inserted(env, chat, state=ActionState.AWAITING_APPROVAL, origin=dictated),
        "revising": await inserted(env, revising_chat, state=ActionState.REVISING, origin=dictated),
    }
    for name, state in {
        "approved": ActionState.APPROVED,
        "executing": ActionState.EXECUTING,
        "uncertain": ActionState.DELIVERY_UNCERTAIN,
        "old_uncertain": ActionState.DELIVERY_UNCERTAIN,
        "failed": ActionState.FAILED,
        "old_failed": ActionState.FAILED,
        "sent": ActionState.SUCCEEDED,
    }.items():
        ids[name] = await inserted(env, chat, state=state, origin=dictated)
    await inserted(env, other, state=ActionState.DELIVERY_UNCERTAIN)
    await update_rows(
        env, Action, Action.id == ids["old_uncertain"], updated_at=now - timedelta(days=8)
    )
    await update_rows(
        env, Action, Action.id == ids["old_failed"], updated_at=now - timedelta(days=2)
    )

    status = await report(env, chat.account_id)

    assert status.actions == ActionsStatus(
        awaiting_approval=2, sending=2, uncertain_7d=1, failed_24h=1
    )
    text = render_status(status, visibility="synthetic")
    assert (
        "Replies: 2 awaiting approval · 2 sending · 1 delivery uncertain — check the chat"
        " · 1 not sent today"
    ) in text


async def test_status_actions_block_is_content_free(env: Env) -> None:
    chat = await seed(env)
    for state in (ActionState.APPROVED, ActionState.DELIVERY_UNCERTAIN, ActionState.FAILED):
        await inserted(env, chat, state=state, text=f"{CANARY} reply")

    status = await report(env, chat.account_id)

    assert CANARY not in json.dumps(status.as_dict())
    assert CANARY not in render_status(status, visibility="synthetic")
    assert status.as_dict()["actions"] == {
        "awaiting_approval": 0,
        "sending": 1,
        "uncertain_7d": 1,
        "failed_24h": 1,
    }
