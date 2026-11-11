"""H4/H10 settlement with production source fences and synthetic transports."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.actions import lifecycle
from assistant.actions.models import Action, ContextFenceRecord
from assistant.actions.owner_fence import StoredOwnerProposalFence
from assistant.actions.request_models import OwnerRequest
from assistant.agent.tools.registry import ToolContext
from assistant.bootstrap import build_owner_runtime
from assistant.config import Settings
from assistant.conversations.fences import ContextFence, capture_fence, observe_conversations
from assistant.conversations.models import Message
from assistant.core.errors import NotInAccountError
from assistant.llm.port import LLMRequest, LLMResponse, ToolResultItem
from assistant.privacy.deletion import purge_conversation
from assistant.telegram.control.approvals import load_card_view, render_card
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import text_response, tool_response
from tests.integration.actions_env import seed_chat as seed_other_account
from tests.integration.actions_env import seed_run
from tests.integration.agent_env import PRODUCTION_FLAGS, ask, gated
from tests.integration.business_env import T_BASE, business_message, deleted_messages, deliver
from tests.integration.drafting_env import (
    DraftEnv,
    actions_of,
    contact_says,
    drafting_environment,
    seed_chat,
)
from tests.integration.execution_env import caught_up, harness
from tests.integration.p2_factories import seed_conversation
from tests.integration.test_draft_outdating import approve_card
from tests.integration.test_reply_tools import call, context, generated_message_args, run_and_turn

pytestmark = pytest.mark.integration
FLAGS = frozenset({"tool.propose_owner_message", "tool.revise_draft"})


@pytest.fixture
async def denv(migrated_url: str) -> AsyncIterator[DraftEnv]:
    async with drafting_environment(migrated_url, trigger="off") as built:
        yield built


async def snapshot(ctx: ToolContext, conversation_id: int) -> int:
    async with ctx.services.session_factory() as session, session.begin():
        reads = await observe_conversations(
            session, account_id=ctx.account_id, conversation_ids=(conversation_id,)
        )
        return await capture_fence(
            session,
            account_id=ctx.account_id,
            run_id=ctx.run_id,
            request=None,
            reads=reads,
            requested_at=ctx.services.clock.now(),
        )


async def prepare(denv: DraftEnv) -> tuple[ToolContext, dict[str, Any]]:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    args = await generated_message_args(denv, account_id, conversation_id)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id, owner_fence=StoredOwnerProposalFence())
    args["context_fence_id"] = await snapshot(ctx, conversation_id)
    return ctx, args


@pytest.mark.parametrize("change", [None, "arrival", "edit", "deletion"])
async def test_main_model_settles_observed_sources_into_reviewable_card(
    denv: DraftEnv, change: str | None
) -> None:
    ctx, args = await prepare(denv)
    if change == "arrival":
        await contact_says(denv, "SYNTH later arrival")
    elif change == "edit":
        await deliver(
            denv.env, business_message(message_id=1, message_text="SYNTH edit", edit=True)
        )
    elif change == "deletion":
        await deliver(denv.env, deleted_messages(message_ids=[1]))
    result = await call(ctx, "propose_owner_message", flags=FLAGS, **args)
    assert result["proposal_status"] == "staged"
    [action] = await actions_of(denv.env, args["conversation_id"])
    assert action.state == "awaiting_approval"
    assert action.text == args["text"]
    assert action.owner_request_id == args["request_id"]
    assert action.context_fence_id is not None
    assert action.draft_run_id == ctx.run_id
    async with denv.env.factory() as session:
        request = await session.get(OwnerRequest, args["request_id"])
        record = await session.get(ContextFenceRecord, action.context_fence_id)
        assert request is not None
        assert record is not None
        facts = ContextFence.model_validate(record.facts)
        assert facts.request_id == request.id
        assert facts.request_version == request.version
        assert action.requested_at == request.requested_at
        view = await load_card_view(
            session, account_id=ctx.account_id, action_id=action.id, sending_enabled=True
        )
    assert view is not None
    rendered = render_card(view)
    if change is None:
        assert action.freshness_warning is None
    else:
        assert (
            action.freshness_warning
            == {
                "arrival": "new_contact_message",
                "edit": "source_edited",
                "deletion": "source_deleted",
            }[change]
        )
        assert "Warning:" in rendered.text
        assert rendered.keyboard is not None
        assert [b.text for row in rendered.keyboard.inline_keyboard for b in row] == [
            "Approve anyway",
            "Re-draft",
            "Discard",
        ]
    await approve_card(denv, action.id)
    await caught_up(denv.env)
    target = harness(denv.env)
    outcome = await target.execute(action.id)
    assert outcome.status.value == "sent"
    await target.execute(action.id)
    assert target.sender.business_calls == 1
    assert target.sender.plain_sends[0]["text"] == args["text"]


@pytest.mark.parametrize("identity", ["missing", "other_run", "foreign", "wrong_request"])
async def test_unissued_or_unrelated_fence_has_uniform_failure(
    denv: DraftEnv, identity: str
) -> None:
    ctx, args = await prepare(denv)
    async with denv.env.factory() as session, session.begin():
        record = await session.get(ContextFenceRecord, args["context_fence_id"])
        assert record is not None
        if identity == "missing":
            args["context_fence_id"] = 999_999_999
        elif identity == "other_run":
            record.run_id = await seed_run(session, ctx.account_id)
        elif identity == "foreign":
            # The scoped lookup must not reveal whether an opaque id exists.
            other = await seed_other_account(session, denv.env.clock.now())
            args["context_fence_id"] = other.context_fence_id
        else:
            record.facts = {
                **record.facts,
                "request_id": args["request_id"],
                "request_version": 999_999,
            }
    result = await call(ctx, "propose_owner_message", flags=FLAGS, **args)
    assert result["proposal_status"] == "not_available"
    assert result["reason"] == "not_found"
    assert result["action"] is None
    assert await actions_of(denv.env, args["conversation_id"]) == []


async def test_earlier_selected_fence_cannot_omit_later_other_chat_read(denv: DraftEnv) -> None:
    ctx, args = await prepare(denv)
    async with denv.env.factory() as session, session.begin():
        other = await seed_conversation(session, account_id=ctx.account_id)
        other_id, other_chat_id = other.id, other.business_chat_id
    await snapshot(ctx, other_id)
    await deliver(
        denv.env,
        business_message(
            chat_id=other_chat_id,
            from_id=other_chat_id,
            message_id=1,
            message_text="SYNTH other chat changed",
        ),
    )
    result = await call(ctx, "propose_owner_message", flags=FLAGS, **args)
    assert result["proposal_status"] == "staged"
    [action] = await actions_of(denv.env, args["conversation_id"])
    assert action.freshness_warning == "new_contact_message"
    async with denv.env.factory() as session:
        record = await session.get(ContextFenceRecord, action.context_fence_id)
        assert record is not None
        facts = ContextFence.model_validate(record.facts)
        assert {item.conversation_id for item in facts.conversations} == {
            args["conversation_id"],
            other_id,
        }


async def test_erased_dependency_invalidates_run_even_after_a_later_recipient_read(
    denv: DraftEnv,
) -> None:
    ctx, args = await prepare(denv)
    async with denv.env.factory() as session, session.begin():
        other = await seed_conversation(session, account_id=ctx.account_id)
        other_id, other_chat_id = other.id, other.business_chat_id
    # B's text is still in the paused run's context when its records are erased.
    await deliver(
        denv.env,
        business_message(
            chat_id=other_chat_id,
            from_id=other_chat_id,
            message_id=1,
            message_text="SYNTH confidential source for the composition",
        ),
    )
    await snapshot(ctx, other_id)
    async with denv.env.factory() as session, session.begin():
        await purge_conversation(
            session,
            account_id=ctx.account_id,
            conversation_id=other_id,
            business_chat_id=other_chat_id,
            clock=denv.env.clock,
        )
    # Reading A again cannot reconstruct an apparently clean dependency union.
    with pytest.raises(NotInAccountError):
        await snapshot(ctx, args["conversation_id"])
    # Nor may the model select an earlier surviving A fence to stage B-derived text.
    result = await call(ctx, "propose_owner_message", flags=FLAGS, **args)
    assert result["proposal_status"] == "not_available"
    assert await actions_of(denv.env, args["conversation_id"]) == []
    fresh_run, turn_id = await run_and_turn(denv, ctx.account_id)
    fresh_context = context(
        denv, ctx.account_id, fresh_run, turn_id, owner_fence=StoredOwnerProposalFence()
    )
    assert await snapshot(fresh_context, args["conversation_id"]) > 0


@pytest.mark.parametrize("tool", ["get_attention_list", "summarize_period"])
async def test_managed_summary_read_is_preserved_when_recipient_is_read_later(
    denv: DraftEnv, tool: str
) -> None:
    ctx, args = await prepare(denv)
    async with denv.env.factory() as session, session.begin():
        other = await seed_conversation(session, account_id=ctx.account_id)
        other_id, other_chat_id = other.id, other.business_chat_id
    await deliver(
        denv.env,
        business_message(
            chat_id=other_chat_id,
            from_id=other_chat_id,
            message_id=1,
            message_text="SYNTH can you review the options?",
        ),
    )
    # The synthetic Telegram date is later than the generic fixture's clock.
    denv.env.clock.set(datetime.fromtimestamp(T_BASE + 60, tz=UTC))
    values = (
        {"active_within_days": None, "limit": None}
        if tool == "get_attention_list"
        else {
            "since": (denv.env.clock.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M"),
            "until": None,
            "contact_id": None,
            "conversation_id": other_id,
        }
    )
    read = await call(ctx, tool, flags=frozenset({f"tool.{tool}"}), **values)
    assert read["status"] == "ok"
    assert other_id in {item["conversation_id"] for item in read.get("conversations", [])} | {
        item["conversation_id"] for item in read.get("not_summarized", [])
    }
    # Read A through the real conversation tool, then compose using its opaque id.
    recipient = await call(
        ctx,
        "get_conversation",
        flags=frozenset({"tool.get_conversation"}),
        conversation_id=args["conversation_id"],
        contact_id=None,
        message_id=None,
        cursor=None,
        limit=None,
    )
    args["context_fence_id"] = recipient["context_fence_id"]
    await deliver(
        denv.env,
        business_message(
            chat_id=other_chat_id,
            from_id=other_chat_id,
            message_id=1,
            message_text="SYNTH changed options?",
            edit=True,
        ),
    )
    staged = await call(ctx, "propose_owner_message", flags=FLAGS, **args)
    assert staged["proposal_status"] == "staged"
    [action] = await actions_of(denv.env, args["conversation_id"])
    assert action.freshness_warning == "source_edited"
    async with denv.env.factory() as session:
        record = await session.get(ContextFenceRecord, action.context_fence_id)
        assert record is not None
        assert other_id in {
            item.conversation_id for item in ContextFence.model_validate(record.facts).conversations
        }


async def test_main_model_revision_binds_new_snapshot_and_preserves_warning(denv: DraftEnv) -> None:
    ctx, args = await prepare(denv)
    first = await call(ctx, "propose_owner_message", flags=FLAGS, **args)
    fence_id = await snapshot(ctx, args["conversation_id"])
    await contact_says(denv, "SYNTH arrives during rewrite")
    result = await call(
        ctx,
        "revise_draft",
        flags=FLAGS,
        call_id="rewrite",
        request_id=args["request_id"],
        request_version=first["request_version"],
        action_id=first["action_id"],
        expected_version=1,
        edit="rewrite",
        text="SYNTH complete replacement",
        source_ids=[],
        context_fence_id=fence_id,
    )
    assert result["proposal_status"] == "revised"
    [action] = await actions_of(denv.env, args["conversation_id"])
    assert action.version == 2
    assert action.freshness_warning == "new_contact_message"
    assert action.context_fence_id is not None
    async with denv.env.factory() as session:
        record = await session.get(ContextFenceRecord, action.context_fence_id)
        request = await session.get(OwnerRequest, args["request_id"])
        assert record is not None
        assert request is not None
        facts = ContextFence.model_validate(record.facts)
        assert facts.request_version == request.version
    await approve_card(denv, action.id)
    await caught_up(denv.env)
    target = harness(denv.env)
    assert (await target.execute(action.id)).status.value == "sent"


async def test_change_after_main_model_card_rotates_token_and_sends_shown_text_once(
    denv: DraftEnv,
) -> None:
    ctx, args = await prepare(denv)
    await call(ctx, "propose_owner_message", flags=FLAGS, **args)
    [original] = await actions_of(denv.env, args["conversation_id"])
    await contact_says(denv, "SYNTH arrives after card")
    [warned] = await actions_of(denv.env, args["conversation_id"])
    assert warned.approval_token != original.approval_token
    assert warned.freshness_warning == "new_contact_message"
    async with denv.env.factory() as session, session.begin():
        obsolete = await lifecycle.approve(
            session,
            account_id=ctx.account_id,
            token=original.approval_token or "",
            now=denv.env.clock.now(),
        )
    assert obsolete.status is lifecycle.ApproveStatus.NOT_ACTIVE
    await approve_card(denv, warned.id)
    await caught_up(denv.env)
    target = harness(denv.env)
    assert (await target.execute(warned.id)).status.value == "sent"
    await target.execute(warned.id)
    assert target.sender.business_calls == 1
    assert target.sender.plain_sends[0]["text"] == original.text


@pytest.mark.parametrize("reason", ["rights_changed", "superseded"])
async def test_explicit_rewrite_does_not_reopen_other_outdated_cards(
    denv: DraftEnv, reason: str
) -> None:
    ctx, args = await prepare(denv)
    staged = await call(ctx, "propose_owner_message", flags=FLAGS, **args)
    async with denv.env.factory() as session, session.begin():
        action = await session.get(Action, staged["action_id"])
        assert action is not None
        action.state = "outdated"
        action.state_reason = reason
        action.approval_token = None
        await session.flush()
        started = await lifecycle.start_revision(
            session,
            account_id=ctx.account_id,
            action_id=action.id,
            expected_version=action.version,
            turn_id=ctx.turn_id or 0,
            now=denv.env.clock.now(),
        )
    assert started is lifecycle.RevisionStart.NOT_ACTIVE


async def test_deleted_reply_target_during_main_composition_keeps_standalone_approval(
    denv: DraftEnv,
) -> None:
    ctx, args = await prepare(denv)
    async with denv.env.factory() as session, session.begin():
        target_id = await session.scalar(
            select(Message.id).where(
                Message.account_id == ctx.account_id,
                Message.conversation_id == args["conversation_id"],
            )
        )
        request = await session.get(OwnerRequest, args["request_id"])
        assert request is not None
        assert target_id is not None
        request.operation = "reply"
        request.target_message_id = target_id
    args.update(mode="reply", answered_message_id=target_id)
    await deliver(denv.env, deleted_messages(message_ids=[1]))
    staged = await call(ctx, "propose_owner_message", flags=FLAGS, **args)
    assert staged["proposal_status"] == "staged"
    [action] = await actions_of(denv.env, args["conversation_id"])
    assert action.base_message_id == target_id
    assert action.freshness_warning == "source_deleted"
    await approve_card(denv, action.id)
    await caught_up(denv.env)
    target = harness(denv.env)
    assert (await target.execute(action.id)).status.value == "sent"
    assert target.sender.plain_sends[0]["text"] == args["text"]
    assert "reply_parameters" not in target.sender.plain_sends[0]


@pytest.mark.parametrize("change", ["arrival", "edit", "deletion"])
async def test_bootstrap_runtime_warns_when_read_dependency_changes_during_fake_model_call(
    denv: DraftEnv, clean_env: None, change: str
) -> None:
    account_id, conversation_id = await seed_chat(denv, mode="observe")
    args = await generated_message_args(denv, account_id, conversation_id)
    turn_id = await ask(denv.env, "SYNTH compose the requested comparison")

    async def generate(request: LLMRequest) -> LLMResponse:
        [result] = [
            item
            for item in request.items
            if isinstance(item, ToolResultItem) and item.call_id == "read"
        ]
        observed = json.loads(result.output)
        args["context_fence_id"] = observed["context_fence_id"]
        # The real read-tool transaction must have ended before the model runs.
        async with denv.env.factory() as session, session.begin():
            await session.execute(
                text("SELECT id FROM app.accounts WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": account_id},
            )
            await session.execute(
                text("SELECT id FROM app.conversations WHERE id = :id FOR UPDATE NOWAIT"),
                {"id": conversation_id},
            )
        if change == "arrival":
            await contact_says(denv, "SYNTH during main model call")
        elif change == "edit":
            await deliver(
                denv.env, business_message(message_id=1, message_text="SYNTH edit", edit=True)
            )
        else:
            await deliver(denv.env, deleted_messages(message_ids=[1]))
        return tool_response(("compose", "propose_owner_message", json.dumps(args)))

    denv.fake.queue(
        tool_response(
            (
                "read",
                "get_conversation",
                json.dumps(
                    {
                        "conversation_id": conversation_id,
                        "contact_id": None,
                        "message_id": None,
                        "cursor": None,
                        "limit": None,
                    }
                ),
            )
        ),
        generate,
        text_response("SYNTH warned approval card is ready"),
    )
    runtime = build_owner_runtime(
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://synthetic:synthetic@db/assistant",
            feature_flags=",".join(sorted(PRODUCTION_FLAGS | FLAGS)),
        ),
        denv.env.factory,
        channel=FakeChannel(),
        clock=denv.env.clock,
        llm=gated(denv.env, denv.fake),
        approval_cards=denv.presenter,
        actions_config=denv.config,
    )
    await runtime.run(turn_id)
    [action] = await actions_of(denv.env, conversation_id)
    assert action.state == "awaiting_approval"
    assert (
        action.freshness_warning
        == {
            "arrival": "new_contact_message",
            "edit": "source_edited",
            "deletion": "source_deleted",
        }[change]
    )
    async with denv.env.factory() as session:
        view = await load_card_view(
            session, account_id=account_id, action_id=action.id, sending_enabled=True
        )
    assert view is not None
    markup = render_card(view).keyboard
    assert markup is not None
    assert [b.text for row in markup.inline_keyboard for b in row] == [
        "Approve anyway",
        "Re-draft",
        "Discard",
    ]
    assert len(denv.fake.requests) == 3
    offered = {tool.name for tool in denv.fake.requests[0].tools}
    assert "propose_owner_message" in offered
    assert "propose_reply" not in offered
    assert denv.fake.parsed == []
