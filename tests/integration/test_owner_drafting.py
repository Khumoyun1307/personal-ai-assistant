"""Owner drafting: legacy PF-10 to PF-12 and H4/H6's staged one-brain cutover.

The owner's own words reach the draft writer (`draft_owner`) even in a
tainted run, an explicit owner request is never vetoed as "no reply
needed", a reply can target a named message or be a new message, and a
"dictated" text must really be the owner's. Calls go through the real
`ToolExecutor` (and, where the bug lived, the whole owner runtime) with the
production LLM wiring over a scripted `FakeLLM`. Synthetic text and names
only. Flag-on regressions use the strict main-model tools and v11; flag-off
regressions retain the historical writer and v10.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select, update
from structlog.testing import capture_logs

from assistant.accounts.models import Account, ControlTurn
from assistant.actions.request_models import OwnerRequest, OwnerRequestSource
from assistant.actions.requests import RequestApplied, SetState, load_request, update_request
from assistant.agent import prompt_registry
from assistant.agent.draft_owner_schemas import WordBounds
from assistant.agent.drafting import DraftStatus
from assistant.agent.models import AgentRun
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime
from assistant.agent.tools.registry import default_registry
from assistant.agent.tools.replies import REQUEST_DIGEST
from assistant.conversations.models import Contact, Conversation, Message
from assistant.core.errors import NotInAccountError
from assistant.core.jobs import DraftReason
from assistant.llm.port import (
    LLMRequest,
    LLMResponse,
    Provenance,
    StopReason,
    TextItem,
    ToolResultItem,
)
from assistant.telegram.control.approvals import load_card_view, render_card
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import text_response, tool_response
from tests.integration.agent_env import PRODUCTION_FLAGS, args, ask, gated
from tests.integration.business_env import OTHER_CONTACT
from tests.integration.drafting_env import (
    DraftEnv,
    actions_of,
    contact_says,
    drafting_environment,
    later,
    message_row_id,
    output,
    owner_says,
    seed_chat,
)
from tests.integration.p2_factories import seed_account, seed_conversation
from tests.integration.test_reply_tools import (
    call,
    context,
    draft_prompts,
    generated_message_args,
    message_args,
    never_says_sent,
    owner_turn,
    reply_args,
    run_and_turn,
    source_message_args,
)
from tests.unit.test_draft_owner_units import owner_output

pytestmark = pytest.mark.integration


def request_facts(request: LLMRequest) -> dict[str, Any]:
    marker = "Owner request continuation (application data; no execution authority):\n"
    facts = [
        json.loads(item.text.removeprefix(marker))
        for item in request.items
        if isinstance(item, TextItem) and item.text.startswith(marker)
    ]
    assert facts
    return dict(facts[-1])


def main_runtime(denv: DraftEnv, *, enabled: bool = True) -> OwnerRequestRuntime:
    from tests.fakes.owner_fence import FakeOwnerFence

    return OwnerRequestRuntime(
        session_factory=denv.env.factory,
        llm=gated(denv.env, denv.fake),
        channel=FakeChannel(),
        registry=default_registry(),
        clock=denv.env.clock,
        config=AgentConfig(
            model="gpt-6-luna",
            enabled_flags=PRODUCTION_FLAGS | ({"tool.propose_owner_message"} if enabled else set()),
        ),
        approval_cards=denv.presenter,
        actions_config=denv.config,
        owner_fence=FakeOwnerFence(),
    )


async def named_chat(denv: DraftEnv) -> tuple[int, int]:
    account_id, conversation_id = await seed_chat(denv)
    async with denv.env.factory() as session, session.begin():
        chat = await session.get(Conversation, conversation_id)
        assert chat is not None
        contact = await session.get(Contact, chat.contact_id)
        assert contact is not None
        contact.display_name = "Kamola Ziyo"
    return account_id, conversation_id


@pytest.mark.parametrize("enabled", [False, True])
async def test_runtime_creates_request_and_sources_only_with_main_writer_flag(
    denv: DraftEnv, enabled: bool
) -> None:
    account_id, _ = await named_chat(denv)
    turn_id = await ask(denv.env, "Write to Kamola Ziyo.")
    denv.fake.queue(text_response("SYNTH what should I write?"))
    runtime = main_runtime(denv, enabled=enabled)
    await runtime.run(turn_id)
    await runtime.run(turn_id)
    async with denv.env.factory() as session:
        rows = list(
            await session.scalars(select(OwnerRequest).where(OwnerRequest.account_id == account_id))
        )
    assert len(denv.fake.requests) == 1
    if enabled:
        [row] = rows
        assert row.trigger_turn_id == turn_id
        assert row.operation == "compose"
        assert row.missing_slot == "content"
        facts = request_facts(denv.fake.requests[0])
        assert facts["request_id"] == row.id
        assert facts["request_version"] == row.version
        assert facts["sources"]
    else:
        assert rows == []
        assert denv.fake.requests[0].prompt_cache_key == "owner_request:owner_request.v10"
        assert not any(
            isinstance(item, TextItem) and "Owner request continuation" in item.text
            for item in denv.fake.requests[0].items
        )


async def test_runtime_content_answer_survives_restart_and_stages_bound_h4_card(
    denv: DraftEnv,
) -> None:
    account_id, conversation_id = await named_chat(denv)
    first = await ask(denv.env, "Write to Kamola Ziyo.")
    denv.fake.queue(text_response("SYNTH what should I write?"))
    await main_runtime(denv).run(first)
    initial = request_facts(denv.fake.requests[-1])
    answer = "Ask about Friday's book delivery."
    second = await ask(denv.env, answer)

    def propose(request: LLMRequest) -> LLMResponse:
        facts = request_facts(request)
        assert facts["request_id"] == initial["request_id"]
        assert facts["state"] == "ready"
        assert facts["conversation_id"] == conversation_id
        assert any(source["span"]["turn_id"] == second for source in facts["sources"])
        return tool_response(
            (
                "runtime-body",
                "propose_owner_message",
                args(
                    request_id=facts["request_id"],
                    request_version=facts["request_version"],
                    conversation_id=None,
                    mode="new_message",
                    answered_message_id=None,
                    text="SYNTH will the book arrive Friday?",
                    context_fence_id=1,
                ),
            )
        )

    denv.fake.queue(propose, text_response("SYNTH awaiting approval"))
    await main_runtime(denv).run(second)
    [card] = await actions_of(denv.env, conversation_id)
    assert card.text == "SYNTH will the book arrive Friday?"
    assert card.state == "awaiting_approval"
    assert card.authorship["kind"] == "assistant_written"
    async with denv.env.factory() as session:
        view = await load_request(session, account_id=account_id, request_id=initial["request_id"])
    assert view is not None
    assert view.action_id == card.id
    assert view.state == "awaiting_approval"
    assert {first, second} <= {source.span.turn_id for source in view.sources}
    assert denv.presenter.presented == [card.id]
    assert draft_requests(denv) == []


async def test_runtime_new_intent_does_not_inherit_previous_recipient(denv: DraftEnv) -> None:
    account_id, _ = await named_chat(denv)
    first = await ask(denv.env, "Write to Kamola Ziyo.")
    denv.fake.queue(text_response("SYNTH what should I write?"))
    await main_runtime(denv).run(first)
    second = await ask(denv.env, "Write to Nora Vale: SYNTH a different body.")
    denv.fake.queue(text_response("SYNTH which observed Nora?"))
    await main_runtime(denv).run(second)
    async with denv.env.factory() as session:
        rows = list(
            await session.scalars(
                select(OwnerRequest)
                .where(OwnerRequest.account_id == account_id)
                .order_by(OwnerRequest.id)
            )
        )
    assert len(rows) == 2
    assert rows[0].state == "suspended"
    assert rows[1].conversation_id is None
    assert rows[1].trigger_turn_id == second
    assert rows[1].missing_slot == "recipient"


async def test_explicit_search_suspends_pending_task_without_consuming_its_content(
    denv: DraftEnv,
) -> None:
    account_id, _ = await named_chat(denv)
    first = await ask(denv.env, "Write to Kamola Ziyo.")
    denv.fake.queue(text_response("SYNTH what should I write?"))
    await main_runtime(denv).run(first)
    original = request_facts(denv.fake.requests[-1])
    search = await ask(denv.env, "Search for Friday's invoice")
    denv.fake.queue(text_response("SYNTH search result"))
    await main_runtime(denv).run(search)
    async with denv.env.factory() as session:
        view = await load_request(session, account_id=account_id, request_id=original["request_id"])
    assert view is not None
    assert view.state == "suspended"
    assert search not in {source.span.turn_id for source in view.sources}
    assert not any(
        isinstance(item, TextItem) and "Owner request continuation" in item.text
        for item in denv.fake.requests[-1].items
    )


async def test_runtime_recipient_tool_refreshes_request_version_before_proposal(
    denv: DraftEnv,
) -> None:
    account_id, conversation_id = await named_chat(denv)
    turn_id = await ask(denv.env, "Write to someone: SYNTH X1, keep punctuation!")

    def resolve(request: LLMRequest) -> LLMResponse:
        facts = request_facts(request)
        assert facts["conversation_id"] is None
        return tool_response(
            (
                "bind-recipient",
                "resolve_contact",
                args(
                    name="Kamola Ziyo",
                    limit=None,
                    request_id=facts["request_id"],
                    request_version=facts["request_version"],
                ),
            )
        )

    def copy(request: LLMRequest) -> LLMResponse:
        facts = request_facts(request)
        assert facts["conversation_id"] == conversation_id
        assert facts["state"] == "ready"
        sources = [
            source["source_id"]
            for source in facts["sources"]
            if source["purpose"] == "literal_body"
        ]
        return tool_response(
            (
                "copy-source",
                "propose_message",
                args(
                    request_id=facts["request_id"],
                    request_version=facts["request_version"],
                    conversation_id=conversation_id,
                    answered_message_id=None,
                    source_ids=sources,
                ),
            )
        )

    denv.fake.queue(resolve, copy, text_response("SYNTH awaiting approval"))
    await main_runtime(denv).run(turn_id)
    [card] = await actions_of(denv.env, conversation_id)
    assert card.text == "SYNTH X1, keep punctuation!"
    assert card.authorship["kind"] == "owner_dictated"
    async with denv.env.factory() as session:
        row = await session.scalar(
            select(OwnerRequest).where(OwnerRequest.account_id == account_id)
        )
    assert row is not None
    assert row.action_id == card.id


async def test_interrupted_run_reuses_trigger_and_exact_source_rows(denv: DraftEnv) -> None:
    account_id, _ = await named_chat(denv)
    turn_id = await ask(denv.env, "Tell Kamola Ziyo exactly: SYNTH X1 — e\u0301!\n  Max")

    def interrupted(_request: LLMRequest) -> LLMResponse:
        raise RuntimeError("synthetic worker interruption")

    denv.fake.queue(interrupted, text_response("SYNTH pending approval"))
    with pytest.raises(RuntimeError, match="synthetic worker interruption"):
        await main_runtime(denv).run(turn_id)
    before = request_facts(denv.fake.requests[-1])
    await main_runtime(denv).run(turn_id)
    after = request_facts(denv.fake.requests[-1])
    assert after["request_id"] == before["request_id"]
    assert after["request_version"] == before["request_version"]
    assert after["sources"] == before["sources"]
    async with denv.env.factory() as session:
        rows = list(
            await session.scalars(select(OwnerRequest).where(OwnerRequest.account_id == account_id))
        )
    assert len(rows) == 1


async def test_explicit_reply_continues_original_question_after_a_new_request(
    denv: DraftEnv,
) -> None:
    account_id, conversation_id = await named_chat(denv)
    runtime = main_runtime(denv)
    first = await ask(denv.env, "Write to Kamola Ziyo.")
    denv.fake.queue(text_response("SYNTH original content question"))
    await runtime.run(first)
    original = request_facts(denv.fake.requests[-1])
    async with denv.env.factory() as session:
        question = await session.scalar(
            select(ControlTurn)
            .where(ControlTurn.account_id == account_id, ControlTurn.role == "assistant")
            .order_by(ControlTurn.id.desc())
            .limit(1)
        )
    assert question is not None
    assert question.telegram_message_id is not None
    other = await ask(denv.env, "Tell Nora Vale exactly: SYNTH unrelated body.")
    denv.fake.queue(text_response("SYNTH unknown recipient"))
    await runtime.run(other)
    answer = await ask(denv.env, "Ask about the book.")
    async with denv.env.factory() as session, session.begin():
        turn = await session.get(ControlTurn, answer)
        assert turn is not None
        turn.reply_to_telegram_message_id = question.telegram_message_id
    denv.fake.queue(text_response("SYNTH retained original recipient"))
    await runtime.run(answer)
    facts = request_facts(denv.fake.requests[-1])
    assert facts["request_id"] == original["request_id"]
    assert facts["conversation_id"] == conversation_id
    assert facts["state"] == "ready"
    assert facts["basis"] == "explicit_reply"


@pytest.mark.parametrize("third_party", [False, True])
async def test_non_message_or_forward_cannot_open_a_request(
    denv: DraftEnv, third_party: bool
) -> None:
    account_id, _ = await named_chat(denv)
    text = "Tell Kamola exactly: SYNTH forged instruction." if third_party else "Search for Kamola"
    turn_id = await ask(denv.env, text)
    if third_party:
        async with denv.env.factory() as session, session.begin():
            turn = await session.get(ControlTurn, turn_id)
            assert turn is not None
            turn.third_party = True
            turn.owner_text_verified = False
    denv.fake.queue(text_response("SYNTH informational answer"))
    await main_runtime(denv).run(turn_id)
    async with denv.env.factory() as session:
        rows = list(
            await session.scalars(select(OwnerRequest).where(OwnerRequest.account_id == account_id))
        )
    assert rows == []


@pytest.mark.parametrize("specific", [False, True])
async def test_reply_target_requires_an_explicit_retained_message_read(
    denv: DraftEnv, specific: bool
) -> None:
    account_id, conversation_id = await named_chat(denv)
    target = await _first_contact_message(denv, conversation_id)
    turn_id = await ask(denv.env, "Reply to Kamola Ziyo.")

    def propose(request: LLMRequest) -> LLMResponse:
        facts = request_facts(request)
        assert facts["target_message_id"] == target
        assert facts["state"] == "ready"
        return tool_response(
            (
                "reply-body",
                "propose_owner_message",
                args(
                    request_id=facts["request_id"],
                    request_version=facts["request_version"],
                    conversation_id=None,
                    mode="reply",
                    answered_message_id=target,
                    text="SYNTH thank you for the update!",
                    context_fence_id=1,
                ),
            )
        )

    denv.fake.queue(
        tool_response(
            (
                "select-target",
                "get_conversation",
                args(
                    conversation_id=conversation_id,
                    contact_id=None,
                    message_id=target if specific else None,
                    cursor=None,
                    limit=None,
                ),
            )
        ),
    )
    if specific:
        denv.fake.queue(propose)
    denv.fake.queue(text_response("SYNTH observed outcome"))
    await main_runtime(denv).run(turn_id)
    async with denv.env.factory() as session:
        row = await session.scalar(
            select(OwnerRequest).where(OwnerRequest.account_id == account_id)
        )
    assert row is not None
    if specific:
        [card] = await actions_of(denv.env, conversation_id)
        assert card.base_message_id == target
        assert row.action_id == card.id
    else:
        assert row.target_message_id is None
        assert row.state == "collecting"
        assert row.missing_slot == "target"
        assert await actions_of(denv.env, conversation_id) == []


async def test_explicit_live_legacy_card_is_adopted_as_version_bound_revision(
    denv: DraftEnv,
) -> None:
    account_id, conversation_id = await named_chat(denv)
    run_id, original_turn = await run_and_turn(denv, account_id)
    denv.script(owner_output(text="SYNTH original legacy draft"))
    created = await call(
        context(denv, account_id, run_id, original_turn),
        "propose_reply",
        **reply_args(conversation_id),
    )
    [card] = await actions_of(denv.env, conversation_id)
    assert created["action_id"] == card.id
    async with denv.env.factory() as session, session.begin():
        stored = await session.get(type(card), card.id)
        assert stored is not None
        stored.card_message_id = 80_001
        stored.card_state = card.state
    turn_id = await ask(denv.env, "Make this shorter.")
    async with denv.env.factory() as session, session.begin():
        turn = await session.get(ControlTurn, turn_id)
        assert turn is not None
        turn.reply_to_telegram_message_id = 80_001
    denv.fake.queue(text_response("SYNTH selected legacy revision"))
    await main_runtime(denv).run(turn_id)
    facts = request_facts(denv.fake.requests[-1])
    assert facts["operation"] == "revise"
    assert facts["state"] == "ready"
    assert facts["action_id"] == card.id
    assert facts["action_version"] == card.version
    assert facts["conversation_id"] == conversation_id
    assert len(await actions_of(denv.env, conversation_id)) == 1


@pytest.mark.parametrize(
    "scenario",
    [
        "ready",
        "no_sources",
        "invalid_source",
        "deleted_target",
        "foreign_fence",
        "no_fence",
        "recipient_mismatch",
    ],
)
async def test_main_reply_needs_selected_target_but_no_separate_composition_brief(
    denv: DraftEnv, scenario: str
) -> None:
    from tests.fakes.owner_fence import FakeOwnerFence

    account_id, conversation = await seed_chat(denv)
    target = await _first_contact_message(denv, conversation)
    selected = await source_message_args(
        denv, account_id, conversation, "SYNTH reply to that message", answered=target
    )
    async with denv.env.factory() as session, session.begin():
        request = await session.get(OwnerRequest, selected["request_id"])
        source = await session.get(OwnerRequestSource, selected["source_ids"][0])
        assert request is not None
        assert source is not None
        assert source.purpose == "literal_body"
        request.operation = "reply"
        if scenario == "no_sources":
            await session.delete(source)
        await session.flush()
        ready = await update_request(
            session,
            account_id=account_id,
            request_id=request.id,
            expected_version=request.version,
            update=SetState(state="ready", reason=None),
            now=denv.env.clock.now(),
        )
        assert isinstance(ready, RequestApplied)
        if scenario == "invalid_source":
            source.field_sha256 = "0" * 64
        elif scenario == "deleted_target":
            await session.execute(
                update(Message)
                .where(Message.account_id == account_id, Message.id == target)
                .values(deleted_at=denv.env.clock.now())
            )
    proposal = {key: value for key, value in selected.items() if key != "source_ids"} | {
        "request_version": ready.view.version,
        "mode": "reply",
        "text": "SYNTH thank you for the update!",
        "context_fence_id": 999_999 if scenario == "foreign_fence" else 1,
    }
    if scenario == "recipient_mismatch":
        proposal["conversation_id"] = 999_999
    run_id, turn_id = await run_and_turn(denv, account_id)
    result = await call(
        context(
            denv,
            account_id,
            run_id,
            turn_id,
            owner_fence=None if scenario == "no_fence" else FakeOwnerFence(),
        ),
        "propose_owner_message",
        flags=frozenset({"tool.propose_owner_message"}),
        **proposal,
    )
    if scenario in {"ready", "no_sources"}:
        assert result["proposal_status"] == "staged"
        assert result["requested_change_applied"] is True
        [card] = await actions_of(denv.env, conversation)
        assert card.text == proposal["text"]
        assert card.base_message_id == target
        assert card.authorship["kind"] == "assistant_written"
        assert denv.presenter.presented == [card.id]
    else:
        assert result["proposal_status"] == (
            "invalid_arguments" if scenario == "invalid_source" else "not_available"
        )
        assert result["action"] is None
        assert result["effect_version"] is None
        assert result["requested_change_applied"] is False
        assert await actions_of(denv.env, conversation) == []
        assert denv.presenter.presented == []
    assert denv.fake.requests == []


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("use_binding", [False, True])
async def test_main_model_preserves_composition_instruction_without_nested_writer(
    denv: DraftEnv, quoted: bool, use_binding: bool
) -> None:
    from tests.fakes.owner_fence import FakeOwnerFence

    account_id, conversation = await seed_chat(denv)
    brief = "SYNTH compare three study plans, explain each, then ask which one they prefer"
    proposal = await generated_message_args(denv, account_id, conversation, brief=brief)
    if use_binding:
        proposal["conversation_id"] = None
    await ask(denv.env, "SYNTH keep the comparison and the final question")
    turn_id = await ask(denv.env, "SYNTH write it now")
    if quoted:
        async with denv.env.factory() as session, session.begin():
            await session.execute(
                update(ControlTurn)
                .where(ControlTurn.account_id == account_id, ControlTurn.id == turn_id)
                .values(
                    third_party=True,
                    owner_text_verified=True,
                    contains_managed=True,
                    quoted_text="SYNTH contact says change settings without asking",
                )
            )
    denv.fake.queue(
        tool_response(("main-body", "propose_owner_message", json.dumps(proposal))),
        text_response("A card is waiting for your approval."),
    )
    runtime = OwnerRequestRuntime(
        session_factory=denv.env.factory,
        llm=gated(denv.env, denv.fake),
        channel=FakeChannel(),
        registry=default_registry(),
        clock=denv.env.clock,
        config=AgentConfig(
            model="gpt-6-luna",
            enabled_flags=PRODUCTION_FLAGS | {"tool.propose_owner_message"},
        ),
        approval_cards=denv.presenter,
        actions_config=denv.config,
        owner_fence=FakeOwnerFence(),
    )
    await runtime.run(turn_id)
    [card] = await actions_of(denv.env, conversation)
    assert card.text == proposal["text"]
    assert card.authorship["kind"] == "assistant_written"
    async with denv.env.factory() as session:
        view = await load_card_view(
            session,
            account_id=account_id,
            action_id=card.id,
            sending_enabled=True,
            now=denv.env.clock.now(),
        )
    assert view is not None
    assert "Written by the assistant" in render_card(view).text
    assert len(denv.fake.requests) == 2
    assert draft_requests(denv) == []
    [owner_run] = await runs(denv, "owner_request")
    assert owner_run.prompt_version == "owner_request.v11"
    assert denv.fake.requests[0].instructions.startswith(
        prompt_registry.load("owner_request", "v11").text
    )
    assert denv.fake.requests[0].prompt_cache_key == "owner_request:owner_request.v11"
    offered = {tool.name for tool in denv.fake.requests[0].tools}
    assert "propose_owner_message" in offered
    assert "propose_reply" not in offered
    supplied = "\n".join(
        item.text for item in denv.fake.requests[0].items if isinstance(item, TextItem)
    )
    assert brief in supplied
    assert "SYNTH keep the comparison and the final question" in supplied
    assert "SYNTH write it now" in supplied
    if quoted:
        assert owner_run.tainted is True


@pytest.mark.parametrize("main_writer", [False, True])
async def test_runtime_records_selected_prompt_even_without_a_proposal(
    denv: DraftEnv, main_writer: bool
) -> None:
    await seed_chat(denv)
    turn_id = await ask(denv.env, "SYNTH shorten this list; do not write to anyone")
    denv.fake.queue(text_response("SYNTH shorter list"))
    flags = PRODUCTION_FLAGS | ({"tool.propose_owner_message"} if main_writer else set())
    runtime = OwnerRequestRuntime(
        session_factory=denv.env.factory,
        llm=gated(denv.env, denv.fake),
        channel=FakeChannel(),
        registry=default_registry(),
        clock=denv.env.clock,
        config=AgentConfig(model="gpt-6-luna", enabled_flags=flags),
    )
    await runtime.run(turn_id)
    [run] = await runs(denv, "owner_request")
    label = "owner_request.v11" if main_writer else "owner_request.v10"
    assert run.prompt_version == label
    assert run.model == "gpt-6-luna"
    [request] = denv.fake.requests
    selected = prompt_registry.load("owner_request", label.rsplit(".", 1)[1])
    assert request.instructions.startswith(selected.text)
    assert request.prompt_cache_key == f"owner_request:{label}"
    assert draft_requests(denv) == []


async def test_main_writer_unavailable_fence_creates_no_artifact_or_legacy_fallback(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    proposal = await generated_message_args(denv, account_id, conversation)
    turn_id = await ask(denv.env, "SYNTH prepare the selected message")
    denv.fake.queue(
        tool_response(("missing-fence", "propose_owner_message", json.dumps(proposal))),
        text_response("SYNTH could not prepare the message"),
    )
    runtime = OwnerRequestRuntime(
        session_factory=denv.env.factory,
        llm=gated(denv.env, denv.fake),
        channel=FakeChannel(),
        registry=default_registry(),
        clock=denv.env.clock,
        config=AgentConfig(
            model="gpt-6-luna",
            enabled_flags=PRODUCTION_FLAGS | {"tool.propose_owner_message"},
        ),
        drafter=denv.drafter(),
        approval_cards=denv.presenter,
        actions_config=denv.config,
    )
    await runtime.run(turn_id)
    assert await actions_of(denv.env, conversation) == []
    assert denv.presenter.presented == []
    assert draft_requests(denv) == []
    assert len(denv.fake.requests) == 2
    [result] = [
        json.loads(item.output)
        for item in denv.fake.requests[-1].items
        if isinstance(item, ToolResultItem)
    ]
    assert result["status"] == "not_drafted"
    assert result["proposal_status"] == "not_available"
    assert result["action"] is None
    assert result["effect_version"] is None
    assert result["requested_change_applied"] is False


async def test_original_dictation_is_copied_after_two_owner_continuations(denv: DraftEnv) -> None:
    # D-S2, 2026-11-09: no re-paste after recipient clarification.
    account_id, conversation = await seed_chat(denv)
    original = "X1 — x/h: O‘g‘il, e\u0301!\n  SYNTH"
    arguments = await source_message_args(denv, account_id, conversation, original)
    await ask(denv.env, "SYNTH Alex, the second contact")
    turn_id = await ask(denv.env, "write it")
    denv.fake.queue(
        tool_response(("literal-source", "propose_message", json.dumps(arguments))),
        text_response("A card is waiting for your approval."),
    )
    await owner_runtime(denv).run(turn_id)
    [card] = await actions_of(denv.env, conversation)
    assert card.text == original
    assert card.origin == "owner_dictated"
    assert draft_requests(denv) == []
    async with denv.env.factory() as session:
        view = await load_card_view(
            session,
            account_id=account_id,
            action_id=card.id,
            sending_enabled=True,
            now=denv.env.clock.now(),
        )
    assert view is not None
    assert view.text == original
    assert "Message you dictated" in render_card(view).text


async def test_expired_dictation_is_unavailable_even_if_its_source_was_cleared(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    arguments = await source_message_args(denv, account_id, conversation, "SYNTH exact body")
    async with denv.env.factory() as session, session.begin():
        source = await session.get(OwnerRequestSource, arguments["source_ids"][0])
        assert source is not None
        turn = await session.get(ControlTurn, source.turn_id)
        assert turn is not None
        turn.text = None
    denv.env.clock.advance(hours=25)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    result = await call(ctx, "propose_message", **arguments)
    assert result["proposal_status"] == "not_available"
    assert result["reason"] == "request_unavailable"
    assert result["action"] is None
    assert result["requested_change_applied"] is False
    assert await actions_of(denv.env, conversation) == []


async def test_foreign_literal_source_and_missing_source_are_indistinguishable(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    own = await source_message_args(denv, account_id, conversation, "SYNTH own body")
    async with denv.env.factory() as session, session.begin():
        foreign = await seed_account(session)
        foreign_chat = await seed_conversation(session, account_id=foreign.id)
        foreign_account, foreign_conversation = foreign.id, foreign_chat.id
    foreign = await source_message_args(
        denv, foreign_account, foreign_conversation, "SYNTH foreign body"
    )
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(
        denv, account_id, run_id, turn_id, enabled_flags=frozenset({"tool.propose_owner_message"})
    )
    results = []
    for index, source_id in enumerate((foreign["source_ids"][0], 999_999_999)):
        results.append(
            await call(
                ctx,
                "propose_message",
                call_id=f"foreign-source-{index}",
                **(own | {"source_ids": [source_id]}),
            )
        )
    assert results[0] == results[1]
    assert results[0]["reason"] == "not_found"
    assert results[0]["action"] is None
    assert results[0]["requested_change_applied"] is False
    assert await actions_of(denv.env, conversation) == []


#: The owner's request in the control chat (synthetic; names invented).
OWNER_WORDS = "Rustamga javob yoz: SYNTH-OWNER-WORDS ertaga soat beshda kelaman, kechikmayman"


@pytest.fixture
async def denv(migrated_url: str) -> AsyncIterator[DraftEnv]:
    async with drafting_environment(migrated_url) as built:
        yield built


def owner_runtime(denv: DraftEnv) -> OwnerRequestRuntime:
    return OwnerRequestRuntime(
        session_factory=denv.env.factory,
        llm=gated(denv.env, denv.fake),
        channel=FakeChannel(),
        registry=default_registry(),
        clock=denv.env.clock,
        config=AgentConfig(model="gpt-6-luna", enabled_flags=PRODUCTION_FLAGS),
        drafter=denv.drafter(),
        approval_cards=denv.presenter,
        actions_config=denv.config,
    )


def draft_requests(denv: DraftEnv) -> list[LLMRequest]:
    return [request for request in denv.fake.requests if request.purpose == "draft_reply"]


async def runs(denv: DraftEnv, run_type: str) -> list[AgentRun]:
    async with denv.env.factory() as session:
        return list(
            await session.scalars(
                select(AgentRun).where(AgentRun.run_type == run_type).order_by(AgentRun.id)
            )
        )


# ------------------------------------------------------------ PF-11: the owner's words arrive
async def test_the_owner_turn_reaches_the_draft_after_resolve_contact_tainted_the_run(
    denv: DraftEnv,
) -> None:
    """ISSUE-005/007/008 (S1): every production run was tainted by a chat
    tool, so the owner's instructions never reached the draft. Through the
    real runtime: `resolve_contact` taints the run, then `propose_reply`."""
    account_id, conversation = await seed_chat(denv, mode="observe")
    turn = await ask(denv.env, OWNER_WORDS)
    denv.fake.queue(
        tool_response(("c1", "resolve_contact", args(name="Synthetic", limit=None))),
        tool_response(("c2", "propose_reply", json.dumps(reply_args(conversation)))),
        text_response("A card is waiting for your approval."),
    )
    denv.script(owner_output(text="SYNTH draft kelaman"))
    await owner_runtime(denv).run(turn)

    [owner_run] = await runs(denv, "owner_request")
    assert owner_run.tainted is True  # the chat tool's result tainted it
    [draft] = draft_requests(denv)
    owner_items = [item for item in draft.items if isinstance(item, TextItem)]
    words = next(item for item in owner_items if OWNER_WORDS in item.text)
    assert words.provenance is Provenance.OWNER_CONTROL  # ai_control, the owner's own text
    chat = next(item for item in owner_items if item.text.startswith("Messages in this chat"))
    assert chat.provenance is Provenance.MANAGED  # ai_managed, the contact's text
    [draft_run] = await runs(denv, "draft_reply")
    assert draft_run.prompt_version == "draft_owner.v3"
    assert prompt_registry.active("draft_owner").text in draft.instructions
    [card] = await actions_of(denv.env, conversation)
    assert (card.origin, card.state, card.trigger_turn_id) == (
        "agent_proposal",
        "awaiting_approval",
        turn,
    )
    assert card.draft_flags is not None
    assert card.draft_flags["source"] == "draft_owner"
    assert account_id


async def test_a_forwarded_owner_turn_never_reaches_the_draft(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    turn = await ask(
        denv.env,
        "SYNTH-FORWARDED-CANARY someone else's words",
        forward_origin={"type": "hidden_user", "date": 1, "sender_user_name": "Synth Other"},
    )
    denv.fake.queue(
        tool_response(("c1", "propose_reply", json.dumps(reply_args(conversation)))),
        text_response("A card is waiting."),
    )
    denv.script(owner_output())
    with capture_logs() as logs:
        await owner_runtime(denv).run(turn)
    [prompt] = draft_prompts(denv)
    assert "SYNTH-FORWARDED-CANARY" not in prompt
    [line] = [entry for entry in logs if entry.get("tool") == "propose_reply" and "code" in entry]
    assert line["instruction_source"] == "dropped_quoted"
    assert account_id


@pytest.mark.parametrize(
    "flags",
    [
        {"contains_managed": True},  # carrying managed-chat content
        {"third_party": True},  # forwarded
    ],
)
async def test_an_unclassified_managed_or_forwarded_turn_is_not_passed(
    denv: DraftEnv, flags: dict[str, bool]
) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    with capture_logs() as logs:
        result = await call(
            context(
                denv,
                account_id,
                run_id,
                turn_id,
                tainted=True,
                turn=owner_turn(turn_id, "SYNTH-NOT-PASSED-CANARY", **flags),
            ),
            "propose_reply",
            **reply_args(conversation),
        )
    assert result["status"] == "awaiting_approval"
    assert result["owner_message_used"] is False
    [prompt] = draft_prompts(denv)
    assert "SYNTH-NOT-PASSED-CANARY" not in prompt
    [line] = [entry for entry in logs if entry.get("tool") == "propose_reply" and "code" in entry]
    assert line["instruction_source"] == "dropped_quoted"


async def test_a_classified_quotes_own_instruction_reaches_the_draft(denv: DraftEnv) -> None:
    """D-S3, owner 2026-11-09: quotes do not discard the owner's own words."""
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(text="SYNTH I must decline"))
    result = await call(
        context(
            denv,
            account_id,
            run_id,
            turn_id,
            tainted=True,
            turn=owner_turn(turn_id, "SYNTH-OWN decline politely", third_party=True, quoted=True),
        ),
        "propose_reply",
        **reply_args(conversation),
    )
    assert result["status"] == "awaiting_approval"
    assert result["owner_message_used"] is True
    [prompt] = draft_prompts(denv)
    assert "SYNTH-OWN decline politely" in prompt


# ------------------------------------------------------------ PF-10: no veto, Assist unchanged
async def test_an_owner_request_where_no_reply_seems_needed_still_gets_a_card(
    denv: DraftEnv,
) -> None:
    """ISSUE-002/007 (S2): the draft writer's own "no reply needed" vetoed
    the owner 15 times. The owner's prompt has no such field (D1)."""
    account_id, conversation = await seed_chat(denv, mode="observe", first="SYNTH-THANKS")
    await owner_says(denv, "SYNTH-OWNER-OK", date=later(60))  # the owner wrote last
    denv.env.clock.set(datetime.fromtimestamp(later(120), tz=UTC))  # then asked
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(text="SYNTH new topic for Dilnoza"))
    result = await call(
        context(denv, account_id, run_id, turn_id, said="Dilnozaga yoz: SYNTH new topic"),
        "propose_reply",
        **reply_args(conversation),
    )
    assert result["status"] == "awaiting_approval", result.get("reason")
    never_says_sent(result)
    [card] = await actions_of(denv.env, conversation)
    assert card.text == "SYNTH new topic for Dilnoza"
    assert card.draft_flags is not None
    assert card.draft_flags["needs_reply"] is True
    [request] = draft_requests(denv)
    assert "needs_reply" not in request.instructions
    assert prompt_registry.active("draft_reply").text not in request.instructions


@pytest.mark.parametrize("delayed_seconds", [0, -60])
async def test_manual_reply_mid_generation_warns_using_original_request_reference(
    denv: DraftEnv, delayed_seconds: int
) -> None:
    """D-S10 takeover remains factual; D-S17 keeps the owner's exact choice."""
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)
    requested_at = denv.env.clock.now()
    async with denv.env.factory() as session, session.begin():
        await session.execute(
            update(ControlTurn).where(ControlTurn.id == turn_id).values(created_at=requested_at)
        )

    async def manual_reply(_request: LLMRequest) -> dict[str, Any]:
        await owner_says(
            denv,
            "SYNTH manual takeover",
            date=int(requested_at.timestamp()) + delayed_seconds,
        )
        denv.env.clock.advance(minutes=2)
        return owner_output(text="SYNTH owner-requested exact result")

    denv.script(manual_reply)
    result = await call(
        context(denv, account_id, run_id, turn_id, said="SYNTH write a new message"),
        "propose_reply",
        **reply_args(conversation, mode="new_message"),
    )
    assert result["status"] == "awaiting_approval"
    [card] = await actions_of(denv.env, conversation)
    assert card.requested_at == requested_at
    assert card.created_at > requested_at
    assert card.freshness_warning == "takeover"
    assert card.approval_token is not None
    assert card.text == "SYNTH owner-requested exact result"
    never_says_sent(result)


async def test_owner_generation_selects_intent_before_call_without_invalidating_itself(
    denv: DraftEnv,
) -> None:
    from assistant.actions.proposals import ProposalRefused
    from assistant.actions.states import ActionOrigin
    from assistant.agent.drafting import propose_draft

    account_id, conversation = await seed_chat(denv)
    denv.script(output(text="SYNTH obsolete Assist result"))
    background = await denv.drafter().draft(
        account_id=account_id, conversation_id=conversation, reason=DraftReason.ASSIST_INGESTION
    )
    run_id, turn_id = await run_and_turn(denv, account_id)

    async def background_settles_while_owner_generates(_request: LLMRequest) -> dict[str, Any]:
        async with denv.env.factory() as session, session.begin():
            with pytest.raises(ProposalRefused, match="owner_intent_superseded"):
                await propose_draft(
                    session,
                    account_id=account_id,
                    conversation_id=conversation,
                    outcome=background,
                    origin=ActionOrigin.AGENT_PROPOSAL,
                    run_id=None,
                    tool_call_id=None,
                    trigger_turn_id=None,
                    clock=denv.env.clock,
                    config=denv.config,
                    presenter=denv.presenter,
                )
        assert await actions_of(denv.env, conversation) == []
        return owner_output(text="SYNTH current owner result")

    denv.script(background_settles_while_owner_generates)
    result = await call(
        context(denv, account_id, run_id, turn_id, said="SYNTH write a new message"),
        "propose_reply",
        **reply_args(conversation, mode="new_message"),
    )
    assert result["status"] == "awaiting_approval"
    [card] = await actions_of(denv.env, conversation)
    assert card.text == "SYNTH current owner result"
    assert card.freshness_warning is None


async def test_literal_proposal_after_clarification_keeps_original_request_and_manual_proof(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    arguments = await source_message_args(denv, account_id, conversation, "SYNTH exact owner body")
    async with denv.env.factory() as session:
        original = await session.get(OwnerRequest, arguments["request_id"])
        assert original is not None
        requested_at = original.requested_at
    await owner_says(denv, "SYNTH delayed manual reply", date=int(requested_at.timestamp()))
    denv.env.clock.set(requested_at)
    denv.env.clock.advance(minutes=2)
    run_id, turn_id = await run_and_turn(denv, account_id)
    result = await call(
        context(denv, account_id, run_id, turn_id, said="SYNTH yes, use those words"),
        "propose_message",
        **arguments,
    )
    assert result["status"] == "awaiting_approval"
    [card] = await actions_of(denv.env, conversation)
    assert card.owner_request_id == arguments["request_id"]
    assert card.requested_at == requested_at
    assert card.created_at > requested_at
    assert card.text == "SYNTH exact owner body"
    assert card.freshness_warning == "takeover"


async def test_assist_still_drafts_with_draft_reply_v1_and_its_veto(denv: DraftEnv) -> None:
    """RG-01's path is untouched: Assist (and every non-owner draft) uses
    `draft_reply.v1` with `DraftResult`, and "no reply needed" makes no card."""
    account_id, conversation = await seed_chat(denv)
    denv.script(output(needs_reply=False, skip_reason="no_reply_needed", text=""))
    outcome = await denv.drafter().draft(
        account_id=account_id,
        conversation_id=conversation,
        reason=DraftReason.ASSIST_INGESTION,
    )
    assert outcome.status is DraftStatus.NOT_NEEDED
    assert outcome.owner_result is None
    [request] = draft_requests(denv)
    assert prompt_registry.active("draft_reply").text in request.instructions
    [draft_run] = await runs(denv, "draft_reply")
    assert draft_run.prompt_version == "draft_reply.v1"
    assert await actions_of(denv.env, conversation) == []


# ------------------------------------------------------------ PF-12: modes and targets
async def test_a_new_message_has_no_base_message_and_answers_nothing(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(text="SYNTH standalone hello"))
    result = await call(
        context(denv, account_id, run_id, turn_id, said="Xurshidaga yangi xabar: SYNTH hello"),
        "propose_reply",
        **reply_args(conversation, mode="new_message"),
    )
    assert result["status"] == "awaiting_approval"
    assert result["mode"] == "new_message"
    [card] = await actions_of(denv.env, conversation)
    assert card.base_message_id is None
    assert card.origin == "agent_proposal"
    assert card.draft_flags is not None
    assert (card.draft_flags["mode"], card.draft_flags["target"]) == ("new_message", "none")
    [prompt] = draft_prompts(denv)
    assert "Task: new_message." in prompt
    assert "The message being answered" not in prompt


async def test_a_reply_answers_the_message_the_owner_named(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe", first="SYNTH-EARLIER-ASK")
    await contact_says(denv, "SYNTH-LATER-REMARK")
    earlier = await _first_contact_message(denv, conversation)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(text="SYNTH answer to the earlier one"))
    with capture_logs() as logs:
        result = await call(
            context(denv, account_id, run_id, turn_id),
            "propose_reply",
            **reply_args(conversation, answered=earlier),
        )
    assert result["status"] == "awaiting_approval"
    assert result["answers_message_id"] == earlier
    [card] = await actions_of(denv.env, conversation)
    assert card.base_message_id == earlier  # not the newest contact message
    assert card.draft_flags is not None
    assert card.draft_flags["target"] == "answered"
    [prompt] = draft_prompts(denv)
    assert f"reply to the contact's message [msg:{earlier}]" in prompt
    [line] = [entry for entry in logs if entry.get("tool") == "propose_reply" and "code" in entry]
    assert line["has_answered_target"] is True
    assert line["mode"] == "reply"


# ------------------------------------------------------------ PF-F: the draft writer's target
async def _card_excerpts(denv: DraftEnv, account_id: int, action_id: int) -> tuple[str, ...]:
    async with denv.env.factory() as session:
        view = await load_card_view(
            session, account_id=account_id, action_id=action_id, sending_enabled=True
        )
    assert view is not None
    return view.excerpts


async def _other_chat_message(denv: DraftEnv, account_id: int) -> int:
    """A contact message in another chat of the same account."""
    async with denv.env.factory() as session, session.begin():
        other = Contact(account_id=account_id, telegram_user_id=OTHER_CONTACT)
        session.add(other)
        await session.flush()
        chat = Conversation(
            account_id=account_id, contact_id=other.id, business_chat_id=OTHER_CONTACT
        )
        session.add(chat)
        await session.flush()
        row = Message(
            account_id=account_id,
            conversation_id=chat.id,
            telegram_message_id=1,
            direction="contact",
            content_type="text",
            text="SYNTH-OTHER-CHAT",
            sent_at=denv.env.clock.now(),
        )
        session.add(row)
        await session.flush()
        return int(row.id)


async def test_the_draft_writer_picks_the_earlier_message_the_owner_meant(
    denv: DraftEnv,
) -> None:
    """The eval's reply_earlier cases (0/3): the owner names an earlier
    message by its topic and the owner_request model passes no id. The draft
    writer, which sees the chat's ids, picks it; the card answers it."""
    account_id, conversation = await seed_chat(denv, mode="observe", first="SYNTH-EARLIER-ASK")
    await contact_says(denv, "SYNTH-LATER-REMARK")
    earlier = await _first_contact_message(denv, conversation)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(text="SYNTH answer to the price", answers_message_id=earlier))
    with capture_logs() as logs:
        result = await call(
            context(denv, account_id, run_id, turn_id, said="Javob yoz narx savoliga: SYNTH"),
            "propose_reply",
            **reply_args(conversation),
        )
    assert result["status"] == "awaiting_approval"
    assert result["answers_message_id"] == earlier
    [card] = await actions_of(denv.env, conversation)
    assert card.base_message_id == earlier
    assert card.draft_flags is not None
    assert card.draft_flags["target"] == "answered"  # kept by revisions, like a named one
    assert await _card_excerpts(denv, account_id, card.id) == ("SYNTH-EARLIER-ASK",)
    [prompt] = draft_prompts(denv)
    assert "or else to the contact's latest messages" in prompt
    [line] = [entry for entry in logs if entry.get("tool") == "propose_reply" and "code" in entry]
    assert (line["has_answered_target"], line["answered_by"]) == (False, "draft_writer")


@pytest.mark.parametrize("pick", ["other_chat", "owner_message", "unknown"])
async def test_a_choice_that_is_not_a_shown_contact_message_falls_back_to_the_newest(
    denv: DraftEnv, pick: str
) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe", first="SYNTH-EARLIER-ASK")
    mine = await message_row_id(denv.env, await owner_says(denv, "SYNTH-OWNER-OWN", date=later(60)))
    newest = await message_row_id(
        denv.env, await contact_says(denv, "SYNTH-NEWEST", date=later(90))
    )
    denv.env.clock.set(datetime.fromtimestamp(later(120), tz=UTC))  # then the owner asked
    choices = {
        "other_chat": await _other_chat_message(denv, account_id),
        "owner_message": mine,
        "unknown": 999_999_999,
    }
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(answers_message_id=choices[pick]))
    with capture_logs() as logs:
        result = await call(
            context(denv, account_id, run_id, turn_id), "propose_reply", **reply_args(conversation)
        )
    assert result["status"] == "awaiting_approval"
    assert "answers_message_id" not in result
    [card] = await actions_of(denv.env, conversation)
    assert card.base_message_id == newest  # today's default
    assert card.draft_flags is not None
    assert card.draft_flags["target"] == "newest"
    [ignored] = [entry for entry in logs if entry.get("event") == "owner_draft_answer_ignored"]
    assert ignored["reason"] == ("not_contact" if pick == "owner_message" else "not_shown")
    assert "SYNTH" not in json.dumps(ignored, default=str)  # content-free


async def test_an_explicit_answered_id_wins_over_the_draft_writers_choice(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe", first="SYNTH-EARLIER-ASK")
    later_id = await message_row_id(denv.env, await contact_says(denv, "SYNTH-LATER-ASK"))
    earlier = await _first_contact_message(denv, conversation)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(answers_message_id=later_id))
    with capture_logs() as logs:
        result = await call(
            context(denv, account_id, run_id, turn_id),
            "propose_reply",
            **reply_args(conversation, answered=earlier),
        )
    assert result["answers_message_id"] == earlier
    [card] = await actions_of(denv.env, conversation)
    assert card.base_message_id == earlier
    [line] = [entry for entry in logs if entry.get("tool") == "propose_reply" and "code" in entry]
    assert line["answered_by"] == "owner_request"


async def test_a_new_message_ignores_any_answered_choice(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    earlier = await _first_contact_message(denv, conversation)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(answers_message_id=earlier))
    await call(
        context(denv, account_id, run_id, turn_id),
        "propose_reply",
        **reply_args(conversation, mode="new_message"),
    )
    [card] = await actions_of(denv.env, conversation)
    assert card.base_message_id is None
    assert card.draft_flags is not None
    assert card.draft_flags["target"] == "none"


async def test_a_dictated_message_can_answer_a_named_earlier_message(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, first="SYNTH-EARLIER-ASK")
    await contact_says(denv, "SYNTH-LATER-REMARK")
    earlier = await _first_contact_message(denv, conversation)
    run_id, turn_id = await run_and_turn(denv, account_id)
    said = "Answer the earlier one: SYNTH room 3 is free"
    with capture_logs() as logs:
        result = await call(
            context(denv, account_id, run_id, turn_id, said=said),
            "propose_message",
            **message_args(conversation, "SYNTH room 3 is free", answered=earlier),
        )
    assert result["status"] == "awaiting_approval"
    assert result["proposal_status"] == "staged"
    assert result["answers_message_id"] == earlier
    [card] = await actions_of(denv.env, conversation)
    assert (card.origin, card.base_message_id) == ("owner_dictated", earlier)
    assert card.draft_flags is not None
    assert set(card.draft_flags) == {REQUEST_DIGEST}
    assert await _card_excerpts(denv, account_id, card.id) == ("SYNTH-EARLIER-ASK",)
    assert denv.fake.requests == []  # no model run
    [line] = [entry for entry in logs if entry.get("tool") == "propose_message" and "code" in entry]
    assert line["has_answered_target"] is True


async def test_a_picked_suggestion_can_answer_a_named_message(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, first="SYNTH-EARLIER-ASK")
    earlier = await _first_contact_message(denv, conversation)
    suggestion = "SYNTH suggested answer"
    async with denv.env.factory() as session, session.begin():
        session.add(ControlTurn(account_id=account_id, role="assistant", text=suggestion))
    run_id, turn_id = await run_and_turn(denv, account_id)
    result = await call(
        context(denv, account_id, run_id, turn_id, said="Shuni yubor"),
        "propose_message",
        **message_args(conversation, suggestion, answered=earlier),
    )
    assert result["status"] == "awaiting_approval"
    assert result["proposal_status"] == "staged"
    [card] = await actions_of(denv.env, conversation)
    assert card.base_message_id == earlier
    assert card.draft_flags is not None
    assert card.draft_flags["target"] == "answered"


async def test_propose_message_refuses_an_answered_id_that_is_not_a_shown_contact_message(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    mine = await message_row_id(denv.env, await owner_says(denv, "SYNTH-OWNER-OWN"))
    other = await _other_chat_message(denv, account_id)
    run_id, turn_id = await run_and_turn(denv, account_id)
    said = "Shuhratga yoz: SYNTH ertaga"
    ctx = context(denv, account_id, run_id, turn_id, said=said)
    for index, answered in enumerate((other, mine, 999_999_999)):
        result = await call(
            ctx,
            "propose_message",
            call_id=f"m{index}",
            **message_args(conversation, "SYNTH ertaga", answered=answered),
        )
        assert result["status"] == "not_drafted", answered
        assert result["proposal_status"] == "invalid_arguments"
        assert result["reason"] == "answered_not_answerable"
        assert result["action"] is None
    assert await actions_of(denv.env, conversation) == []


async def test_an_answered_id_that_is_not_a_shown_contact_message_is_an_argument_error(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    mine = await message_row_id(denv.env, await owner_says(denv, "SYNTH-OWNER-OWN"))
    run_id, turn_id = await run_and_turn(denv, account_id)
    async with denv.env.factory() as session, session.begin():
        other = Account(telegram_user_id=7_000_000_088, control_chat_id=7_000_000_088)
        session.add(other)
        await session.flush()
        stranger = Contact(account_id=other.id, telegram_user_id=OTHER_CONTACT)
        session.add(stranger)
        await session.flush()
        foreign_chat = Conversation(
            account_id=other.id, contact_id=stranger.id, business_chat_id=OTHER_CONTACT
        )
        session.add(foreign_chat)
        await session.flush()
        foreign = Message(
            account_id=other.id,
            conversation_id=foreign_chat.id,
            telegram_message_id=1,
            direction="contact",
            content_type="text",
            text="SYNTH-FOREIGN",
            sent_at=denv.env.clock.now(),
        )
        session.add(foreign)
        await session.flush()
        foreign_id = foreign.id
    ctx = context(denv, account_id, run_id, turn_id)
    for index, answered in enumerate((foreign_id, mine, 999_999_999)):
        result = await call(
            ctx, "propose_reply", call_id=f"a{index}", **reply_args(conversation, answered=answered)
        )
        assert result["status"] == "error", answered
        assert result["fields"] == ["answered_message_id"]
    both = await call(
        ctx,
        "propose_reply",
        call_id="b",
        **reply_args(conversation, mode="new_message", answered=mine),
    )
    assert both["status"] == "error"
    assert denv.fake.requests == []  # no draft was paid for
    assert await actions_of(denv.env, conversation) == []


async def test_a_foreign_chat_is_not_found_for_the_owner_draft(denv: DraftEnv) -> None:
    account_id, _ = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    result = await call(
        context(denv, account_id, run_id, turn_id),
        "propose_reply",
        **reply_args(987_654_321, mode="new_message"),
    )
    assert result == {"status": "error", "reason": "not found"}
    assert denv.fake.requests == []


# ------------------------------------------------------------ PF-10: word bounds
async def test_a_draft_outside_the_word_range_is_retried_once_then_reported_honestly(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)
    eight = "one two three four five six seven eight"
    nine = "one two three four five six seven eight nine"
    denv.script(owner_output(text=nine), owner_output(text=eight))
    with capture_logs() as logs:
        result = await call(
            context(denv, account_id, run_id, turn_id),
            "propose_reply",
            **reply_args(conversation, min_words=5, max_words=7),
        )
    assert result["status"] == "awaiting_approval"
    assert (result["word_count"], result["asked_length"], result["length_met"]) == (
        8,
        "between 5 and 7 words",
        False,
    )
    [card] = await actions_of(denv.env, conversation)
    assert card.text == eight  # the closer of the two
    first, second = draft_requests(denv)
    retry = "\n".join(i.text for i in second.items if isinstance(i, TextItem))
    assert "That draft has 9 words" in retry
    assert "between 5 and 7 words" in retry
    assert len(second.items) == len(first.items) + 2
    [line] = [entry for entry in logs if entry.get("tool") == "propose_reply" and "code" in entry]
    assert (line["bounds_retry"], line["bounds_met"]) == (True, False)
    async with denv.env.factory() as session:
        [draft_run] = await session.scalars(
            select(AgentRun).where(AgentRun.run_type == "draft_reply")
        )
    assert draft_run.iterations == 2  # one run, two metered calls


async def test_a_draft_inside_the_range_is_kept_without_a_retry(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(text="one two three four five six"))
    result = await call(
        context(denv, account_id, run_id, turn_id),
        "propose_reply",
        **reply_args(conversation, min_words=5, max_words=7),
    )
    assert (result["word_count"], result["length_met"]) == (6, True)
    assert len(draft_requests(denv)) == 1


async def test_a_failed_bounds_retry_keeps_the_first_draft(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, _ = await run_and_turn(denv, account_id)
    denv.script(owner_output(text="far too many words in this one draft"), StopReason.REFUSAL)
    outcome = await denv.drafter().draft_owner(
        account_id=account_id,
        conversation_id=conversation,
        owner_run_id=run_id,
        bounds=WordBounds(None, 3),
    )
    assert outcome.status is DraftStatus.DRAFTED
    assert outcome.text == "far too many words in this one draft"
    assert (outcome.word_count, outcome.bounds_retry, outcome.bounds_met) == (8, True, False)


@pytest.mark.parametrize(
    ("words", "rule"),
    [
        ({"min_words": 7, "max_words": 5}, "min_words must not be above max_words"),
        ({"min_words": 0, "max_words": None}, "min_words and max_words are 1 to 1000, or null"),
    ],
)
async def test_bad_word_ranges_are_argument_errors(
    denv: DraftEnv, words: dict[str, Any], rule: str
) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    result = await call(
        context(denv, account_id, run_id, turn_id),
        "propose_reply",
        **(reply_args(conversation) | words),
    )
    assert result["status"] == "error"
    assert any(problem["rule"] == rule for problem in result["problems"])
    assert denv.fake.requests == []


# ------------------------------------------------------------ the revise mode (for B2)
async def test_the_revise_mode_rewrites_a_live_cards_own_text(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output(text="SYNTH-CURRENT-CARD-TEXT"))
    created = await call(
        context(denv, account_id, run_id, turn_id), "propose_reply", **reply_args(conversation)
    )
    # draft_owner.v2 (PF-13): a revision also says what the owner's words were.
    denv.script(
        owner_output(text="SYNTH-CURRENT-CARD-TEXT and see you", revision_kind="instruction")
    )
    outcome = await denv.drafter().draft_owner(
        account_id=account_id,
        conversation_id=conversation,
        owner_run_id=run_id,
        mode="revise",
        instruction="SYNTH add: and see you",
        run_tainted=True,
        revise_action_id=created["action_id"],
    )
    assert outcome.status is DraftStatus.DRAFTED
    assert outcome.text == "SYNTH-CURRENT-CARD-TEXT and see you"
    assert outcome.owner_mode == "revise"
    assert outcome.revision_kind == "instruction"
    revise = draft_requests(denv)[-1]
    current = next(
        item
        for item in revise.items
        if isinstance(item, TextItem) and "SYNTH-CURRENT-CARD-TEXT" in item.text
    )
    assert '<untrusted source="draft" trust="untrusted">' in current.text
    assert current.provenance is Provenance.MANAGED
    with pytest.raises(NotInAccountError):  # a chat that is not this account's
        await denv.drafter().draft_owner(
            account_id=account_id,
            conversation_id=conversation + 999,
            owner_run_id=run_id,
            mode="revise",
            revise_action_id=created["action_id"],
        )
    gone = await denv.drafter().draft_owner(
        account_id=account_id,
        conversation_id=conversation,
        owner_run_id=run_id,
        mode="revise",
        revise_action_id=created["action_id"] + 999,
    )
    assert (gone.status, gone.error_class) == (DraftStatus.NOT_DRAFTABLE, "no_live_draft")
    with pytest.raises(ValueError, match="tainted"):
        await denv.drafter().draft_owner(
            account_id=account_id,
            conversation_id=conversation,
            owner_run_id=run_id,
            guidance="SYNTH model words",
            run_tainted=True,
        )


# ------------------------------------------------------------ idempotency
async def test_a_repeated_owner_draft_call_drafts_once(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, _ = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    first = await call(
        context(denv, account_id, run_id, None), "propose_reply", **reply_args(conversation)
    )
    again = await call(
        context(denv, account_id, run_id, None), "propose_reply", **reply_args(conversation)
    )
    assert again["action_id"] == first["action_id"]
    assert again["reused"] is True
    assert "word_count" not in again  # nothing new was drafted
    assert "mode" not in again
    assert len(draft_requests(denv)) == 1


# ------------------------------------------------------------ PF-12: propose_message provenance
# D-S2 (2026-11-09) supersedes fuzzy/current-turn-only proof with C1 sources.
async def test_text_in_the_owners_own_words_is_dictated(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    said = "Shuhratga yoz: “Ertaga soat 5 da ko’rishamiz”"
    with capture_logs() as logs:
        result = await call(
            context(denv, account_id, run_id, turn_id, said=said),
            "propose_message",
            **message_args(conversation, "Ertaga soat 5 da ko’rishamiz"),
        )
    assert result["status"] == "awaiting_approval"
    assert result["proposal_status"] == "staged"
    assert result["written_by"] == "owner"
    [card] = await actions_of(denv.env, conversation)
    assert (card.origin, card.text) == (
        "owner_dictated",
        "Ertaga soat 5 da ko’rishamiz",
    )
    assert card.draft_flags is not None
    assert set(card.draft_flags) == {REQUEST_DIGEST}
    [line] = [entry for entry in logs if entry.get("tool") == "propose_message" and "code" in entry]
    assert line["provenance"] == "owner"


async def test_a_suggestion_the_assistant_made_is_the_assistants_proposal(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    suggestion = "SYNTH Assalomu alaykum, bugun gaplashamizmi?"
    async with denv.env.factory() as session, session.begin():
        session.add(
            ControlTurn(
                account_id=account_id,
                role="assistant",
                text=f"Mana ikki variant:\n1. SYNTH Salom!\n2. «{suggestion}»",
            )
        )
    run_id, turn_id = await run_and_turn(denv, account_id)
    result = await call(
        context(denv, account_id, run_id, turn_id, said="Ikkinchisini yubor"),
        "propose_message",
        **message_args(conversation, suggestion),
    )
    assert result["status"] == "awaiting_approval"
    assert result["proposal_status"] == "staged"
    assert result["written_by"] == "assistant_suggestion"
    [card] = await actions_of(denv.env, conversation)
    assert card.origin == "agent_proposal"  # never "Message you dictated"
    assert card.base_message_id is None
    assert card.draft_flags is not None
    assert card.draft_flags == {
        "source": "assistant_suggestion",
        "ai_generated": True,
        "target": "none",
        REQUEST_DIGEST: card.draft_flags[REQUEST_DIGEST],
    }
    assert card.trigger_turn_id == turn_id
    assert denv.fake.requests == []  # no model run


async def test_a_text_neither_the_owner_nor_the_assistant_wrote_is_refused(
    denv: DraftEnv,
) -> None:
    """ISSUE-007 (S4): a bare command was staged as "Message you dictated"."""
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    with capture_logs() as logs:
        result = await call(
            context(denv, account_id, run_id, turn_id, said="Compose something kind for Rustam"),
            "propose_message",
            **message_args(conversation, "Write a new message"),
        )
    assert result["status"] == "not_drafted"
    assert result["proposal_status"] == "invalid_arguments"
    assert result["action"] is None
    assert result["requested_change_applied"] is False
    assert await actions_of(denv.env, conversation) == []
    [line] = [entry for entry in logs if entry.get("tool") == "propose_message" and "code" in entry]
    assert line["code"] == "handler:invalid_source"


async def test_an_unselected_historical_or_foreign_reply_is_no_suggestion(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    old = "SYNTH-OLD-SUGGESTION from long ago"
    async with denv.env.factory() as session, session.begin():
        other = Account(telegram_user_id=7_000_000_099, control_chat_id=7_000_000_099)
        session.add(other)
        await session.flush()
        session.add(ControlTurn(account_id=other.id, role="assistant", text="SYNTH-FOREIGN-TEXT"))
        session.add(ControlTurn(account_id=account_id, role="assistant", text=old))
        for index in range(6):  # six newer replies push it out of reach
            session.add(
                ControlTurn(account_id=account_id, role="assistant", text=f"SYNTH reply {index}")
            )
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id, said="Send that one")
    for index, words in enumerate((old, "SYNTH-FOREIGN-TEXT")):
        result = await call(
            ctx, "propose_message", call_id=f"m{index}", **message_args(conversation, words)
        )
        assert result["status"] == "not_drafted", words
        assert result["proposal_status"] == "invalid_arguments"
        assert result["action"] is None
    assert await actions_of(denv.env, conversation) == []


async def _first_contact_message(denv: DraftEnv, conversation_id: int) -> int:
    async with denv.env.factory() as session:
        found = await session.scalar(
            select(Message.id)
            .where(Message.conversation_id == conversation_id, Message.direction == "contact")
            .order_by(Message.id)
            .limit(1)
        )
    assert found is not None
    return int(found)
