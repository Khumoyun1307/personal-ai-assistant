"""PF-12: a target the owner chose survives the card's Edit revision.

Before, every revision re-anchored the draft on the chat's newest contact
message (`approvals.py`, revise apply), so a reply to an earlier message
the owner named, or a standalone new message, moved. Through the
production wiring (`_wire_m04`), the real inbox and the `replies.revise`
job; only the clock, the transport and the model are fake. Synthetic text.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select, update

from assistant.accounts.models import AuditLogEntry, ControlTurn
from assistant.actions import lifecycle
from assistant.actions.authorship import VersionAuthorship, current_authorship, is_ai_generated
from assistant.actions.request_models import OwnerRequest, OwnerRequestSource
from assistant.actions.states import StateReason
from assistant.agent.tools.drafts import ReviseDraftArgs, revise_draft_tool
from assistant.agent.tools.registry import OwnerTurn, ToolContext, ToolServices
from assistant.conversations.models import Message
from tests.integration.actions_env import seed_run
from tests.integration.agent_env import ask
from tests.integration.business_env import business_message, deliver
from tests.integration.p2_factories import seed_action, seed_conversation
from tests.integration.test_approvals import start_edit
from tests.integration.test_reply_tools import call, source_message_args
from tests.scenarios.p2_drive import Scenario, m04_environment, revision_result, run_queued

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("tainted", [False, True])
async def test_proposal_and_literal_revision_share_durable_call_identity(
    s: Scenario, tainted: bool
) -> None:
    async with s.env.factory() as session, session.begin():
        run_id = await seed_run(session, s.account_id)
    turn_id = await ask(s.env, "SYNTH copy the selected words")
    ctx = ToolContext(
        account_id=s.account_id,
        run_id=run_id,
        run_type="owner_request",
        tainted=tainted,
        services=ToolServices(
            s.env.factory,
            s.env.clock,
            None,
            approval_cards=s.wiring.approval_cards,
            actions_config=s.wiring.actions_config,
        ),
        turn_id=turn_id,
        owner_turn=OwnerTurn(turn_id, "SYNTH copy the selected words", False, False, False),
        managed_ok=True,
    )
    proposal_args = await source_message_args(ctx, s.account_id, s.conversation_id, "SYNTH X1!")
    flags = frozenset({"tool.propose_message", "tool.revise_draft"})
    staged = await call(ctx, "propose_message", flags=flags, **proposal_args)
    assert staged["proposal_status"] == "staged"
    original = await s.action(staged["action_id"])
    revision_args = {
        "conversation_id": None,
        "action_id": original.id,
        "min_words": None,
        "max_words": None,
        "literal": {
            "request_id": proposal_args["request_id"],
            "request_version": staged["request_version"],
            "expected_version": original.version,
            "edit": "literal_append",
            "source_ids": proposal_args["source_ids"],
        },
    }
    # call() constructs a fresh executor each time: no in-memory digest can help.
    conflict = await call(ctx, "revise_draft", flags=flags, **revision_args)
    assert conflict["proposal_status"] == "idempotency_conflict"
    assert conflict["effect_version"] is None
    assert conflict["requested_change_applied"] is False
    unchanged = await s.action(original.id)
    assert unchanged.version == original.version
    assert unchanged.text == original.text
    assert unchanged.approval_token == original.approval_token
    assert "action_revision_requested" not in await s.events(original.id)

    revised = await call(ctx, "revise_draft", call_id="c2", flags=flags, **revision_args)
    assert revised["proposal_status"] == "revised"
    conflict = await call(ctx, "propose_message", call_id="c2", flags=flags, **proposal_args)
    assert conflict["proposal_status"] == "idempotency_conflict"
    assert conflict["effect_version"] is None
    assert conflict["requested_change_applied"] is False
    replay = await call(ctx, "revise_draft", call_id="c2", flags=flags, **revision_args)
    assert replay["proposal_status"] == "reused_exact"
    assert replay["effect_version"] == original.version + 1
    proposal_replay = await call(ctx, "propose_message", flags=flags, **proposal_args)
    assert proposal_replay["proposal_status"] == "reused_exact"
    assert proposal_replay["effect_version"] == original.version
    assert proposal_replay["action"]["version"] == original.version + 1
    async with s.env.factory() as session:
        effects = list(
            await session.scalars(
                select(AuditLogEntry).where(
                    AuditLogEntry.account_id == s.account_id,
                    AuditLogEntry.event == "tool_effect_recorded",
                    AuditLogEntry.subject_id == original.id,
                )
            )
        )
    assert len(effects) == 2
    assert {effect.details["tool_call_id"] for effect in effects} == {"c1", "c2"}
    final = await s.action(original.id)
    assert final.version == original.version + 1
    assert final.text == original.text + "SYNTH X1!"


@pytest.mark.parametrize("fresh_run", [False, True])
async def test_literal_revision_retry_conflicts_when_another_card_is_selected(
    s: Scenario, fresh_run: bool
) -> None:
    first = await s.seed(text="SYNTH first card")
    async with s.env.factory() as session, session.begin():
        chat = await seed_conversation(session, account_id=s.account_id)
        second = await seed_action(
            session,
            account_id=s.account_id,
            conversation_id=chat.id,
            state="awaiting_approval",
            version=first.version,
            text="SYNTH second card",
            approval_token="QAseed5678",
            expires_at=first.expires_at,
            created_at=s.env.clock.now(),
            updated_at=s.env.clock.now(),
        )
        run_id = await seed_run(session, s.account_id)
        retry_run_id = await seed_run(session, s.account_id) if fresh_run else run_id
    turn_id = await ask(s.env, "SYNTH append the selected words")
    ctx = ToolContext(
        account_id=s.account_id,
        run_id=run_id,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(
            s.env.factory, s.env.clock, None, approval_cards=s.wiring.approval_cards
        ),
        turn_id=turn_id,
        owner_turn=OwnerTurn(turn_id, "SYNTH append the selected words", False, False, False),
        managed_ok=True,
    )
    arguments: list[dict[str, Any]] = []
    for card in (first, second):
        selected = await source_message_args(ctx, s.account_id, card.conversation_id, " X1 x/h!")
        async with s.env.factory() as session, session.begin():
            request = await session.get(OwnerRequest, selected["request_id"])
            assert request is not None
            request.operation = "revise"
            request.action_id = card.id
            request.action_version = card.version
        arguments.append(
            {
                "conversation_id": None,
                "action_id": card.id,
                "min_words": None,
                "max_words": None,
                "literal": {
                    "request_id": selected["request_id"],
                    "request_version": selected["request_version"],
                    "expected_version": card.version,
                    "edit": "literal_append",
                    "source_ids": selected["source_ids"],
                },
            }
        )
    # Both requests remain eligible; changing focus must not hide a prior effect.
    async with s.env.factory() as session, session.begin():
        for args in arguments:
            request = await session.get(OwnerRequest, args["literal"]["request_id"])
            assert request is not None
            request.state = "ready"
            args["literal"]["request_version"] = request.version
    flags = frozenset({"tool.revise_draft"})
    applied = await call(ctx, "revise_draft", flags=flags, **arguments[0])
    assert applied["proposal_status"] == "revised"
    retry_ctx = replace(ctx, run_id=retry_run_id)
    retry_call_id = "retry" if fresh_run else "c1"
    replay = await call(
        retry_ctx, "revise_draft", call_id=retry_call_id, flags=flags, **arguments[0]
    )
    assert replay["proposal_status"] == "reused_exact"
    assert replay["effect_version"] == first.version + 1
    conflict = await call(
        retry_ctx, "revise_draft", call_id=retry_call_id, flags=flags, **arguments[1]
    )
    assert conflict["status"] == "not_drafted"
    assert conflict["proposal_status"] == (
        "proposal_conflict" if fresh_run else "idempotency_conflict"
    )
    assert conflict["action"]["action_id"] == first.id
    assert conflict["effect_version"] is None
    assert conflict["requested_change_applied"] is False
    # Scenario.action scopes reads to its conversation; this card is in the other chat.
    untouched = await replace(s, conversation_id=second.conversation_id).action(second.id)
    assert untouched.text == second.text
    assert untouched.version == second.version
    assert untouched.approval_token == second.approval_token
    assert "action_revision_requested" not in await s.events(second.id)


@pytest.mark.parametrize("state", ["cancelled", "revising"])
async def test_literal_revision_unavailable_states_use_c2_results(s: Scenario, state: str) -> None:
    card = await s.seed(state=state, text="SYNTH retained card")
    turn_id = await ask(s.env, "SYNTH add X1")
    ctx = ToolContext(
        account_id=s.account_id,
        run_id=1,
        run_type="owner_request",
        tainted=False,
        services=ToolServices(
            s.env.factory, s.env.clock, None, approval_cards=s.wiring.approval_cards
        ),
        turn_id=turn_id,
        owner_turn=OwnerTurn(turn_id, "SYNTH add X1", False, False, False),
        managed_ok=True,
    )
    selected = await source_message_args(ctx, s.account_id, card.conversation_id, " X1")
    async with s.env.factory() as session, session.begin():
        request = await session.get(OwnerRequest, selected["request_id"])
        assert request is not None
        request.operation = "revise"
        request.action_id = card.id
        request.action_version = card.version
    result = await call(
        ctx,
        "revise_draft",
        flags=frozenset({"tool.revise_draft"}),
        conversation_id=None,
        action_id=card.id,
        min_words=None,
        max_words=None,
        literal={
            "request_id": selected["request_id"],
            "request_version": selected["request_version"],
            "expected_version": card.version,
            "edit": "literal_append",
            "source_ids": selected["source_ids"],
        },
    )
    assert result["status"] == "not_drafted"
    assert result["proposal_status"] == "not_available"
    assert result["action"]["action_id"] == card.id
    assert result["action"]["state"] == state
    assert result["effect_version"] is None
    assert result["requested_change_applied"] is False
    untouched = await s.action(card.id)
    assert untouched.text == card.text
    assert untouched.version == card.version
    assert untouched.approval_token == card.approval_token
    assert "action_revision_requested" not in await s.events(card.id)


@pytest.mark.parametrize("edit", ["literal_append", "literal_replace"])
@pytest.mark.parametrize("source_lost", [False, True])
@pytest.mark.parametrize("owner_base", [False, True])
async def test_literal_revision_copies_sources_without_normalizing_or_adding_spaces(
    s: Scenario,
    edit: str,
    source_lost: bool,
    owner_base: bool,
) -> None:
    row = await s.seed(
        text="SYNTH retained body  ",
        origin="owner_dictated" if owner_base else "agent_proposal",
        authorship=VersionAuthorship(
            version=1,
            kind="owner_dictated" if owner_base else "assistant_written",
            source_ids=[],
            parent_version=None,
            retains_generated_text=not owner_base,
        ).model_dump(mode="json"),
    )
    addition = "\n  X1 x/h, O‘g‘il e\u0301! 👍  "
    turn_id = await ask(s.env, "write it")
    services = ToolServices(
        s.env.factory,
        s.env.clock,
        None,
        approval_cards=s.wiring.approval_cards,
    )
    ctx = ToolContext(
        account_id=s.account_id,
        run_id=1,
        run_type="owner_request",
        tainted=False,
        services=services,
        call_id="literal-revision",
        turn_id=turn_id,
        owner_turn=OwnerTurn(turn_id, "write it", False, False, False),
    )
    selected = await source_message_args(ctx, s.account_id, s.conversation_id, addition)
    async with s.env.factory() as session, session.begin():
        request = await session.get(OwnerRequest, selected["request_id"])
        assert request is not None
        request.operation = "revise"
        request.action_id = row.id
        request.action_version = row.version
        if source_lost:
            source = await session.get(OwnerRequestSource, selected["source_ids"][0])
            assert source is not None
            await session.execute(
                update(ControlTurn).where(ControlTurn.id == source.turn_id).values(text=None)
            )
    args = ReviseDraftArgs.model_validate(
        {
            "conversation_id": None,
            "action_id": row.id,
            "min_words": None,
            "max_words": None,
            "literal": {
                "request_id": selected["request_id"],
                "request_version": selected["request_version"],
                "expected_version": row.version,
                "edit": edit,
                "source_ids": selected["source_ids"],
            },
        }
    )
    outcome = await revise_draft_tool(ctx, args)
    if source_lost:
        assert outcome.status == "not_drafted"
        assert outcome.data["proposal_status"] == "invalid_arguments"
        assert outcome.data["action"] is None
        assert outcome.data["effect_version"] is None
        assert outcome.data["requested_change_applied"] is False
        unchanged = await s.action(row.id)
        assert unchanged.version == row.version
        assert unchanged.text == row.text
        assert unchanged.approval_token == row.approval_token
        assert unchanged.authorship == row.authorship
        assert "action_revision_requested" not in await s.events(row.id)
        return
    assert outcome.status == "revised"
    revised = await s.action(row.id)
    assert revised.version == row.version + 1
    assert revised.text == (row.text + addition if edit == "literal_append" else addition)
    assert revised.approval_token != row.approval_token
    assert revised.origin == row.origin
    proof = current_authorship(revised.authorship, version=revised.version)
    assert proof is not None
    assert proof.kind == "owner_literal_edit"
    assert proof.source_ids == selected["source_ids"]
    generated = edit == "literal_append" and not owner_base
    assert is_ai_generated(revised.authorship, version=revised.version) is generated
    assert revised.expires_at == row.expires_at
    assert revised.approved_text_hash is None
    async with s.env.factory() as session, session.begin():
        stale = await lifecycle.approve(
            session,
            account_id=s.account_id,
            token=row.approval_token or "",
            now=s.env.clock.now(),
        )
        assert stale.status is lifecycle.ApproveStatus.NOT_ACTIVE
        cancelled = await lifecycle.cancel_pending_ai_generated(
            session,
            account_id=s.account_id,
            reason=StateReason.CONSENT_REVOKED,
            now=s.env.clock.now(),
        )
    assert cancelled == ([row.id] if generated else [])
    assert s.fake.requests == []


@pytest.fixture
async def s(migrated_url: str) -> AsyncIterator[Scenario]:
    async with m04_environment(migrated_url) as scenario:
        yield scenario


async def contact_ids(s: Scenario) -> list[int]:
    async with s.env.factory() as session:
        return list(
            await session.scalars(
                select(Message.id)
                .where(Message.conversation_id == s.conversation_id, Message.direction == "contact")
                .order_by(Message.id)
            )
        )


def owner_flags(target: str, mode: str = "reply") -> dict[str, Any]:
    return {
        "needs_reply": True,
        "confidence": "high",
        "sensitive_topic": False,
        "makes_commitment": False,
        "should_escalate": None,
        "uses_only_permitted_information": None,
        "language": "en",
        "source": "draft_owner",
        "mode": mode,
        "target": target,
    }


@pytest.mark.parametrize(
    ("target", "mode", "base", "kept"),
    [
        ("answered", "reply", "earlier", "earlier"),
        ("none", "new_message", None, None),
        ("newest", "reply", "earlier", "newest"),  # today's default: re-anchored
    ],
)
async def test_a_revision_keeps_an_explicit_target_and_re_anchors_the_default(
    s: Scenario, target: str, mode: str, base: str | None, kept: str | None
) -> None:
    await deliver(
        s.env, business_message(message_id=4401, message_text="SYNTH-LATER", date=s.date + 1)
    )
    earlier, *_, newest = await contact_ids(s)
    named = {"earlier": earlier, "newest": newest, None: None}
    row = await s.seed(
        base_message_id=named[base],
        shown_through_message_id=newest,
        draft_flags=owner_flags(target, mode),
        trigger_turn_id=None,
    )
    s.fake.parsed.append(revision_result(text="Synthetic shorter reply."))
    await start_edit(s, row, "Synthetic: shorter please.")
    assert await run_queued(s, "replies.revise") == 1
    revised = await s.action(row.id)
    assert (revised.state, revised.version, revised.text) == (
        "awaiting_approval",
        2,
        "Synthetic shorter reply.",
    )
    assert revised.base_message_id == named[kept]
    assert revised.draft_flags is not None
    # PF-13: revisions run `draft_owner`, whose flags always name the target.
    assert revised.draft_flags["target"] == target


@pytest.mark.parametrize(("base", "kept"), [("earlier", "earlier"), (None, None)])
async def test_a_dictated_card_keeps_the_message_it_answers_through_a_revision(
    s: Scenario, base: str | None, kept: str | None
) -> None:
    """PF-F: `propose_message` may name the message the owner's words answer;
    a revision keeps it like any explicit target (a dictated card carries no
    draft flags, R8, so its base message is the record)."""
    await deliver(
        s.env, business_message(message_id=4402, message_text="SYNTH-LATER", date=s.date + 1)
    )
    earlier, *_, newest = await contact_ids(s)
    named = {"earlier": earlier, None: None}
    row = await s.seed(
        origin="owner_dictated",
        base_message_id=named[base],
        shown_through_message_id=newest,
        draft_flags=None,
        trigger_turn_id=None,
    )
    s.fake.parsed.append(revision_result(text="Synthetic shorter reply."))
    await start_edit(s, row, "Synthetic: shorter please.")
    assert await run_queued(s, "replies.revise") == 1
    revised = await s.action(row.id)
    assert (revised.state, revised.version) == ("awaiting_approval", 2)
    assert revised.base_message_id == named[kept]
    # H9 / R2-03: an AI rewrite is generated even when v1 was dictated.
    assert is_ai_generated(revised.authorship, version=revised.version)
    proof = current_authorship(revised.authorship, version=revised.version)
    assert proof is not None
    assert proof.kind == "assistant_written"
    assert proof.parent_version == 1
    assert revised.draft_flags is not None
    if kept is not None:
        [draft] = [r for r in s.fake.requests if r.purpose == "draft_reply"]
        shown = "\n".join(getattr(item, "text", "") for item in draft.items)
        assert f"[msg:{earlier}]." in shown


@pytest.mark.parametrize("literal", [False, True])
async def test_revision_authorship_token_expiry_and_consent_cancellation_follow_current_body(
    s: Scenario, literal: bool
) -> None:
    original_proof = VersionAuthorship(
        version=1,
        kind="owner_dictated",
        source_ids=[],
        parent_version=None,
        retains_generated_text=False,
    )
    row = await s.seed(
        origin="owner_dictated",
        authorship=original_proof.model_dump(mode="json"),
        approved_text_hash="synthetic-prior-approval",
    )
    s.fake.parsed.append(
        revision_result(
            text="Synthetic AI rewrite.",
            revision_kind="replacement" if literal else "instruction",
        )
    )
    await start_edit(s, row, "Synthetic exact owner replacement.")
    assert await run_queued(s, "replies.revise") == 1
    revised = await s.action(row.id)
    proof = current_authorship(revised.authorship, version=2)
    assert proof is not None
    assert proof.kind == ("owner_literal_edit" if literal else "assistant_written")
    assert is_ai_generated(revised.authorship, version=2) is not literal
    assert revised.text == (
        "Synthetic exact owner replacement." if literal else "Synthetic AI rewrite."
    )
    assert revised.origin == "owner_dictated"
    assert revised.approval_token != row.approval_token
    assert revised.approved_text_hash is None
    assert revised.expires_at == row.expires_at
    async with s.env.factory() as session, session.begin():
        stale = await lifecycle.approve(
            session,
            account_id=s.account_id,
            token=row.approval_token or "",
            now=s.env.clock.now(),
        )
        assert stale.status is lifecycle.ApproveStatus.NOT_ACTIVE
        changed = await lifecycle.cancel_pending_ai_generated(
            session,
            account_id=s.account_id,
            reason=StateReason.CONSENT_REVOKED,
            now=s.env.clock.now(),
        )
    assert changed == ([] if literal else [row.id])
    assert (await s.action(row.id)).state == ("awaiting_approval" if literal else "cancelled")
