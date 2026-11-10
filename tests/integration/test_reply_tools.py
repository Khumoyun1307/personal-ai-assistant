"""S04.2 §3.5: `propose_reply`, `propose_message` (ARCHITECTURE §8.3; AGENTS §8 list).

Calls go through the real `ToolExecutor` (offer check, argument validation,
"not found" for foreign ids) with real `ToolServices`: the `Drafter` over a
scripted `FakeLLM`, F0's recording presenter and an `ActionsConfig`. One test
runs the whole owner runtime for the content-free step rows. Synthetic text.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from structlog.testing import capture_logs

from assistant.accounts.models import Account, AuditLogEntry, ControlTurn
from assistant.actions.models import Action
from assistant.actions.request_models import OwnerRequest, OwnerRequestSource
from assistant.actions.requests import (
    RequestApplied,
    RequestSource,
    SelectRecipient,
    SelectSuggestion,
    SetState,
    SetTarget,
    SourceSpan,
    open_request,
    update_request,
)
from assistant.actions.source_copy import CopyUnavailable, copy_request_body
from assistant.agent.models import AgentRunStep, ToolConfirmation
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime
from assistant.agent.tools.common import ONE_TARGET_RULE
from assistant.agent.tools.executor import ToolExecutor
from assistant.agent.tools.registry import (
    CHAT_ACCESS_OFF,
    CapabilityTier,
    OwnerTurn,
    ToolContext,
    ToolServices,
    default_registry,
)
from assistant.agent.tools.replies import (
    GUIDANCE_CHARS,
    REQUEST_DIGEST,
    ProposeReplyArgs,
    propose_reply_tool,
)
from assistant.conversations.models import Contact, Conversation, Message
from assistant.llm.port import LLMRequest, LLMResponse, StopReason, TextItem, ToolCall
from assistant.privacy.owner_requests import purge_request_sources
from assistant.privacy.requests import export_owner_requests
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import text_response, tool_response
from tests.integration.actions_env import seed_run, seed_turn
from tests.integration.agent_env import PRODUCTION_FLAGS, PROPOSE_FLAGS, ask, gated
from tests.integration.business_env import (
    OTHER_CONTACT,
    business_message,
    deleted_messages,
    deliver,
)
from tests.integration.drafting_env import (
    DraftEnv,
    actions_of,
    contact_deletes,
    contact_says,
    contact_shares_location,
    drafting_environment,
    later,
    message_row_id,
    owner_says,
    seed_chat,
)
from tests.unit.test_draft_owner_units import owner_output

pytestmark = pytest.mark.integration


async def source_message_args(
    denv: DraftEnv | ToolContext,
    account_id: int,
    conversation_id: int,
    field: str,
    *,
    start: int = 0,
    end: int | None = None,
    answered: int | None = None,
) -> dict[str, Any]:
    """Explicit synthetic owner literal-body selection, using the C1 service."""
    factory = denv.env.factory if isinstance(denv, DraftEnv) else denv.services.session_factory
    clock = denv.env.clock if isinstance(denv, DraftEnv) else denv.services.clock
    async with factory() as session, session.begin():
        turn = ControlTurn(
            account_id=account_id,
            role="owner",
            text=field,
            created_at=clock.now(),
        )
        session.add(turn)
        await session.flush()
        source = RequestSource(
            span=SourceSpan(
                turn_id=turn.id,
                field="text",
                start=start,
                end=len(field) if end is None else end,
                field_sha256=hashlib.sha256(field.encode("utf-8")).hexdigest(),
            ),
            purpose="literal_body",
        )
        request = await open_request(
            session,
            account_id=account_id,
            turn_id=turn.id,
            operation="dictate",
            sources=(source,),
            now=clock.now(),
        )
        contact_id = await session.scalar(
            select(Conversation.contact_id).where(
                Conversation.account_id == account_id,
                Conversation.id == conversation_id,
            )
        )
        assert contact_id is not None
        selected = await update_request(
            session,
            account_id=account_id,
            request_id=request.request_id,
            expected_version=request.version,
            update=SelectRecipient(
                contact_id=contact_id,
                conversation_id=conversation_id,
                basis="owner_choice",
            ),
            now=clock.now(),
        )
        assert isinstance(selected, RequestApplied)
        if answered is not None:
            targeted = await update_request(
                session,
                account_id=account_id,
                request_id=request.request_id,
                expected_version=selected.view.version,
                update=SetTarget(message_id=answered),
                now=clock.now(),
            )
            if isinstance(targeted, RequestApplied):
                selected = targeted
        ready = await update_request(
            session,
            account_id=account_id,
            request_id=request.request_id,
            expected_version=selected.view.version,
            update=SetState(state="ready", reason=None),
            now=clock.now(),
        )
        assert isinstance(ready, RequestApplied)
        source_id = await session.scalar(
            select(OwnerRequestSource.id).where(
                OwnerRequestSource.account_id == account_id,
                OwnerRequestSource.request_id == request.request_id,
            )
        )
        assert source_id is not None
        return {
            "request_id": request.request_id,
            "request_version": ready.view.version,
            "conversation_id": conversation_id,
            "answered_message_id": answered,
            "source_ids": [source_id],
        }


async def prepare_message(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    """Migrate historical synthetic fixtures to explicit C1 selections.

    Production accepts only source ids. These fixtures still spell out the
    selected text to keep old safety regressions readable; matching here is
    exact, and a missing selection deliberately gets an invalid source.
    """
    words: str = arguments["text"]
    turn = ctx.owner_turn
    field = turn.text if turn is not None and turn.text else "SYNTH missing source"
    start = field.find(words)
    valid = start >= 0 and bool(words.strip())
    async with ctx.services.session_factory() as session:
        conversation = arguments["conversation_id"]
        if conversation is None:
            conversation = await session.scalar(
                select(Conversation.id).where(
                    Conversation.account_id == ctx.account_id,
                    Conversation.contact_id == arguments["contact_id"],
                )
            )
        scoped = await session.scalar(
            select(Conversation.id).where(
                Conversation.account_id == ctx.account_id,
                Conversation.id == conversation,
            )
        )
        if scoped is None:
            return {
                "request_id": 999_999_999,
                "request_version": 1,
                "conversation_id": conversation or 999_999_999,
                "answered_message_id": arguments["answered_message_id"],
                "source_ids": [999_999_999],
            }
        # A fixture retry refers to the very same persisted selection.
        found = await session.scalar(
            select(OwnerRequestSource)
            .join(
                OwnerRequest,
                OwnerRequest.id == OwnerRequestSource.request_id,
            )
            .where(
                OwnerRequestSource.account_id == ctx.account_id,
                OwnerRequestSource.field_sha256 == hashlib.sha256(field.encode()).hexdigest(),
                OwnerRequestSource.start_char == max(start, 0),
                OwnerRequestSource.end_char == (start + len(words) if valid else len(field)),
                OwnerRequest.conversation_id == scoped,
                OwnerRequest.target_message_id == arguments["answered_message_id"],
                *([] if ctx.turn_id is None else [OwnerRequest.last_turn_id == ctx.turn_id]),
            )
            .order_by(OwnerRequestSource.id)
            .limit(1)
        )
        if found is not None:
            selected_id = found.id
            if found.purpose == "instruction":
                suggestion_id = await session.scalar(
                    select(OwnerRequestSource.id).where(
                        OwnerRequestSource.account_id == ctx.account_id,
                        OwnerRequestSource.request_id == found.request_id,
                        OwnerRequestSource.purpose == "suggestion",
                    )
                )
                if suggestion_id is not None:
                    selected_id = suggestion_id
            return {
                "request_id": found.request_id,
                "request_version": 3 + (arguments["answered_message_id"] is not None),
                "conversation_id": scoped,
                "answered_message_id": arguments["answered_message_id"],
                "source_ids": [selected_id],
            }
        suggestion = None
        if not valid:
            recent = list(
                await session.scalars(
                    select(ControlTurn)
                    .where(
                        ControlTurn.account_id == ctx.account_id,
                        ControlTurn.role == "assistant",
                    )
                    .order_by(ControlTurn.id.desc())
                    .limit(6)
                )
            )
            suggestion = next((row for row in recent if row.text and words in row.text), None)
    result = await source_message_args(
        ctx,
        ctx.account_id,
        scoped,
        field,
        start=start if valid else 0,
        end=start + len(words) if valid else len(field),
        answered=arguments["answered_message_id"],
    )
    async with ctx.services.session_factory() as session, session.begin():
        source = await session.get(OwnerRequestSource, result["source_ids"][0])
        assert source is not None
        own = await session.get(ControlTurn, source.turn_id)
        assert own is not None
        request = await session.get(OwnerRequest, source.request_id)
        assert request is not None
        if ctx.turn_id is not None:
            request.last_turn_id = ctx.turn_id
        if turn is not None:
            own.third_party = turn.third_party
            own.contains_managed = turn.contains_managed
            own.quoted_text = "SYNTH third-party quote" if turn.quoted else None
        if not valid:
            source.purpose = "instruction"
        if suggestion is not None:
            request.operation = "compose"
            assert suggestion.text is not None
            start = suggestion.text.index(words)
            selection = OwnerRequestSource(
                account_id=ctx.account_id,
                request_id=source.request_id,
                ordinal=1,
                turn_id=suggestion.id,
                field="text",
                purpose="suggestion",
                start_char=start,
                end_char=start + len(words),
                field_sha256=hashlib.sha256(suggestion.text.encode()).hexdigest(),
            )
            session.add(selection)
            await session.flush()
            selected = await update_request(
                session,
                account_id=ctx.account_id,
                request_id=request.id,
                expected_version=request.version,
                update=SelectSuggestion(
                    instruction_source_id=source.id, source_ids=(selection.id,)
                ),
                now=ctx.services.clock.now(),
            )
            assert isinstance(selected, RequestApplied)
            result["request_version"] = selected.view.version
            result["source_ids"] = [selection.id]
    return result


async def prepare_literal(ctx: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
    """Historical append fixtures select their separator from the owner field."""
    arguments = dict(arguments)
    words = arguments.pop("append_text")
    if words is None:
        return {**arguments, "literal": None}
    turn = ctx.owner_turn
    field = turn.text if turn is not None and turn.text else ""
    start = field.find(words)
    if start > 0 and field[start - 1] == " ":
        words = " " + words
    async with ctx.services.session_factory() as session:
        card = await session.scalar(
            select(Action)
            .where(
                Action.account_id == ctx.account_id,
                Action.id == arguments["action_id"]
                if arguments["action_id"] is not None
                else Action.conversation_id == arguments["conversation_id"],
            )
            .order_by(Action.id.desc())
            .limit(1)
        )
    if card is None:
        return {
            **arguments,
            "literal": {
                "request_id": 999_999_999,
                "request_version": 1,
                "expected_version": 1,
                "edit": "literal_append",
                "source_ids": [999_999_999],
            },
        }
    selected = await prepare_message(ctx, message_args(card.conversation_id, words))
    async with ctx.services.session_factory() as session, session.begin():
        request = await session.get(OwnerRequest, selected["request_id"])
        assert request is not None
        request.action_id = card.id
        request.action_version = card.version
        request.operation = "revise"
        prior = await session.scalar(
            select(AuditLogEntry.details)
            .where(
                AuditLogEntry.account_id == ctx.account_id,
                AuditLogEntry.event == "action_revision_requested",
                AuditLogEntry.subject_id == card.id,
                AuditLogEntry.details["turn_id"].astext == str(ctx.turn_id),
            )
            .order_by(AuditLogEntry.id)
            .limit(1)
        )
    expected = prior["version"] if prior is not None else card.version
    return {
        **arguments,
        "literal": {
            "request_id": selected["request_id"],
            "request_version": selected["request_version"],
            "expected_version": expected,
            "edit": "literal_append",
            "source_ids": selected["source_ids"],
        },
    }


async def test_dictation_survives_recipient_clarification_and_write_it(denv: DraftEnv) -> None:
    # D-S2 (2026-11-09) supersedes current-turn-only PF-12 authorship.
    account_id, conversation = await seed_chat(denv)
    body = "  X1 x/h, O‘g‘il! e\u0301\n👍  "
    arguments = await source_message_args(
        denv,
        account_id,
        conversation,
        f"Tell them:{body}",
        start=len("Tell them:"),
    )
    async with denv.env.factory() as session, session.begin():
        session.add(ControlTurn(account_id=account_id, role="owner", text="SYNTH Alex"))
    run_id, turn_id = await run_and_turn(denv, account_id)
    result = await call(
        context(denv, account_id, run_id, turn_id, said="write it"),
        "propose_message",
        **arguments,
    )
    assert result["status"] == "awaiting_approval"
    assert result["proposal_status"] == "staged"
    assert result["requested_change_applied"] is True
    assert result["action"] is not None
    [card] = await actions_of(denv.env, conversation)
    assert result["action_id"] == card.id
    assert card.text == body
    assert card.origin == "owner_dictated"
    assert denv.fake.requests == []


async def test_runtime_dictation_uses_only_model_visible_source_ids(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    body = "  X1 x/h, O‘g‘il! e\u0301\n👍  "
    # Prepare the durable request; none of its returned IDs reach the fake model.
    await source_message_args(
        denv, account_id, conversation, f"Tell them:{body}", start=len("Tell them:")
    )
    await ask(denv.env, "SYNTH Alex")
    turn_id = await ask(denv.env, "write it")

    def propose_from_context(request: LLMRequest) -> LLMResponse:
        [context] = [
            item.text
            for item in request.items
            if isinstance(item, TextItem) and item.text.startswith("Owner request continuation")
        ]
        facts = json.loads(context.split("\n", 1)[1])
        source_ids = [
            source["source_id"]
            for source in facts["sources"]
            if source["purpose"] == "literal_body"
        ]
        assert source_ids
        return tool_response(
            (
                "dictate-visible-sources",
                "propose_message",
                json.dumps(
                    {
                        "request_id": facts["request_id"],
                        "request_version": facts["request_version"],
                        "conversation_id": facts["conversation_id"],
                        "answered_message_id": facts["target_message_id"],
                        "source_ids": source_ids,
                    }
                ),
            )
        )

    denv.fake.queue(propose_from_context, text_response("A card is waiting for your approval."))
    runtime = OwnerRequestRuntime(
        session_factory=denv.env.factory,
        llm=gated(denv.env, denv.fake),
        channel=FakeChannel(),
        registry=default_registry(),
        clock=denv.env.clock,
        config=AgentConfig(model="gpt-6-luna", enabled_flags=PRODUCTION_FLAGS),
        approval_cards=denv.presenter,
        actions_config=denv.config,
    )
    await runtime.run(turn_id)
    [card] = await actions_of(denv.env, conversation)
    assert card.text == body
    assert card.origin == "owner_dictated"
    assert denv.presenter.presented == [card.id]


async def test_suggestion_selection_cannot_copy_an_unselected_retained_source(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    async with denv.env.factory() as session, session.begin():
        suggestions = [
            ControlTurn(account_id=account_id, role="assistant", text=f"SYNTH option {i}")
            for i in (1, 2)
        ]
        session.add_all(suggestions)
        await session.flush()
        suggestion_turn_ids = [turn.id for turn in suggestions]
    args = await source_message_args(denv, account_id, conversation, "Use the second option")
    async with denv.env.factory() as session, session.begin():
        instruction = await session.get(OwnerRequestSource, args["source_ids"][0])
        assert instruction is not None
        instruction.purpose = "instruction"
        sources = [
            OwnerRequestSource(
                account_id=account_id,
                request_id=args["request_id"],
                ordinal=index,
                turn_id=turn_id,
                field="text",
                purpose="suggestion",
                start_char=0,
                end_char=len(f"SYNTH option {index}"),
                field_sha256=hashlib.sha256(f"SYNTH option {index}".encode()).hexdigest(),
            )
            for index, turn_id in enumerate(suggestion_turn_ids, start=1)
        ]
        session.add_all(sources)
        await session.flush()
        unbound = await copy_request_body(
            session,
            account_id=account_id,
            request_id=args["request_id"],
            source_ids=[sources[1].id],
        )
        assert isinstance(unbound, CopyUnavailable)
        assert unbound.reason == "invalid_source"
        invalid = await update_request(
            session,
            account_id=account_id,
            request_id=args["request_id"],
            expected_version=args["request_version"],
            update=SelectSuggestion(
                instruction_source_id=sources[0].id, source_ids=(sources[1].id,)
            ),
            now=denv.env.clock.now(),
        )
        assert invalid.status == "unavailable"
        selected = await update_request(
            session,
            account_id=account_id,
            request_id=args["request_id"],
            expected_version=args["request_version"],
            update=SelectSuggestion(
                instruction_source_id=instruction.id, source_ids=(sources[1].id,)
            ),
            now=denv.env.clock.now(),
        )
        assert isinstance(selected, RequestApplied)
        args["request_version"] = selected.view.version
        unselected_id, selected_id = [source.id for source in sources]
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id, said="write it")
    rejected = await call(ctx, "propose_message", **(args | {"source_ids": [unselected_id]}))
    assert rejected["proposal_status"] == "invalid_arguments"
    assert rejected["action"] is None
    assert rejected["effect_version"] is None
    assert rejected["requested_change_applied"] is False
    assert await actions_of(denv.env, conversation) == []
    accepted = await call(
        ctx, "propose_message", call_id="selected", **(args | {"source_ids": [selected_id]})
    )
    assert accepted["proposal_status"] == "staged"
    assert accepted["written_by"] == "assistant_suggestion"
    [card] = await actions_of(denv.env, conversation)
    assert card.text == "SYNTH option 2"
    assert card.origin == "agent_proposal"
    async with denv.env.factory() as session, session.begin():
        exported = await export_owner_requests(session, account_id=account_id)
        [selection] = exported["suggestion_selections"]
        assert selection["source_ids"] == [selected_id]
        assert selection["instruction_source_id"] == args["source_ids"][0]
        await purge_request_sources(
            session,
            account_id=account_id,
            conversation_ids=(conversation,),
            now=denv.env.clock.now(),
        )
        erased = await export_owner_requests(session, account_id=account_id)
        assert erased["suggestion_selections"] == []


async def test_source_retry_returns_current_artifact_without_copying_again(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    args = await source_message_args(denv, account_id, conversation, "SYNTH X1 original")
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    first = await call(ctx, "propose_message", **args)
    async with denv.env.factory() as session, session.begin():
        source = await session.get(OwnerRequestSource, args["source_ids"][0])
        assert source is not None
        await session.execute(
            update(ControlTurn).where(ControlTurn.id == source.turn_id).values(text=None)
        )
        await session.execute(
            update(Action)
            .where(
                Action.id == first["action"]["action_id"],
            )
            .values(draft_flags=None)
        )
    retry = await call(ctx, "propose_message", **args)
    assert retry["status"] == "awaiting_approval"
    assert retry["proposal_status"] == "reused_exact"
    assert retry["action"]["action_id"] == first["action"]["action_id"]
    assert retry["effect_version"] == first["effect_version"]
    assert retry["requested_change_applied"] is False
    [card] = await actions_of(denv.env, conversation)
    assert card.version == 1
    assert len(denv.presenter.presented) == 1
    async with denv.env.factory() as session, session.begin():
        await session.execute(update(Action).where(Action.id == card.id).values(text=None))
    unavailable = await call(ctx, "propose_message", **args)
    assert unavailable["status"] == "not_drafted"
    assert unavailable["proposal_status"] == "not_available"
    assert unavailable["action"] is None
    assert unavailable["effect_version"] is None
    assert unavailable["requested_change_applied"] is False
    assert len(await actions_of(denv.env, conversation)) == 1
    async with denv.env.factory() as session:
        effects = await session.scalar(
            select(func.count())
            .select_from(AuditLogEntry)
            .where(
                AuditLogEntry.account_id == account_id,
                AuditLogEntry.event == "tool_effect_recorded",
            )
        )
    assert effects == 1


@pytest.mark.parametrize(
    "missing",
    [
        "purged",
        "deleted",
        "changed",
        "foreign",
        "instruction",
        "quoted",
        "unclassified_quote",
        "forwarded",
        "assistant",
        "whitespace",
        "too_long",
    ],
)
async def test_invalid_literal_source_has_no_action_receipt(
    denv: DraftEnv,
    missing: str,
) -> None:
    from sqlalchemy import delete

    account_id, conversation = await seed_chat(denv)
    existing_args = await source_message_args(denv, account_id, conversation, "SYNTH existing card")
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    await call(ctx, "propose_message", **existing_args)
    [existing] = await actions_of(denv.env, conversation)
    presented = list(denv.presenter.presented)
    field = {"whitespace": "  \n ", "too_long": "👍" * 2049}.get(missing, "SYNTH X1")
    arguments = await source_message_args(denv, account_id, conversation, field)
    async with denv.env.factory() as session, session.begin():
        source = await session.get(OwnerRequestSource, arguments["source_ids"][0])
        assert source is not None
        if missing == "foreign":
            arguments["request_id"] = 999_999_999
        elif missing == "deleted":
            await session.execute(delete(ControlTurn).where(ControlTurn.id == source.turn_id))
        elif missing == "instruction":
            source.purpose = "instruction"
        elif missing == "quoted":
            source.field = "quoted_text"
            source.purpose = "quote"
        elif missing == "unclassified_quote":
            await session.execute(
                update(ControlTurn)
                .where(ControlTurn.id == source.turn_id)
                .values(
                    third_party=True,
                    quoted_text="SYNTH legacy quote with unclassified outer text",
                    owner_text_verified=False,
                )
            )
        elif missing in {"forwarded", "assistant"}:
            await session.execute(
                update(ControlTurn)
                .where(ControlTurn.id == source.turn_id)
                .values(
                    **({"third_party": True} if missing == "forwarded" else {"role": "assistant"}),
                )
            )
        elif missing not in {"whitespace", "too_long"}:
            await session.execute(
                update(ControlTurn)
                .where(ControlTurn.id == source.turn_id)
                .values(
                    text=None if missing == "purged" else "SYNTH x1",
                )
            )
    result = await call(
        ctx,
        "propose_message",
        call_id="invalid-source",
        **arguments,
    )
    assert result["status"] == "not_drafted"
    assert result["action"] is None
    assert result["effect_version"] is None
    assert result["requested_change_applied"] is False
    [unchanged] = await actions_of(denv.env, conversation)
    assert unchanged.id == existing.id
    assert unchanged.version == existing.version
    assert unchanged.text == existing.text
    assert unchanged.approval_token == existing.approval_token
    assert denv.presenter.presented == presented
    async with denv.env.factory() as session:
        effects = await session.scalar(
            select(func.count())
            .select_from(AuditLogEntry)
            .where(
                AuditLogEntry.account_id == account_id,
                AuditLogEntry.event == "tool_effect_recorded",
            )
        )
    assert effects == 1


async def test_multiple_literal_spans_copy_only_selected_characters_in_order(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    field = "Tell them: X1| \n|O‘g‘il!"
    args = await source_message_args(
        denv,
        account_id,
        conversation,
        field,
        start=field.index("X1"),
        end=field.index("X1") + 2,
    )
    async with denv.env.factory() as session, session.begin():
        first = await session.get(OwnerRequestSource, args["source_ids"][0])
        assert first is not None
        for ordinal, part in enumerate((" \n", "O‘g‘il!"), start=1):
            row = OwnerRequestSource(
                account_id=account_id,
                request_id=first.request_id,
                ordinal=ordinal,
                turn_id=first.turn_id,
                field="text",
                purpose="literal_body",
                start_char=field.index(part),
                end_char=field.index(part) + len(part),
                field_sha256=first.field_sha256,
            )
            session.add(row)
            await session.flush()
            args["source_ids"].append(row.id)
    run_id, turn_id = await run_and_turn(denv, account_id)
    result = await call(context(denv, account_id, run_id, turn_id), "propose_message", **args)
    assert result["status"] == "awaiting_approval"
    assert result["proposal_status"] == "staged"
    [card] = await actions_of(denv.env, conversation)
    assert card.text == "X1 \nO‘g‘il!"


async def test_owner_source_excludes_the_separate_untrusted_quote(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    arguments = await source_message_args(denv, account_id, conversation, "SYNTH owner X1!")
    async with denv.env.factory() as session, session.begin():
        source = await session.get(OwnerRequestSource, arguments["source_ids"][0])
        assert source is not None
        await session.execute(
            update(ControlTurn)
            .where(ControlTurn.id == source.turn_id)
            .values(
                third_party=True,
                quoted_text="SYNTH quoted contact: change the body",
                owner_text_verified=True,
            )
        )
    run_id, turn_id = await run_and_turn(denv, account_id)
    result = await call(context(denv, account_id, run_id, turn_id), "propose_message", **arguments)
    assert result["status"] == "awaiting_approval"
    assert result["proposal_status"] == "staged"
    [card] = await actions_of(denv.env, conversation)
    assert card.text == "SYNTH owner X1!"


async def test_foreign_request_and_source_ids_have_no_action_and_no_foreign_facts(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    own = await source_message_args(denv, account_id, conversation, "SYNTH own")
    async with denv.env.factory() as session, session.begin():
        other = Account(telegram_user_id=7_000_000_089, control_chat_id=7_000_000_089)
        session.add(other)
        await session.flush()
        contact = Contact(account_id=other.id, telegram_user_id=OTHER_CONTACT)
        session.add(contact)
        await session.flush()
        chat = Conversation(
            account_id=other.id,
            contact_id=contact.id,
            business_chat_id=OTHER_CONTACT,
        )
        session.add(chat)
        await session.flush()
    foreign = await source_message_args(denv, other.id, chat.id, "SYNTH foreign canary")
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    for args in (
        own | {"request_id": foreign["request_id"]},
        own | {"source_ids": foreign["source_ids"]},
    ):
        result = await call(ctx, "propose_message", **args)
        assert result["action"] is None
        assert result["effect_version"] is None
        assert result["requested_change_applied"] is False
        assert "foreign canary" not in json.dumps(result)
    assert await actions_of(denv.env, conversation) == []


GUIDANCE_CANARY = "SYNTH-GUIDANCE-CANARY offer Monday"
SENT = re.compile(r"\bsent\b", re.IGNORECASE)


@pytest.fixture
async def denv(migrated_url: str) -> AsyncIterator[DraftEnv]:
    async with drafting_environment(migrated_url) as built:
        yield built


async def run_and_turn(denv: DraftEnv, account_id: int) -> tuple[int, int]:
    async with denv.env.factory() as session, session.begin():
        return await seed_run(session, account_id), await seed_turn(session, account_id)


def services(denv: DraftEnv, **changes: Any) -> ToolServices:
    values: dict[str, Any] = {
        "session_factory": denv.env.factory,
        "clock": denv.env.clock,
        "monthly_budget_usd": None,
        "drafter": denv.drafter(),
        "approval_cards": denv.presenter,
        "actions_config": denv.config,
    }
    values.update(changes)
    return ToolServices(**values)


#: The owner's own words in the turn these tests answer: it dictates every
#: synthetic text the tests below propose (`propose_message` checks that a
#: dictated text is the owner's, PF-12).
OWNER_SAID = (
    "Tell them: SYNTH-DICTATED. Also SYNTH-ONE, SYNTH-X, SYNTH-Y, SYNTH-BY-CONTACT and "
    "SYNTH-AND-ONE-MORE-THING."
)


def owner_turn(turn_id: int | None, said: str, **flags: bool) -> OwnerTurn:
    values = {"third_party": False, "contains_managed": False, "quoted": False} | flags
    return OwnerTurn(id=turn_id or 0, text=said, **values)


def context(
    denv: DraftEnv,
    account_id: int,
    run_id: int,
    turn_id: int | None,
    *,
    tainted: bool = False,
    said: str | None = OWNER_SAID,
    turn: OwnerTurn | None = None,
    **service_changes: Any,
) -> ToolContext:
    """A tool context as the runtime builds it; `said` is the owner's turn
    (None: no owner turn, as for a call applied after a confirmation tap)."""
    return ToolContext(
        account_id=account_id,
        run_id=run_id,
        run_type="owner_request",
        tainted=tainted,
        services=services(denv, **service_changes),
        turn_id=turn_id,
        owner_turn=turn or (owner_turn(turn_id, said) if said is not None else None),
        managed_ok=True,
    )


async def call(
    ctx: ToolContext,
    name: str,
    *,
    call_id: str = "c1",
    managed: bool = True,
    flags: frozenset[str] = PROPOSE_FLAGS,
    **arguments: Any,
) -> dict[str, Any]:
    """One call through the executor, as the runtime makes it."""
    if name == "propose_message" and "text" in arguments:
        arguments = await prepare_message(ctx, arguments)
    registry = default_registry()
    offered = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=flags,
        managed_consent=managed,
        stub_managed_tools=True,
    )

    async def record(*_step: object) -> None:
        return None

    executor = ToolExecutor(
        context=ctx, offered=offered, record_step=record, clock=ctx.services.clock
    )
    item = await executor.execute(ToolCall(call_id, name, json.dumps(arguments)))
    result: dict[str, Any] = json.loads(item.output)
    return result


def reply_args(
    conversation_id: int | None,
    *,
    contact_id: int | None = None,
    guidance: Any = None,
    mode: str = "reply",
    answered: int | None = None,
    min_words: int | None = None,
    max_words: int | None = None,
) -> dict[str, Any]:
    """Every field: the strict schema requires them all (null = not given)."""
    return {
        "conversation_id": conversation_id,
        "contact_id": contact_id,
        "mode": mode,
        "answered_message_id": answered,
        "guidance": guidance,
        "min_words": min_words,
        "max_words": max_words,
    }


def message_args(
    conversation_id: int | None,
    words: str,
    *,
    contact_id: int | None = None,
    answered: int | None = None,
) -> dict[str, Any]:
    """Every field: the strict schema requires them all (null = not given)."""
    return {
        "conversation_id": conversation_id,
        "contact_id": contact_id,
        "text": words,
        "answered_message_id": answered,
    }


async def close_window(denv: DraftEnv, conversation_id: int) -> None:
    async with denv.env.factory() as session, session.begin():
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(last_contact_msg_at=denv.env.clock.now() - timedelta(hours=30))
        )


async def possibly_paused(denv: DraftEnv, conversation_id: int) -> None:
    """A proposal-phase block that is not the window (P2-O20 lifted that one
    for owner-approved actions): the chat looks paused."""
    async with denv.env.factory() as session, session.begin():
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(availability="possibly_paused")
        )


def never_says_sent(result: dict[str, Any]) -> None:
    assert not SENT.search(json.dumps(result)), result


def draft_prompts(denv: DraftEnv) -> list[str]:
    return [
        request.instructions + "\n".join(i.text for i in request.items if isinstance(i, TextItem))
        for request in denv.fake.requests
        if request.purpose == "draft_reply"
    ]


# ------------------------------------------------------------ RG-20 and statuses
async def test_rg20_propose_results_never_say_sent(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)

    denv.script(owner_output(text="Synthetic reply; it was sent? No, a draft."))
    awaiting = await call(
        context(denv, account_id, run_id, turn_id), "propose_reply", **reply_args(conversation)
    )
    assert awaiting["status"] == "awaiting_approval"
    assert awaiting["card"] == "queued for the owner"
    assert "text" not in awaiting  # the owner reads the draft on the card
    assert awaiting["sendable_until"] is not None
    never_says_sent(awaiting)

    # An owner's request is never "no reply needed" (D1): only the provider's
    # refusal or an unusable output ends without a card, said as it is.
    denv.script(StopReason.REFUSAL)
    declined = await call(
        context(denv, account_id, run_id, None),
        "propose_reply",
        call_id="c2",
        **reply_args(conversation),
    )
    assert declined == {
        "status": "declined_by_provider",
        "conversation_id": conversation,
        "reason": "provider_refused",
    }
    never_says_sent(declined)
    denv.script(owner_output(text="   "))
    not_drafted = await call(
        context(denv, account_id, run_id, None),
        "propose_reply",
        call_id="c2b",
        **reply_args(conversation),
    )
    assert not_drafted == {
        "status": "not_drafted",
        "conversation_id": conversation,
        "reason": "invalid",
    }
    never_says_sent(not_drafted)

    await possibly_paused(denv, conversation)
    blocked = await call(
        context(denv, account_id, run_id, None),
        "propose_message",
        call_id="c3",
        **message_args(conversation, "SYNTH-DICTATED"),
    )
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "policy_denied"
    assert blocked["sendable_until"] is None
    never_says_sent(blocked)


async def test_propose_results_distinguish_success_from_no_action_in_logs(denv: DraftEnv) -> None:
    """Created cards are successful calls; no-action outcomes stay at info."""
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    with capture_logs() as logs:
        denv.script(owner_output(text="Synthetic reply."))
        await call(
            context(denv, account_id, run_id, turn_id), "propose_reply", **reply_args(conversation)
        )
        denv.script(owner_output(text=""))
        await call(
            context(denv, account_id, run_id, None),
            "propose_reply",
            call_id="c2",
            **reply_args(conversation),
        )
        denv.script(StopReason.REFUSAL)
        await call(
            context(denv, account_id, run_id, None),
            "propose_reply",
            call_id="c2b",
            **reply_args(conversation),
        )
        await possibly_paused(denv, conversation)
        await call(
            context(denv, account_id, run_id, None),
            "propose_message",
            call_id="c3",
            **message_args(conversation, "SYNTH-DICTATED"),
        )
    lines = [
        (entry["event"], entry["log_level"], entry["status"])
        for entry in logs
        if entry["event"]
        in {"agent_tool_call_succeeded", "agent_tool_call_failed", "agent_tool_call_not_run"}
    ]
    assert lines == [
        ("agent_tool_call_succeeded", "info", "awaiting_approval"),
        ("agent_tool_call_not_run", "info", "not_drafted"),
        ("agent_tool_call_not_run", "info", "declined_by_provider"),
        ("agent_tool_call_not_run", "info", "blocked"),
    ]


async def test_with_sending_off_proposals_are_normal_awaiting_cards(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    off = replace(denv.config, sending_enabled=False)
    result = await call(
        context(denv, account_id, run_id, turn_id, actions_config=off),
        "propose_message",
        **message_args(conversation, "SYNTH-DICTATED"),
    )
    assert result["status"] == "awaiting_approval"
    assert result["proposal_status"] == "staged"
    [card] = await actions_of(denv.env, conversation)
    assert card.state == "awaiting_approval"
    assert card.approval_token


async def test_propose_message_keeps_the_owners_exact_text(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    words = "SYNTH  exact,  words!\nLine two — ok 👍"
    result = await call(
        context(denv, account_id, run_id, turn_id, said=f"Send them exactly this:   {words}\n"),
        "propose_message",
        **message_args(conversation, f"  {words}\n"),
    )
    assert result["status"] == "awaiting_approval"
    assert result["proposal_status"] == "staged"
    [card] = await actions_of(denv.env, conversation)
    assert card.text == f"  {words}\n"
    assert denv.fake.requests == []  # no model run for dictated text


async def test_propose_message_has_no_base_message(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    await owner_says(denv, "SYNTH-OWNER-LATEST")
    run_id, turn_id = await run_and_turn(denv, account_id)
    await call(
        context(denv, account_id, run_id, turn_id),
        "propose_message",
        **message_args(conversation, "SYNTH-DICTATED"),
    )
    [card] = await actions_of(denv.env, conversation)
    async with denv.env.factory() as session:
        newest = await session.scalar(
            select(Message.id)
            .where(Message.conversation_id == conversation)
            .order_by(Message.id.desc())
            .limit(1)
        )
    assert card.origin == "owner_dictated"
    assert card.base_message_id is None
    assert card.shown_through_message_id == newest  # newest of any direction
    assert card.draft_flags is not None
    assert set(card.draft_flags) == {REQUEST_DIGEST}
    assert card.draft_run_id is None
    assert card.trigger_turn_id == turn_id
    assert (card.run_id, card.tool_call_id) == (run_id, "c1")


async def test_a_dictated_message_right_after_the_owners_manual_reply_is_a_normal_card(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    replied = await owner_says(denv, "SYNTH-OWNER-REPLIED-MANUALLY", date=later(120))
    # A minute after the owner's own manual reply (decisions R3).
    denv.env.clock.set(datetime.fromtimestamp(later(180), tz=UTC))
    run_id, turn_id = await run_and_turn(denv, account_id)
    result = await call(
        context(denv, account_id, run_id, turn_id),
        "propose_message",
        **message_args(conversation, "SYNTH-AND-ONE-MORE-THING"),
    )
    assert result["status"] == "awaiting_approval"
    assert result["proposal_status"] == "staged"
    [card] = await actions_of(denv.env, conversation)
    assert card.state == "awaiting_approval"
    assert card.base_message_id is None
    assert card.shown_through_message_id == await message_row_id(denv.env, replied)


# ------------------------------------------------------------ guidance and taint
async def test_propose_reply_in_a_tainted_run_ignores_the_guidance_and_says_so(
    denv: DraftEnv,
) -> None:
    # Decisions R2, kept for the MODEL's words: a tainted run's guidance may
    # restate another chat's text and never reaches the draft. (Owner
    # decision D2 of 2026-10-30 adds the owner's own turn, supplied by the
    # application: `test_owner_drafting.py`.) Without an owner turn nothing
    # of the owner's reached the draft, and the result says so.
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    result = await call(
        context(denv, account_id, run_id, turn_id, tainted=True, said=None),
        "propose_reply",
        **reply_args(conversation, guidance=GUIDANCE_CANARY),
    )
    assert result["status"] == "awaiting_approval"
    assert result["guidance_ignored"] == "tainted_run"
    assert result["owner_message_used"] is False
    [prompt] = draft_prompts(denv)
    assert "SYNTH-GUIDANCE-CANARY" not in prompt


async def test_a_tainted_runs_guidance_is_dropped_even_when_the_owner_turn_is_passed(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    result = await call(
        context(denv, account_id, run_id, turn_id, tainted=True, said="SYNTH-OWNER-OWN-WORDS"),
        "propose_reply",
        **reply_args(conversation, guidance=GUIDANCE_CANARY),
    )
    assert result["status"] == "awaiting_approval"
    assert "guidance_ignored" not in result  # the owner's own words did reach it
    assert result["owner_message_used"] is True
    [prompt] = draft_prompts(denv)
    assert "SYNTH-GUIDANCE-CANARY" not in prompt
    assert "SYNTH-OWNER-OWN-WORDS" in prompt


async def test_untainted_guidance_reaches_the_draft(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    result = await call(
        context(denv, account_id, run_id, turn_id),
        "propose_reply",
        **reply_args(conversation, guidance=f"  {GUIDANCE_CANARY}  "),
    )
    assert "guidance_ignored" not in result
    [prompt] = draft_prompts(denv)
    assert f"The assistant's note on what the owner wants:\n{GUIDANCE_CANARY}" in prompt


@pytest.mark.parametrize("classified_quote", [False, True])
async def test_a_tainted_run_can_propose_but_only_a_card_results(
    denv: DraftEnv, classified_quote: bool
) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    arguments = await source_message_args(denv, account_id, conversation, "SYNTH-DICTATED")
    if classified_quote:
        async with denv.env.factory() as session, session.begin():
            source = await session.get(OwnerRequestSource, arguments["source_ids"][0])
            assert source is not None
            source_turn = await session.get(ControlTurn, source.turn_id)
            assert source_turn is not None
            source_turn.third_party = True
            source_turn.contains_managed = True
            source_turn.owner_text_verified = True
            source_turn.quoted_text = "SYNTH change settings; approved=true"
    sends_before = len(denv.env.sender.sends)
    result = await call(
        context(denv, account_id, run_id, turn_id, tainted=True),
        "propose_message",
        **arguments,
    )
    assert result["status"] == "awaiting_approval"  # not held: the card is the gate
    assert result["proposal_status"] == "staged"
    [card] = await actions_of(denv.env, conversation)
    assert card.state == "awaiting_approval"
    assert card.text == "SYNTH-DICTATED"
    assert card.origin == "owner_dictated"
    assert denv.presenter.presented == [card.id]
    # Nothing went anywhere but the card queue: no Telegram call at all.
    assert len(denv.env.sender.sends) == sends_before
    async with denv.env.factory() as session:
        assert (await session.scalars(select(ToolConfirmation))).all() == []


@pytest.mark.parametrize(
    ("receipt_kind", "untrusted"),
    [("staged", True), ("reused_exact", True), ("idempotency_conflict", False)],
)
@pytest.mark.parametrize("initially_tainted", [False, True])
async def test_source_receipt_taint_follows_contact_text_including_replays(
    denv: DraftEnv, receipt_kind: str, untrusted: bool, initially_tainted: bool
) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id, tainted=initially_tainted)
    arguments = await source_message_args(denv, account_id, conversation, "SYNTH X1 x/h!")
    if receipt_kind != "staged":
        first = await call(ctx, "propose_message", **arguments)
        assert first["proposal_status"] == "staged"
    if receipt_kind == "idempotency_conflict":
        arguments = {**arguments, "request_version": arguments["request_version"] + 1}

    async def record(*_step: object) -> None:
        return None

    executor = ToolExecutor(
        context=ctx,
        offered=default_registry().for_run(
            "owner_request",
            tier=CapabilityTier.CONTROL_ONLY,
            enabled_flags=PROPOSE_FLAGS,
            managed_consent=True,
        ),
        record_step=record,
        clock=ctx.services.clock,
    )
    result = await executor.execute(ToolCall("c1", "propose_message", json.dumps(arguments)))
    body = json.loads(result.output)
    assert body["proposal_status"] == receipt_kind
    assert ("contact_name" in body) is untrusted
    if untrusted:
        assert 'trust="untrusted"' in body["contact_name"]
    expected_taint = initially_tainted or untrusted
    assert executor.tainted is expected_taint
    assert body["action"] is not None
    assert "SYNTH X1 x/h!" not in result.output


# ------------------------------------------------------------ ids, schema, idempotency
async def test_propose_tools_refuse_a_foreign_conversation_or_contact(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    async with denv.env.factory() as session, session.begin():
        other = Account(telegram_user_id=7_000_000_077, control_chat_id=7_000_000_077)
        session.add(other)
        await session.flush()
        stranger = Contact(account_id=other.id, telegram_user_id=OTHER_CONTACT)
        session.add(stranger)
        await session.flush()
        foreign = Conversation(
            account_id=other.id, contact_id=stranger.id, business_chat_id=OTHER_CONTACT
        )
        session.add(foreign)
        await session.flush()
        foreign_ids = (foreign.id, stranger.id)
    ctx = context(denv, account_id, run_id, turn_id)
    for name, arguments in (
        ("propose_reply", reply_args(foreign_ids[0])),
        ("propose_reply", reply_args(None, contact_id=foreign_ids[1])),
        ("propose_message", message_args(foreign_ids[0], "SYNTH")),
        ("propose_message", message_args(None, "SYNTH", contact_id=foreign_ids[1])),
        ("propose_message", message_args(999_999_999, "SYNTH")),
    ):
        result = await call(ctx, name, **arguments)
        if name == "propose_reply":
            assert result == {"status": "error", "reason": "not found"}
        else:
            assert result["status"] == "not_drafted"
            assert result["proposal_status"] == "not_available"
            assert result["reason"] == "not_found"
            assert result["action"] is None
    assert denv.fake.requests == []
    async with denv.env.factory() as session:
        assert (await session.scalars(select(Action))).all() == []
    assert conversation


async def test_schema_valid_but_both_or_neither_target_is_an_error(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    contact = await _contact_of(denv, conversation)
    for arguments in (
        reply_args(conversation, contact_id=contact),
        reply_args(None),
    ):
        result = await call(ctx, "propose_reply", **arguments)
        assert result["status"] == "error"
        assert {"field": "(root)", "rule": ONE_TARGET_RULE} in result["problems"]
    too_long = await call(ctx, "propose_reply", **reply_args(conversation, guidance="x" * 401))
    assert too_long["status"] == "error"
    assert too_long["fields"] == ["guidance"]
    assert GUIDANCE_CHARS == 400
    blank = await call(ctx, "propose_message", **message_args(conversation, "   "))
    assert blank["status"] == "not_drafted"
    assert blank["proposal_status"] == "invalid_arguments"
    assert blank["action"] is None
    assert denv.fake.requests == []
    assert await actions_of(denv.env, conversation) == []


async def test_a_repeated_call_id_creates_no_second_action(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    first = await call(
        context(denv, account_id, run_id, None), "propose_reply", **reply_args(conversation)
    )
    # Another executor (a re-run), the same model call id: no second draft.
    again = await call(
        context(denv, account_id, run_id, None), "propose_reply", **reply_args(conversation)
    )
    assert again["action_id"] == first["action_id"]
    assert again["reused"] is True  # nothing new was created
    assert "reused" not in first
    assert len(denv.fake.requests) == 1
    dictated = await call(
        context(denv, account_id, run_id, None),
        "propose_message",
        call_id="c9",
        **message_args(conversation, "SYNTH-ONE"),
    )
    repeat = await call(
        context(denv, account_id, run_id, None),
        "propose_message",
        call_id="c9",
        **message_args(conversation, "SYNTH-ONE"),
    )
    assert repeat["action_id"] == dictated["action_id"]
    assert repeat["reused"] is True
    assert "reused" not in dictated
    assert len(await actions_of(denv.env, conversation)) == 2
    assert turn_id


async def test_a_retried_owner_turn_reuses_the_awaiting_card_without_drafting_again(
    denv: DraftEnv,
) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    denv.script(owner_output())
    first = await call(
        context(denv, account_id, run_id, turn_id), "propose_reply", **reply_args(conversation)
    )
    # The worker died; the turn is run again as a new run with new call ids.
    async with denv.env.factory() as session, session.begin():
        second_run = await seed_run(session, account_id)
    again = await call(
        context(denv, account_id, second_run, turn_id),
        "propose_reply",
        call_id="other",
        **reply_args(conversation),
    )
    assert again["status"] == "awaiting_approval"
    assert again["action_id"] == first["action_id"]
    assert again["reused"] is True
    assert len(denv.fake.requests) == 1  # no second draft, no cost
    assert len(await actions_of(denv.env, conversation)) == 1


async def test_a_changed_message_in_the_same_turn_conflicts_with_the_first_card(
    denv: DraftEnv,
) -> None:
    # R2-12: a correction must not claim that the new words were staged.
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    first = await call(ctx, "propose_message", **message_args(conversation, "SYNTH-X"))
    second = await call(
        ctx, "propose_message", call_id="c2", **message_args(conversation, "SYNTH-Y")
    )
    assert first["status"] == "awaiting_approval"
    assert first["proposal_status"] == "staged"
    assert second["status"] == "not_drafted"
    assert second["proposal_status"] == "proposal_conflict"
    assert second["action_id"] == first["action_id"]
    assert second["state"] == "awaiting_approval"
    assert "revise_draft" in second["rule"]
    assert "ask the owner" in second["rule"]
    assert "reused" not in second
    assert "card" not in second
    assert "reused" not in first
    [card] = await actions_of(denv.env, conversation)
    assert card.text == "SYNTH-X"


@pytest.mark.parametrize("tool", ["propose_reply", "propose_message"])
async def test_identical_proposal_with_a_new_call_id_reuses_the_card(
    denv: DraftEnv, tool: str
) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    denv.script(owner_output())
    arguments = (
        reply_args(conversation)
        if tool == "propose_reply"
        else message_args(conversation, "SYNTH-X")
    )
    first = await call(ctx, tool, **arguments)
    second = await call(ctx, tool, call_id="c2", **arguments)
    assert second["status"] == "awaiting_approval"
    if tool == "propose_message":
        assert second["proposal_status"] == "reused_exact"
    assert second["action_id"] == first["action_id"]
    assert second["reused"] is True
    assert len(await actions_of(denv.env, conversation)) == 1
    assert len(denv.fake.requests) == (1 if tool == "propose_reply" else 0)


@pytest.mark.parametrize(
    "changed", ["mode", "guidance", "answered_message_id", "min_words", "max_words"]
)
async def test_changed_reply_arguments_conflict_without_another_draft(
    denv: DraftEnv, changed: str
) -> None:
    account_id, conversation = await seed_chat(denv)
    telegram_id = await contact_says(denv, "SYNTH-QUESTION")
    answered = await message_row_id(denv.env, telegram_id)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    denv.script(owner_output())
    arguments = reply_args(conversation)
    first = await call(ctx, "propose_reply", **arguments)
    changes: dict[str, Any] = {
        "mode": "new_message",
        "guidance": "SYNTH-CORRECTION",
        "answered_message_id": answered,
        "min_words": 10,
        "max_words": 20,
    }
    arguments[changed] = changes[changed]
    second = await call(ctx, "propose_reply", call_id="c2", **arguments)
    assert second["status"] == "conflict_existing_draft"
    assert second["action_id"] == first["action_id"]
    assert second["state"] == "awaiting_approval"
    assert "reused" not in second
    assert "card" not in second
    assert len(denv.fake.requests) == 1
    [card] = await actions_of(denv.env, conversation)
    assert card.id == first["action_id"]
    assert card.version == 1


async def test_changed_message_answer_target_conflicts(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    telegram_id = await contact_says(denv, "SYNTH-QUESTION")
    answered = await message_row_id(denv.env, telegram_id)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    first = await call(ctx, "propose_message", **message_args(conversation, "SYNTH-X"))
    second = await call(
        ctx,
        "propose_message",
        call_id="c2",
        **message_args(conversation, "SYNTH-X", answered=answered),
    )
    assert second["status"] == "not_drafted"
    assert second["proposal_status"] == "proposal_conflict"
    assert second["action_id"] == first["action_id"]
    [card] = await actions_of(denv.env, conversation)
    assert card.text == "SYNTH-X"
    assert card.base_message_id is None


@pytest.mark.parametrize("first_tool", ["propose_reply", "propose_message"])
async def test_switching_proposal_tools_in_the_same_turn_conflicts(
    denv: DraftEnv, first_tool: str
) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    denv.script(owner_output())
    arguments = {
        "propose_reply": reply_args(conversation),
        "propose_message": message_args(conversation, "SYNTH-X"),
    }
    second_tool = "propose_message" if first_tool == "propose_reply" else "propose_reply"
    first = await call(ctx, first_tool, **arguments[first_tool])
    second = await call(ctx, second_tool, call_id="c2", **arguments[second_tool])
    assert second["status"] == (
        "conflict_existing_draft" if second_tool == "propose_reply" else "not_drafted"
    )
    if second_tool == "propose_message":
        assert second["proposal_status"] == "proposal_conflict"
    assert second["action_id"] == first["action_id"]
    assert len(await actions_of(denv.env, conversation)) == 1
    assert len(denv.fake.requests) == (1 if first_tool == "propose_reply" else 0)


@pytest.mark.parametrize("tool", ["propose_reply", "propose_message"])
async def test_same_call_id_keeps_its_action_even_with_different_arguments(
    denv: DraftEnv, tool: str
) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    denv.script(owner_output())
    arguments = (
        reply_args(conversation)
        if tool == "propose_reply"
        else message_args(conversation, "SYNTH-X")
    )
    first = await call(ctx, tool, **arguments)
    arguments.update(
        {"guidance": "SYNTH-CHANGED"} if tool == "propose_reply" else {"text": "SYNTH-Y"}
    )
    second = await call(ctx, tool, **arguments)
    assert second["status"] == ("awaiting_approval" if tool == "propose_reply" else "not_drafted")
    assert second["action_id"] == first["action_id"]
    if tool == "propose_reply":
        assert second["reused"] is True
    else:
        assert second["proposal_status"] == "idempotency_conflict"
        assert second["requested_change_applied"] is False
    assert len(await actions_of(denv.env, conversation)) == 1
    assert len(denv.fake.requests) == (1 if tool == "propose_reply" else 0)


async def test_card_without_request_metadata_conflicts_instead_of_guessing(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    arguments = message_args(conversation, "SYNTH-X")
    first = await call(ctx, "propose_message", **arguments)
    async with denv.env.factory() as session, session.begin():
        await session.execute(
            update(Action)
            .where(Action.account_id == account_id, Action.id == first["action_id"])
            .values(draft_flags=None)
        )
    second = await call(ctx, "propose_message", call_id="c2", **arguments)
    assert second["status"] == "not_drafted"
    assert second["proposal_status"] == "proposal_conflict"
    assert second["action_id"] == first["action_id"]
    assert "reused" not in second


# ------------------------------------------------------------ a newest message with nothing to show
@pytest.mark.parametrize("newest", ["location", "deleted"])
async def test_propose_reply_after_a_location_or_a_deleted_message_drafts_once(
    denv: DraftEnv, newest: str
) -> None:
    # PR #106 review (Neryn): such a newest contact message made every draft
    # stale: two model calls, then always `not_drafted`/`chat_changed`.
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, turn_id = await run_and_turn(denv, account_id)
    await contact_says(denv, "SYNTH-QUESTION")
    if newest == "location":
        await contact_shares_location(denv)
    else:
        await contact_deletes(denv)
    denv.script(owner_output(text="Synthetic reply."))
    result = await call(
        context(denv, account_id, run_id, turn_id), "propose_reply", **reply_args(conversation)
    )
    assert result["status"] == "awaiting_approval"
    assert len(draft_prompts(denv)) == 1
    [card] = await actions_of(denv.env, conversation)
    assert card.id == result["action_id"]
    async with denv.env.factory() as session:
        newest_id = await session.scalar(
            select(func.max(Message.id)).where(Message.conversation_id == conversation)
        )
    assert card.shown_through_message_id == newest_id


# ------------------------------------------------------------ the offer
async def test_propose_tools_are_absent_without_their_flags(denv: DraftEnv) -> None:
    registry = default_registry()
    without = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=frozenset({"tool.get_status"}),
        managed_consent=True,
    )
    assert {"propose_reply", "propose_message"}.isdisjoint(without)
    only_message = registry.for_run(
        "owner_request",
        tier=CapabilityTier.CONTROL_ONLY,
        enabled_flags=frozenset({"tool.propose_message"}),
        managed_consent=True,
    )
    assert set(only_message) == {"propose_message"}
    for contact_run in ("draft_reply", "triage", "summarize"):
        assert (
            registry.for_run(
                contact_run,
                tier=CapabilityTier.CONTROL_ONLY,
                enabled_flags=PROPOSE_FLAGS,
                managed_consent=True,
            )
            == {}
        )
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    refused = await call(
        context(denv, account_id, run_id, turn_id),
        "propose_message",
        flags=frozenset(),
        **message_args(conversation, "SYNTH"),
    )
    assert refused == {"status": "error", "reason": "this tool is not available"}


async def test_propose_tools_are_blocked_stubs_without_managed_consent(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    for name, arguments in (
        ("propose_reply", reply_args(conversation)),
        ("propose_message", message_args(conversation, "SYNTH")),
    ):
        result = await call(ctx, name, managed=False, **arguments)
        assert result["status"] == CHAT_ACCESS_OFF
    assert denv.fake.requests == []
    assert await actions_of(denv.env, conversation) == []


async def test_propose_reply_into_an_ignore_mode_or_bot_chat_is_refused(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv, mode="ignore")
    run_id, turn_id = await run_and_turn(denv, account_id)
    ctx = context(denv, account_id, run_id, turn_id)
    assert await call(ctx, "propose_reply", **reply_args(conversation)) == {
        "status": "not_drafted",
        "conversation_id": conversation,
        "reason": "ignored_chat",
    }
    async with denv.env.factory() as session, session.begin():
        contact = await session.scalar(
            select(Conversation.contact_id).where(Conversation.id == conversation)
        )
        await session.execute(update(Contact).where(Contact.id == contact).values(is_bot=True))
        await session.execute(
            update(Conversation).where(Conversation.id == conversation).values(mode="observe")
        )
    for name, arguments in (
        ("propose_reply", reply_args(conversation)),
        ("propose_message", message_args(conversation, "SYNTH")),
    ):
        result = await call(ctx, name, call_id=name, **arguments)
        assert result["status"] == "not_drafted"
        if name == "propose_message":
            assert result["proposal_status"] == "not_available"
        assert result["reason"] == "bot_chat"
    assert denv.fake.requests == []
    assert await actions_of(denv.env, conversation) == []


@pytest.mark.parametrize("tool", ["propose_reply", "propose_message"])
async def test_a_proposal_with_the_window_closed_is_still_an_approval_card(
    denv: DraftEnv, tool: str
) -> None:
    """Invariant 8 as amended (owner decision 2026-10-30, P2-O20): our 24 h
    estimate does not block an owner-requested draft or dictated message;
    the card (with its warning line) goes to the owner, Telegram decides."""
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    await close_window(denv, conversation)
    denv.script(owner_output(text="Synthetic reply."))
    arguments = (
        reply_args(conversation)
        if tool == "propose_reply"
        else message_args(conversation, "SYNTH-DICTATED")
    )
    result = await call(context(denv, account_id, run_id, turn_id), tool, **arguments)
    assert result["status"] == "awaiting_approval"
    if tool == "propose_message":
        assert result["proposal_status"] == "staged"
    assert result["sendable_until"] is not None
    [card] = await actions_of(denv.env, conversation)
    assert card.state == "awaiting_approval"
    assert card.approval_token is not None
    assert denv.presenter.presented == [card.id]
    never_says_sent(result)


async def test_a_tool_without_its_services_is_not_available(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    for missing in ("drafter", "approval_cards", "actions_config"):
        ctx = context(denv, account_id, run_id, turn_id, **{missing: None})
        name = "propose_message" if missing != "drafter" else "propose_reply"
        arguments = (
            message_args(conversation, "SYNTH")
            if name == "propose_message"
            else reply_args(conversation)
        )
        result = await call(ctx, name, call_id=missing, **arguments)
        if name == "propose_reply":
            assert result == {"status": "error", "reason": "not available"}
        else:
            assert result["status"] == "not_drafted"
            assert result["proposal_status"] == "not_available"
            assert result["action"] is None
    assert await actions_of(denv.env, conversation) == []


async def test_a_refused_proposal_is_not_available_and_logs_its_code(denv: DraftEnv) -> None:
    # PR #106 review (Neryn): the refusal code reaches the executor's log line.
    account_id, conversation = await seed_chat(denv, mode="observe")
    run_id, _ = await run_and_turn(denv, account_id)
    await owner_says(denv, "SYNTH-OWNER-ONLY")
    # The contact's only message is gone: the draft has no base message, and
    # without an owner turn S04.1 refuses it (`base_required`).
    await deliver(denv.env, deleted_messages(message_ids=[1]))
    denv.script(owner_output(text="Synthetic reply."))
    outcome = await propose_reply_tool(
        context(denv, account_id, run_id, None),
        ProposeReplyArgs.model_validate(reply_args(conversation)),
    )
    assert (outcome.status, dict(outcome.data)) == ("error", {"reason": "not available"})
    assert outcome.log_code == "proposal_base_required"
    assert await actions_of(denv.env, conversation) == []


# ------------------------------------------------------------ through the runtime
async def test_tool_steps_are_content_free(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    canary = "SYNTH-DICTATED-CANARY 4410"
    turn = await ask(denv.env, f"Tell them {canary}")  # the owner dictates it (PF-12)
    arguments = await source_message_args(denv, account_id, conversation, canary)
    denv.fake.queue(
        tool_response(("c1", "propose_message", json.dumps(arguments))),
        text_response("A card is waiting for your approval."),
    )
    runtime = OwnerRequestRuntime(
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
    with capture_logs() as logs:
        await runtime.run(turn)
    [card] = await actions_of(denv.env, conversation)
    assert card.text == canary
    assert card.trigger_turn_id == turn
    async with denv.env.factory() as session:
        steps = (await session.scalars(select(AgentRunStep))).all()
        dumped = (
            (await session.execute(text("SELECT row_to_json(s)::text FROM app.agent_run_steps s")))
            .scalars()
            .all()
        )
    assert [(s.tool_name, s.status) for s in steps] == [("propose_message", "awaiting_approval")]
    [line] = [entry for entry in logs if entry["event"] == "agent_tool_call_succeeded"]
    assert line["tool"] == "propose_message"
    assert line["status"] == "awaiting_approval"
    assert not any(
        entry["event"] in {"agent_tool_call_not_run", "agent_tool_call_failed"} for entry in logs
    )
    assert all("SYNTH-DICTATED-CANARY" not in row for row in dumped)
    assert account_id


async def _contact_of(denv: DraftEnv, conversation_id: int) -> int:
    async with denv.env.factory() as session:
        found = await session.scalar(
            select(Conversation.contact_id).where(Conversation.id == conversation_id)
        )
        assert found is not None
        return int(found)


async def test_source_fixture_binds_the_selected_contacts_chat(denv: DraftEnv) -> None:
    account_id, conversation = await seed_chat(denv)
    run_id, turn_id = await run_and_turn(denv, account_id)
    contact = await _contact_of(denv, conversation)
    result = await call(
        context(denv, account_id, run_id, turn_id),
        "propose_message",
        **message_args(None, "SYNTH-BY-CONTACT", contact_id=contact),
    )
    assert result["status"] == "awaiting_approval"
    assert result["proposal_status"] == "staged"
    assert result["conversation_id"] == conversation
    assert result["contact_name"].startswith('<untrusted source="contact_name"')
    await deliver(denv.env, business_message(message_id=999, message_text="SYNTH-LATER"))
    [card] = await actions_of(denv.env, conversation)
    assert card.state == "outdated"  # a newer contact message outdates a dictated card too
