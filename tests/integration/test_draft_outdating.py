"""S04.2 §3.8: what ingestion does to drafts (`ActionsIngestionListener`; FR-DRF-05).

Every change arrives through the real business pipeline (inbox → pipeline →
listener, one transaction); cards are made with the real `Drafter` (scripted
`FakeLLM`) and S04.1's `propose()`. Synthetic text only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select, update

from assistant.accounts.models import AuditLogEntry
from assistant.actions.lifecycle import approve
from assistant.actions.models import Action
from assistant.actions.states import ActionOrigin
from assistant.agent.drafting import Stale, propose_draft
from assistant.conversations.models import Conversation, Message
from assistant.core.jobs import DraftReason
from assistant.db.uow import transaction
from assistant.inbox.models import InboundUpdate
from tests.integration.business_env import (
    OWNER,
    business_message,
    deleted_messages,
    deliver,
)
from tests.integration.drafting_env import (
    DraftEnv,
    assistant_sent,
    contact_says,
    drafting_environment,
    message_row_id,
    output,
    owner_automated,
    owner_says,
    seed_chat,
)
from tests.integration.p2_factories import seed_action

pytestmark = pytest.mark.integration


@pytest.fixture
async def denv(migrated_url: str) -> AsyncIterator[DraftEnv]:
    # Observe-mode tests too: outdating does not depend on Assist.
    async with drafting_environment(migrated_url, trigger="off") as built:
        yield built


async def draft_card(denv: DraftEnv, account_id: int, conversation_id: int) -> int:
    """A proactive draft of the chat as it is now, proposed as a card."""
    denv.script(output())
    outcome = await denv.drafter().draft(
        account_id=account_id, conversation_id=conversation_id, reason=DraftReason.EVAL
    )
    async with transaction(denv.env.factory) as uow:
        proposal = await propose_draft(
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
    assert not isinstance(proposal, Stale)
    assert proposal.state.value == "awaiting_approval"
    return proposal.action_id


async def approve_card(denv: DraftEnv, action_id: int) -> None:
    async with transaction(denv.env.factory) as uow:
        row = await uow.session.get(Action, action_id)
        assert row is not None
        assert row.approval_token is not None
        result = await approve(
            uow.session,
            account_id=row.account_id,
            token=row.approval_token,
            now=denv.env.clock.now(),
        )
        assert result.status.value == "approved"


async def states(denv: DraftEnv, *action_ids: int) -> list[tuple[str, str | None]]:
    async with denv.env.factory() as session:
        rows = {
            row.id: (row.state, row.state_reason)
            for row in await session.scalars(select(Action).where(Action.id.in_(action_ids)))
        }
    return [rows[i] for i in action_ids]


async def audits(denv: DraftEnv, event: str, **where: Any) -> list[AuditLogEntry]:
    async with denv.env.factory() as session:
        statement = select(AuditLogEntry).where(AuditLogEntry.event == event)
        for key, value in where.items():
            statement = statement.where(getattr(AuditLogEntry, key) == value)
        return list(await session.scalars(statement.order_by(AuditLogEntry.id)))


async def test_a_new_contact_message_outdates_a_pending_draft(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    card = await draft_card(denv, account_id, conversation)
    await contact_says(denv, "SYNTH-NEWER")
    assert await states(denv, card) == [("outdated", "new_contact_message")]
    assert denv.presenter.synced == [card]
    [moved] = await audits(denv, "action_outdated", subject_id=card)
    assert moved.details == {
        "from": "awaiting_approval",
        "to": "outdated",
        "version": 1,
        "reason": "new_contact_message",
    }


async def test_a_new_contact_message_leaves_an_approved_action_to_the_send_check(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    card = await draft_card(denv, account_id, conversation)
    await approve_card(denv, card)
    await contact_says(denv, "SYNTH-NEWER")
    # `not_stale` stops it at execution (S04.4), with the exact reason there.
    assert await states(denv, card) == [("approved", None)]


async def test_an_owner_manual_message_outdates_pending_and_approved_actions(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    approved = await draft_card(denv, account_id, conversation)
    await approve_card(denv, approved)
    live = await draft_card(denv, account_id, conversation)
    await owner_says(denv, "SYNTH-OWNER-TOOK-OVER")
    assert await states(denv, approved, live) == [
        ("outdated", "takeover"),
        ("outdated", "takeover"),
    ]
    assert sorted(denv.presenter.synced) == sorted([approved, live])


async def test_an_owner_edit_outdates_without_pausing_autonomy(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    owner_message = await owner_says(denv, "SYNTH-OWNER-FIRST")
    async with denv.env.factory() as session, session.begin():
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation)
            .values(autonomy_paused_reason=None)
        )
    takeovers = len(await audits(denv, "takeover_detected"))
    card = await draft_card(denv, account_id, conversation)
    await deliver(
        denv.env,
        business_message(
            from_id=OWNER, message_id=owner_message, message_text="SYNTH-OWNER-EDITED", edit=True
        ),
    )
    assert await states(denv, card) == [("outdated", "owner_edit")]
    async with denv.env.factory() as session:
        paused = await session.scalar(
            select(Conversation.autonomy_paused_reason).where(Conversation.id == conversation)
        )
    assert paused is None
    assert len(await audits(denv, "takeover_detected")) == takeovers


async def test_a_contact_edit_of_the_base_message_outdates_and_audits(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    base = await contact_says(denv, "SYNTH-BASE")
    card = await draft_card(denv, account_id, conversation)
    # Make the card current again: the contact message above outdated nothing yet.
    base_row = await message_row_id(denv.env, base)
    async with denv.env.factory() as session:
        row = await session.get(Action, card)
        assert row is not None
        assert row.base_message_id == base_row
    await deliver(
        denv.env, business_message(message_id=base, message_text="SYNTH-BASE-EDITED", edit=True)
    )
    assert await states(denv, card) == [("outdated", "source_edited")]
    [edited] = await audits(denv, "source_edited_after_action", subject_id=card)
    assert edited.subject_type == "action"
    assert edited.details == {"message_id": base_row}


async def test_a_contact_edit_of_another_message_changes_nothing(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    await contact_says(denv, "SYNTH-BASE")  # the newest contact message: the base
    card = await draft_card(denv, account_id, conversation)
    # Message 1 was shown to the draft too, but it is not the base.
    await deliver(
        denv.env, business_message(message_id=1, message_text="SYNTH-FIRST-EDITED", edit=True)
    )
    assert await states(denv, card) == [("awaiting_approval", None)]
    assert await audits(denv, "source_edited_after_action") == []


async def test_deleting_the_base_message_outdates(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    base = await contact_says(denv, "SYNTH-BASE")
    card = await draft_card(denv, account_id, conversation)
    await deliver(denv.env, deleted_messages(message_ids=[base]))
    assert await states(denv, card) == [("outdated", "source_deleted")]
    assert denv.presenter.synced == [card]


async def test_deleting_an_assistant_sent_message_is_audited_without_attribution(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    sent = await assistant_sent(denv, "SYNTH-ASSISTANT-REPLY")
    async with denv.env.factory() as session, session.begin():
        action = await seed_action(
            session,
            account_id=account_id,
            conversation_id=conversation,
            state="succeeded",
            version=1,
        )
        sent_row = await session.scalar(select(Message).where(Message.telegram_message_id == sent))
        assert sent_row is not None
        assert sent_row.direction == "assistant_sent"
        sent_row.action_id = action.id
        sent_id, action_id = sent_row.id, action.id
    await deliver(denv.env, deleted_messages(message_ids=[sent]))
    [deleted] = await audits(denv, "assistant_message_deleted")
    assert deleted.actor == "system"
    assert (deleted.subject_type, deleted.subject_id) == ("conversation", conversation)
    assert deleted.details == {"message_id": sent_id, "action_id": action_id}  # never "by"
    assert await states(denv, action_id) == [("succeeded", None)]


async def test_outdating_is_idempotent_when_an_update_is_requeued(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    first = await draft_card(denv, account_id, conversation)
    update_payload = business_message(message_id=4242, message_text="SYNTH-NEWER")
    await deliver(denv.env, update_payload)
    assert await states(denv, first) == [("outdated", "new_contact_message")]
    second = await draft_card(denv, account_id, conversation)

    # `inbox requeue`: the same update is processed again.
    async with denv.env.factory() as session, session.begin():
        await session.execute(
            update(InboundUpdate)
            .where(InboundUpdate.update_id == update_payload["update_id"])
            .values(status="pending", processed_at=None)
        )
    await denv.env.processor.process_batch()
    assert await states(denv, first, second) == [
        ("outdated", "new_contact_message"),
        ("awaiting_approval", None),
    ]
    assert len(await audits(denv, "action_outdated", subject_id=first)) == 1


async def test_owner_automated_and_assistant_sent_messages_change_nothing(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    card = await draft_card(denv, account_id, conversation)
    await owner_automated(denv)
    await assistant_sent(denv)
    assert await states(denv, card) == [("awaiting_approval", None)]
    assert denv.presenter.synced == []


async def test_executing_and_terminal_actions_are_never_touched(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    base = await contact_says(denv, "SYNTH-BASE")
    base_row = await message_row_id(denv.env, base)
    untouchable = ("executing", "delivery_uncertain", "succeeded", "failed", "rejected", "expired")
    async with denv.env.factory() as session, session.begin():
        ids = [
            (
                await seed_action(
                    session,
                    account_id=account_id,
                    conversation_id=conversation,
                    state=state,
                    version=1,
                    base_message_id=base_row,
                )
            ).id
            for state in untouchable
        ]
    await contact_says(denv, "SYNTH-NEWER")
    owner_message = await owner_says(denv, "SYNTH-OWNER")
    await deliver(
        denv.env,
        business_message(
            from_id=OWNER, message_id=owner_message, message_text="SYNTH-OWNER-EDIT", edit=True
        ),
    )
    await deliver(
        denv.env, business_message(message_id=base, message_text="SYNTH-BASE-EDIT", edit=True)
    )
    await deliver(denv.env, deleted_messages(message_ids=[base]))
    assert await states(denv, *ids) == [(state, None) for state in untouchable]
    # The edit of their base message is still on record for each of them.
    assert len(await audits(denv, "source_edited_after_action")) == len(untouchable)
