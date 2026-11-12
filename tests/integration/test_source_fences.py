"""C6/H10: synthetic source changes cannot certify generated text as fresh."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select, text, update

from assistant.actions.models import Action, ContextFenceRecord
from assistant.actions.proposals import ProposalRequest, propose
from assistant.actions.states import ActionKind, ActionOrigin, ActionState
from assistant.agent.drafting import DraftStatus, Stale, propose_draft
from assistant.conversations import read
from assistant.conversations.fences import (
    capture_fence,
    check_fence,
    observe_conversations,
)
from assistant.conversations.models import Conversation, Message
from assistant.core.jobs import DraftReason
from assistant.db.uow import transaction
from assistant.llm.port import LLMRequest
from assistant.privacy.deletion import purge_conversation
from assistant.privacy.retention import sweep_message_content
from tests.integration.actions_env import seed_chat as seed_account_chat
from tests.integration.actions_env import seed_run
from tests.integration.business_env import CONTACT, business_message, deleted_messages, deliver
from tests.integration.drafting_env import (
    DraftEnv,
    contact_says,
    drafting_environment,
    owner_says,
    seed_chat,
)
from tests.integration.execution_env import caught_up, harness
from tests.integration.p2_factories import seed_conversation
from tests.integration.test_draft_outdating import approve_card, draft_card, states
from tests.integration.test_reply_tools import run_and_turn
from tests.unit.test_draft_owner_units import owner_output
from tests.unit.test_draft_units import output

pytestmark = pytest.mark.integration


@pytest.fixture
async def denv(migrated_url: str) -> AsyncIterator[DraftEnv]:
    async with drafting_environment(migrated_url, trigger="off") as built:
        yield built


async def revision(denv: DraftEnv, conversation_id: int) -> int:
    async with denv.env.factory() as session:
        value = await session.scalar(
            select(Conversation.content_revision).where(
                Conversation.id == conversation_id,
            )
        )
    assert value is not None
    return value


@pytest.mark.parametrize(
    "change", ["arrival", "edit_base", "edit_other", "delete_base", "delete_other", "manual"]
)
async def test_changed_sources_during_generation_warn_without_holding_locks(
    denv: DraftEnv,
    change: str,
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    _, turn_id = await run_and_turn(denv, account_id)
    base = await contact_says(denv, "SYNTH-BASE")

    async def during_call(_: LLMRequest) -> dict[str, Any]:
        # This NOWAIT fails immediately if a provider call holds a source lock.
        async with transaction(denv.env.factory) as uow:
            await uow.session.execute(
                text("SELECT id FROM app.accounts WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": account_id},
            )
            await uow.session.execute(
                text("SELECT id FROM app.conversations WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": conversation_id},
            )
        if change == "arrival":
            await contact_says(denv, "SYNTH-ARRIVAL")
        elif change.startswith("edit"):
            await deliver(
                denv.env,
                business_message(
                    message_id=base if change == "edit_base" else 1,
                    message_text="SYNTH-EDIT",
                    edit=True,
                ),
            )
        elif change.startswith("delete"):
            await deliver(
                denv.env,
                deleted_messages(
                    message_ids=[base if change == "delete_base" else 1],
                ),
            )
        else:
            # Delayed ingestion/same Telegram second: the captured manual ID
            # must detect takeover even when date precedes action creation.
            await owner_says(denv, "SYNTH-MANUAL", date=int(denv.env.clock.now().timestamp()))
        return output()

    denv.script(during_call)
    async with asyncio.timeout(10):
        outcome = await denv.drafter().draft(
            account_id=account_id,
            conversation_id=conversation_id,
            reason=DraftReason.EVAL,
        )
    assert outcome.status is DraftStatus.DRAFTED
    async with transaction(denv.env.factory) as uow:
        result = await propose_draft(
            uow.session,
            account_id=account_id,
            conversation_id=conversation_id,
            outcome=outcome,
            origin=ActionOrigin.AGENT_PROPOSAL,
            run_id=None,
            tool_call_id=None,
            trigger_turn_id=turn_id,
            clock=denv.env.clock,
            config=denv.config,
            presenter=None,
        )
        assert not isinstance(result, Stale)
        action = await uow.session.get(Action, result.action_id)
        assert action is not None
        assert action.state == "awaiting_approval"
        assert action.approval_token is not None
        assert action.freshness_warning is not None
        if change == "manual":
            assert action.freshness_warning == "takeover"


async def test_revision_advances_once_per_meaningful_input_not_replays_or_noops(
    denv: DraftEnv,
) -> None:
    _, conversation_id = await seed_chat(denv, mode="observe")
    before = await revision(denv, conversation_id)
    await deliver(denv.env, business_message(message_id=1, message_text="SYNTH-HELLO"))
    assert await revision(denv, conversation_id) == before
    await deliver(
        denv.env,
        business_message(
            message_id=1,
            message_text="SYNTH-HELLO",
            edit=True,
        ),
    )
    assert await revision(denv, conversation_id) == before
    await deliver(
        denv.env,
        business_message(
            message_id=1,
            message_text="SYNTH-CHANGED",
            edit=True,
        ),
    )
    assert await revision(denv, conversation_id) == before + 1
    await deliver(denv.env, deleted_messages(message_ids=[1]))
    assert await revision(denv, conversation_id) == before + 2
    await deliver(denv.env, deleted_messages(message_ids=[1, 999999]))
    assert await revision(denv, conversation_id) == before + 2


@pytest.mark.parametrize("change", ["edit", "deletion", "retention"])
async def test_reader_observation_is_from_the_content_snapshot_and_detects_later_changes(
    denv: DraftEnv,
    change: str,
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    async with transaction(denv.env.factory) as uow:
        run_id = await seed_run(uow.session, account_id)
        page = await read.conversation_page(uow.session, account_id, conversation_id)
        assert page.messages[0].text == "SYNTH-HELLO"
        fence_id = await capture_fence(
            uow.session,
            account_id=account_id,
            run_id=run_id,
            request=None,
            reads=(page.observed,),
            requested_at=denv.env.clock.now(),
        )
    if change == "edit":
        await deliver(
            denv.env,
            business_message(
                message_id=1,
                message_text="SYNTH-EDIT",
                edit=True,
            ),
        )
    elif change == "deletion":
        await deliver(denv.env, deleted_messages(message_ids=[1]))
    else:
        denv.env.clock.advance(days=91)
        assert await sweep_message_content(denv.env.factory, denv.env.clock) == 1
    assert await revision(denv, conversation_id) > page.observed.content_revision
    async with transaction(denv.env.factory) as uow:
        checked = await check_fence(
            uow.session,
            account_id=account_id,
            fence_id=fence_id,
            run_id=run_id,
            request_id=None,
            expected_request_version=None,
        )
        assert not checked.current


async def test_mixed_read_revisions_never_replace_old_proof_with_current_revision(
    denv: DraftEnv,
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    async with transaction(denv.env.factory) as uow:
        run_id = await seed_run(uow.session, account_id)
        observed = await observe_conversations(
            uow.session,
            account_id=account_id,
            conversation_ids=(conversation_id,),
        )
        await capture_fence(
            uow.session,
            account_id=account_id,
            run_id=run_id,
            request=None,
            reads=observed,
            requested_at=denv.env.clock.now(),
        )
    await deliver(denv.env, business_message(message_id=1, message_text="SYNTH-EDIT", edit=True))
    async with transaction(denv.env.factory) as uow:
        observed = await observe_conversations(
            uow.session,
            account_id=account_id,
            conversation_ids=(conversation_id,),
        )
        fence_id = await capture_fence(
            uow.session,
            account_id=account_id,
            run_id=run_id,
            request=None,
            reads=observed,
            requested_at=denv.env.clock.now(),
        )
        assert not (
            await check_fence(
                uow.session,
                account_id=account_id,
                fence_id=fence_id,
                run_id=run_id,
                request_id=None,
                expected_request_version=None,
            )
        ).current
        other_run = await seed_run(uow.session, account_id)
        assert not (
            await check_fence(
                uow.session,
                account_id=account_id,
                fence_id=fence_id,
                run_id=other_run,
                request_id=None,
                expected_request_version=None,
            )
        ).current


async def test_another_accounts_fence_is_unavailable(denv: DraftEnv) -> None:
    account_id, _ = await seed_chat(denv, mode="observe")
    async with transaction(denv.env.factory) as uow:
        foreign = await seed_account_chat(uow.session, denv.env.clock.now())
        assert foreign.context_fence_id is not None
        result = await check_fence(
            uow.session,
            account_id=account_id,
            fence_id=foreign.context_fence_id,
            request_id=None,
            expected_request_version=None,
        )
        assert not result.current
        assert result.reason == "source_unavailable"


async def test_scope_purge_removes_fences_clears_derived_text_and_keeps_revision_monotonic(
    denv: DraftEnv,
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    action_id = await draft_card(denv, account_id, conversation_id)
    before = await revision(denv, conversation_id)
    async with transaction(denv.env.factory) as uow:
        await purge_conversation(
            uow.session,
            account_id=account_id,
            conversation_id=conversation_id,
            business_chat_id=CONTACT,
            clock=denv.env.clock,
        )
    assert await revision(denv, conversation_id) > before
    async with denv.env.factory() as session:
        action = await session.get(Action, action_id)
        assert action is not None
        assert action.text is None
        assert action.context_fence_id is None
        assert (
            await session.scalar(
                select(ContextFenceRecord.id).where(
                    ContextFenceRecord.account_id == account_id,
                )
            )
            is None
        )


async def test_non_base_deletion_after_card_creation_invalidates_pending_card(
    denv: DraftEnv,
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    await contact_says(denv, "SYNTH-BASE")
    action_id = await draft_card(denv, account_id, conversation_id)
    await deliver(denv.env, deleted_messages(message_ids=[1]))
    assert await states(denv, action_id) == [("outdated", "source_deleted")]


async def test_execution_rechecks_revision_even_when_no_listener_outdated_the_card(
    denv: DraftEnv,
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    action_id = await draft_card(denv, account_id, conversation_id)
    await approve_card(denv, action_id)
    await caught_up(denv.env)
    async with transaction(denv.env.factory) as uow:
        await uow.session.execute(
            update(Conversation)
            .where(
                Conversation.account_id == account_id,
                Conversation.id == conversation_id,
            )
            .values(content_revision=Conversation.content_revision + 1)
        )
    target = harness(denv.env)
    result = await target.execute(action_id)
    assert result.state is ActionState.OUTDATED
    assert target.sender.business_calls == 0


async def test_legacy_generated_action_without_fence_fails_closed_at_execution(
    denv: DraftEnv,
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    action_id = await draft_card(denv, account_id, conversation_id)
    await approve_card(denv, action_id)
    await caught_up(denv.env)
    async with transaction(denv.env.factory) as uow:
        await uow.session.execute(
            update(Action).where(Action.id == action_id).values(context_fence_id=None)
        )
    target = harness(denv.env)
    result = await target.execute(action_id)
    assert result.state is ActionState.OUTDATED
    assert target.sender.business_calls == 0


async def test_other_chat_dependency_invalidates_output_card_and_clears_text_on_purge(
    denv: DraftEnv,
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    async with transaction(denv.env.factory) as uow:
        other = await seed_conversation(uow.session, account_id=account_id)
        run_id = await seed_run(uow.session, account_id)
        reads = await observe_conversations(
            uow.session,
            account_id=account_id,
            conversation_ids=(conversation_id, other.id),
        )
        fence_id = await capture_fence(
            uow.session,
            account_id=account_id,
            run_id=run_id,
            request=None,
            reads=reads,
            requested_at=denv.env.clock.now(),
        )
        base_id = await uow.session.scalar(
            select(Message.id).where(
                Message.account_id == account_id,
                Message.conversation_id == conversation_id,
            )
        )
        proposal = await propose(
            uow.session,
            ProposalRequest(
                account_id=account_id,
                conversation_id=conversation_id,
                origin=ActionOrigin.AGENT_PROPOSAL,
                kind=ActionKind.SEND_MESSAGE,
                text="SYNTH-DERIVED",
                base_message_id=base_id,
                shown_through_message_id=base_id,
                run_id=run_id,
                tool_call_id="synthetic-proposal",
                trigger_turn_id=None,
                draft_run_id=None,
                context_fence_id=fence_id,
            ),
            clock=denv.env.clock,
            config=denv.config,
        )
        assert proposal.state is ActionState.AWAITING_APPROVAL
        other_id = other.id
        other_chat_id = other.business_chat_id
    await deliver(
        denv.env,
        business_message(
            chat_id=other_chat_id,
            from_id=other_chat_id,
            message_id=1,
            message_text="SYNTH-OTHER-CHAT-ARRIVAL",
        ),
    )
    async with denv.env.factory() as session:
        warned = await session.get(Action, proposal.action_id)
        assert warned is not None
        assert warned.freshness_warning == "new_contact_message"
        assert warned.approval_token is not None
    async with transaction(denv.env.factory) as uow:
        await purge_conversation(
            uow.session,
            account_id=account_id,
            conversation_id=other_id,
            business_chat_id=other_chat_id,
            clock=denv.env.clock,
        )
    async with denv.env.factory() as session:
        action = await session.get(Action, proposal.action_id)
        assert action is not None
        assert action.state == "outdated"
        assert action.text is None
        assert action.context_fence_id is None


async def test_owner_requested_generation_warns_on_a_delayed_same_second_manual_reply(
    denv: DraftEnv,
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)

    async def manual_reply(_: LLMRequest) -> dict[str, Any]:
        await owner_says(denv, "SYNTH-MANUAL")
        return owner_output()

    denv.script(manual_reply)
    outcome = await denv.drafter().draft_owner(
        account_id=account_id,
        conversation_id=conversation_id,
        owner_run_id=run_id,
        instruction="SYNTH-OWNER-INSTRUCTION",
    )
    async with transaction(denv.env.factory) as uow:
        result = await propose_draft(
            uow.session,
            account_id=account_id,
            conversation_id=conversation_id,
            outcome=outcome,
            origin=ActionOrigin.AGENT_PROPOSAL,
            run_id=run_id,
            tool_call_id="synthetic-call",
            trigger_turn_id=turn_id,
            clock=denv.env.clock,
            config=denv.config,
            presenter=None,
        )
        assert not isinstance(result, Stale)
        action = await uow.session.get(Action, result.action_id)
        assert action is not None
        assert action.freshness_warning == "takeover"
        assert action.approval_token is not None


async def test_explicit_retained_target_outside_newest_window_is_read_directly(
    denv: DraftEnv,
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    async with denv.env.factory() as session:
        earlier = await session.scalar(
            select(Message.id).where(
                Message.account_id == account_id,
                Message.conversation_id == conversation_id,
            )
        )
    assert earlier is not None
    for index in range(31):
        await contact_says(denv, f"SYNTH-LATER-{index}")
    run_id, _ = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    outcome = await denv.drafter().draft_owner(
        account_id=account_id,
        conversation_id=conversation_id,
        owner_run_id=run_id,
        answered_message_id=earlier,
    )
    assert outcome.status is DraftStatus.DRAFTED
    assert outcome.base_message_id == earlier
    assert any("SYNTH-HELLO" in item.text for item in denv.fake.requests[-1].items)


@pytest.mark.parametrize("missing", ["deleted", "purged", "unknown"])
async def test_unavailable_explicit_target_never_falls_back_to_latest(
    denv: DraftEnv,
    missing: str,
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    async with transaction(denv.env.factory) as uow:
        selected = await uow.session.scalar(
            select(Message).where(
                Message.account_id == account_id,
                Message.conversation_id == conversation_id,
            )
        )
        assert selected is not None
        target_id = selected.id
        if missing == "purged":
            selected.text = None
            selected.purged_at = denv.env.clock.now()
    if missing == "deleted":
        await deliver(denv.env, deleted_messages(message_ids=[1]))
    elif missing == "unknown":
        target_id += 999999999
    await contact_says(denv, "SYNTH-LATEST")
    run_id, _ = await run_and_turn(denv, account_id)
    outcome = await denv.drafter().draft_owner(
        account_id=account_id,
        conversation_id=conversation_id,
        owner_run_id=run_id,
        answered_message_id=target_id,
    )
    assert outcome.status is DraftStatus.NOT_DRAFTABLE
    assert outcome.error_class == "source_unavailable"
    assert denv.fake.requests == []
