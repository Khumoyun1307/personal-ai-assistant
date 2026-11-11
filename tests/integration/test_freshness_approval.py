"""D-S17: source warnings leave the exact shown draft approvable."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from assistant.actions.lifecycle import ApproveStatus, approve
from assistant.actions.models import Action
from assistant.actions.states import ActionOrigin, ActionState
from assistant.agent.drafting import Stale, propose_draft
from assistant.core.jobs import DraftReason
from assistant.db.uow import transaction
from assistant.llm.port import LLMRequest
from assistant.telegram.control.approvals import load_card_view, render_card
from tests.fakes.sender import telegram_error
from tests.integration.business_env import business_message, deleted_messages, deliver
from tests.integration.drafting_env import (
    DraftEnv,
    contact_says,
    drafting_environment,
    output,
    seed_chat,
)
from tests.integration.execution_env import caught_up, harness
from tests.integration.test_draft_outdating import approve_card, draft_card

pytestmark = pytest.mark.integration


@pytest.fixture
async def denv(migrated_url: str) -> AsyncIterator[DraftEnv]:
    async with drafting_environment(migrated_url, trigger="off") as built:
        yield built


async def assert_choices(denv: DraftEnv, account_id: int, action_id: int) -> None:
    async with denv.env.factory() as session:
        view = await load_card_view(
            session, account_id=account_id, action_id=action_id, sending_enabled=True
        )
    assert view is not None
    card = render_card(view)
    assert "Warning:" in card.text
    assert card.keyboard is not None
    labels = [button.text for row in card.keyboard.inline_keyboard for button in row]
    assert labels == ["Approve anyway", "Re-draft", "Discard"]


@pytest.mark.parametrize("change", ["edit", "delete", "arrival"])
async def test_change_during_generation_still_presents_all_three_choices(
    denv: DraftEnv, change: str
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")

    async def during_call(_: LLMRequest) -> dict[str, Any]:
        if change == "edit":
            await deliver(
                denv.env,
                business_message(message_id=1, message_text="SYNTH-EDIT", edit=True),
            )
        elif change == "delete":
            await deliver(denv.env, deleted_messages(message_ids=[1]))
        else:
            await contact_says(denv, "SYNTH-NEW")
        return output()

    denv.script(during_call)
    outcome = await denv.drafter().draft(
        account_id=account_id, conversation_id=conversation_id, reason=DraftReason.EVAL
    )
    async with transaction(denv.env.factory) as uow:
        result = await propose_draft(
            uow.session,
            account_id=account_id,
            conversation_id=conversation_id,
            outcome=outcome,
            origin=ActionOrigin.AGENT_PROPOSAL,
            run_id=None,
            tool_call_id=None,
            trigger_turn_id=None,
            clock=denv.env.clock,
            config=denv.config,
            presenter=None,
        )
    assert not isinstance(result, Stale)
    await assert_choices(denv, account_id, result.action_id)


@pytest.mark.parametrize("change", ["edit", "delete", "arrival"])
async def test_warned_shown_version_can_be_approved_once_with_a_rotated_token(
    denv: DraftEnv, change: str
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    action_id = await draft_card(denv, account_id, conversation_id)
    async with denv.env.factory() as session:
        before = await session.get(Action, action_id)
        assert before is not None
        old_token, shown = before.approval_token, before.text
    assert old_token is not None
    if change == "edit":
        await deliver(
            denv.env, business_message(message_id=1, message_text="SYNTH-EDIT", edit=True)
        )
    elif change == "delete":
        await deliver(denv.env, deleted_messages(message_ids=[1]))
    else:
        await contact_says(denv, "SYNTH-NEW")
    await assert_choices(denv, account_id, action_id)
    async with transaction(denv.env.factory) as uow:
        rejected = await approve(
            uow.session, account_id=account_id, token=old_token, now=denv.env.clock.now()
        )
        assert rejected.status is ApproveStatus.NOT_ACTIVE
    await approve_card(denv, action_id)
    await caught_up(denv.env)
    target = harness(denv.env)
    result = await target.execute(action_id)
    assert result.state is ActionState.SUCCEEDED
    await target.execute(action_id)
    assert target.sender.business_calls == 1
    assert target.sender.plain_sends[0]["text"] == shown
    assert "reply_parameters" not in target.sender.plain_sends[0]
    async with denv.env.factory() as session:
        after = await session.get(Action, action_id)
        assert after is not None
        assert after.text == shown


async def test_telegram_refusal_of_approve_anyway_is_definite_and_not_retried(
    denv: DraftEnv,
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    action_id = await draft_card(denv, account_id, conversation_id)
    await deliver(denv.env, deleted_messages(message_ids=[1]))
    await approve_card(denv, action_id)
    await caught_up(denv.env)
    target = harness(denv.env)
    target.sender.plain_script = [telegram_error("Bad Request: PEER_ID_INVALID", status_code=400)]
    result = await target.execute(action_id)
    assert result.state is ActionState.EXPIRED
    await target.execute(action_id)
    assert target.sender.business_calls == 1
    async with denv.env.factory() as session:
        row = await session.get(Action, action_id)
        assert row is not None
        assert row.text is not None
        assert row.state != "delivery_uncertain"


async def test_a_further_source_change_after_approve_anyway_cannot_change_the_shown_body(
    denv: DraftEnv,
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    action_id = await draft_card(denv, account_id, conversation_id)
    await contact_says(denv, "SYNTH-NEW")
    await approve_card(denv, action_id)
    await deliver(denv.env, deleted_messages(message_ids=[1]))
    await caught_up(denv.env)
    target = harness(denv.env)
    assert (await target.execute(action_id)).state is ActionState.SUCCEEDED
    await target.execute(action_id)
    assert target.sender.business_calls == 1
