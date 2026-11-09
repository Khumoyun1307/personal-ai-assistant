"""Durable request regressions. Every source and body here is synthetic."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select

from assistant.accounts.models import Account, ControlTurn, PendingInput
from assistant.actions.models import Action
from assistant.actions.request_models import OwnerRequest, OwnerRequestSource
from assistant.actions.requests import (
    AddSources,
    LinkAction,
    RequestConstraints,
    RequestSource,
    SelectRecipient,
    SetConstraints,
    SetQuestion,
    SetState,
    SetTarget,
    SourceSpan,
    expire_requests,
    load_request,
    load_source,
    open_request,
    resolve_continuation,
    update_request,
)
from assistant.conversations.events import ObservedDeletion
from assistant.conversations.models import Contact, Conversation, Message
from assistant.conversations.service import record_deletion
from assistant.privacy.owner_requests import (
    cancel_restored_requests,
    delete_source_turns,
    purge_request_sources,
)
from assistant.privacy.requests import export_owner_requests
from tests.integration.actions_env import add_message, seed_chat
from tests.integration.business_env import Env

pytestmark = pytest.mark.integration


def source(turn_id: int, text: str, *, purpose: str = "literal_body") -> RequestSource:
    return RequestSource.model_validate(
        {
            "span": {
                "turn_id": turn_id,
                "field": "text",
                "start": 0,
                "end": len(text),
                "field_sha256": hashlib.sha256(text.encode()).hexdigest(),
            },
            "purpose": purpose,
        }
    )


async def seed_request(env: Env) -> tuple[int, int, int, int]:
    async with env.factory() as session, session.begin():
        chat = await seed_chat(session, env.clock.now())
        turn = ControlTurn(
            account_id=chat.account_id,
            role="owner",
            text="Synthetic X1 — Oʻzbek 🙂",
            created_at=env.clock.now(),
        )
        session.add(turn)
        await session.flush()
        request = await open_request(
            session,
            account_id=chat.account_id,
            turn_id=turn.id,
            operation="dictate",
            sources=(source(turn.id, "Synthetic X1 — Oʻzbek 🙂"),),
            now=env.clock.now(),
        )
        return chat.account_id, chat.conversation_id, turn.id, request.request_id


async def test_dictation_survives_name_continuation_and_new_session(env: Env) -> None:
    account, conversation, turn, request_id = await seed_request(env)
    original_time = env.clock.now()
    env.clock.advance(minutes=10)
    async with env.factory() as session, session.begin():
        contact = await session.get(OwnerRequest, request_id)
        assert contact is not None
        assert contact.contact_id is None
        chat = await session.get(Conversation, conversation)
        assert chat is not None
        name = ControlTurn(
            account_id=account, role="owner", text="Synthetic Name", created_at=env.clock.now()
        )
        session.add(name)
        await session.flush()
        continued = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=AddSources(sources=(source(name.id, "Synthetic Name", purpose="instruction"),)),
            now=env.clock.now(),
        )
        assert continued.status == "applied"
        result = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=2,
            update=SelectRecipient(
                contact_id=chat.contact_id, conversation_id=conversation, basis="unique_strong"
            ),
            now=env.clock.now(),
        )
        assert result.status == "applied"
        retry = await open_request(
            session,
            account_id=account,
            turn_id=turn,
            operation="dictate",
            sources=(source(turn, "Synthetic X1 — Oʻzbek 🙂"),),
            now=env.clock.now(),
        )
        assert retry.request_id == request_id
        assert retry.version == result.view.version
    async with env.factory() as session:
        request = await load_request(session, account_id=account, request_id=request_id)
        assert request is not None
        assert request.requested_at == original_time
        assert request.expires_at == original_time + timedelta(hours=24)
        assert request.conversation_id == conversation
        body = await load_source(session, account_id=account, source=request.sources[0])
        assert body.status == "validated"
        assert body.text == "Synthetic X1 — Oʻzbek 🙂"
        assert request.sources[0].span.turn_id == turn


async def test_exact_trigger_retry_and_different_task_do_not_replace_sources(env: Env) -> None:
    account, _, turn, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        retry = await open_request(
            session,
            account_id=account,
            turn_id=turn,
            operation="dictate",
            sources=(source(turn, "Synthetic X1 — Oʻzbek 🙂"),),
            now=env.clock.now(),
        )
        assert retry.request_id == request_id
        other = ControlTurn(
            account_id=account, role="owner", text="Another task", created_at=env.clock.now()
        )
        session.add(other)
        await session.flush()
        new = await open_request(
            session,
            account_id=account,
            turn_id=other.id,
            operation="compose",
            sources=(source(other.id, "Another task", purpose="instruction"),),
            now=env.clock.now(),
        )
        assert new.request_id != request_id
        old = await load_request(session, account_id=account, request_id=request_id)
        assert old is not None
        assert old.state == "suspended"
        assert old.sources[0].span.turn_id == turn
        assert old.contact_id is None


async def test_stale_cas_and_foreign_ids_write_nothing(env: Env) -> None:
    account, _, _, request_id = await seed_request(env)
    foreign, _, foreign_turn, _ = await seed_request(env)
    async with env.factory() as session, session.begin():
        applied = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=SetState(state="suspended", reason="topic_switch"),
            now=env.clock.now(),
        )
        assert applied.status == "applied"
        stale = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=SetState(state="cancelled", reason="owner_cancelled"),
            now=env.clock.now(),
        )
        assert stale.status == "stale"
        assert stale.current_version == 2
        missing = await update_request(
            session,
            account_id=foreign,
            request_id=request_id,
            expected_version=2,
            update=SetState(state="cancelled", reason="owner_cancelled"),
            now=env.clock.now(),
        )
        assert missing.status == "unavailable"
        bad = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=2,
            update=AddSources(sources=(source(foreign_turn, "Synthetic X1 — Oʻzbek 🙂"),)),
            now=env.clock.now(),
        )
        assert bad.status == "unavailable"


@pytest.mark.parametrize("physical", [False, True])
@pytest.mark.parametrize("action_state", ["awaiting_approval", "executing"])
async def test_source_loss_invalidates_proof_and_body_preserving_claim(
    env: Env, *, physical: bool, action_state: str
) -> None:
    account, conversation, turn, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        action = Action(
            account_id=account,
            conversation_id=conversation,
            kind="send_message",
            origin="owner_dictated",
            state="awaiting_approval",
            text="Synthetic X1 — Oʻzbek 🙂",
            version=1,
        )
        session.add(action)
        await session.flush()
        # Link while unclaimed; execution begins outside request bookkeeping.
        result = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=LinkAction(action_id=action.id, version=1),
            now=env.clock.now(),
        )
        assert result.status == "applied"
        action.state = action_state
        await session.flush()
        action_id = action.id
    async with env.factory() as session, session.begin():
        if physical:
            await delete_source_turns(
                session, account_id=account, turn_ids=(turn,), now=env.clock.now()
            )
        else:
            await purge_request_sources(
                session, account_id=account, turn_ids=(turn,), now=env.clock.now()
            )
            row = await session.get(ControlTurn, turn)
            assert row is not None
            row.text = None
    async with env.factory() as session:
        action = await session.get(Action, action_id)
        assert action is not None
        assert action.text is None
        assert action.state == ("executing" if action_state == "executing" else "cancelled")
        assert await session.scalar(select(func.count()).select_from(OwnerRequestSource)) == 0
        request = await session.get(OwnerRequest, request_id)
        if physical:
            assert request is None
        else:
            assert request is not None
            assert request.state == "cancelled"


async def test_expiry_uses_original_time(env: Env) -> None:
    account, _, _, request_id = await seed_request(env)
    env.clock.advance(hours=24)
    async with env.factory() as session, session.begin():
        assert await expire_requests(session, account_id=account, now=env.clock.now()) == 1
        view = await load_request(session, account_id=account, request_id=request_id)
        assert view is not None
        assert view.state == "expired"
        assert view.requested_at == env.clock.now() - timedelta(hours=24)


async def test_export_and_account_cascade_are_scoped(env: Env) -> None:
    account, _, _, request_id = await seed_request(env)
    other, _, _, other_request = await seed_request(env)
    async with env.factory() as session, session.begin():
        exported = await export_owner_requests(session, account_id=account)
        assert [row["id"] for row in exported["owner_requests"]] == [request_id]
        assert len(exported["owner_request_sources"]) == 1
        await session.execute(delete(Account).where(Account.id == account))
    async with env.factory() as session:
        assert await load_request(session, account_id=account, request_id=request_id) is None
        assert await load_request(session, account_id=other, request_id=other_request) is not None


async def test_unknown_explicit_reply_never_falls_back_to_focused_request(env: Env) -> None:
    account, _, turn, _ = await seed_request(env)
    async with env.factory() as session:
        result = await resolve_continuation(
            session,
            account_id=account,
            turn_id=turn,
            reply_to_message_id=999999,
            now=env.clock.now(),
        )
        assert result.status == "unavailable"


def test_source_schema_rejects_boolean_ids_and_non_unicode_bounds() -> None:
    with pytest.raises(ValidationError):
        SourceSpan(turn_id=True, field="text", start=0, end=2, field_sha256="a" * 64)
    with pytest.raises(ValidationError):
        SourceSpan(turn_id=1, field="text", start=2, end=2, field_sha256="a" * 64)


@pytest.mark.parametrize(
    "state", ["collecting", "ready", "preparing", "awaiting_approval", "suspended"]
)
async def test_every_unfinished_state_reloads_without_business_effect(env: Env, state: str) -> None:
    account, conversation, _, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        chat = await session.get(Conversation, conversation)
        assert chat is not None
        selected = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=SelectRecipient(
                contact_id=chat.contact_id, conversation_id=conversation, basis="owner_choice"
            ),
            now=env.clock.now(),
        )
        assert selected.status == "applied"
        version = selected.view.version
        if state == "awaiting_approval":
            action = Action(
                account_id=account,
                conversation_id=conversation,
                kind="send_message",
                origin="owner_dictated",
                state="awaiting_approval",
                text="Synthetic body",
                expires_at=env.clock.now() + timedelta(hours=6),
            )
            session.add(action)
            await session.flush()
            linked = await update_request(
                session,
                account_id=account,
                request_id=request_id,
                expected_version=version,
                update=LinkAction(action_id=action.id, version=1),
                now=env.clock.now(),
            )
            assert linked.status == "applied"
            version = linked.view.version
        result = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=version,
            update=SetState.model_validate({"state": state, "reason": None}),
            now=env.clock.now(),
        )
        assert result.status == "applied"
    async with env.factory() as session:
        view = await load_request(session, account_id=account, request_id=request_id)
        assert view is not None
        assert view.state == state
        assert view.requested_at == env.clock.now()
        assert view.sources[0].purpose == "literal_body"
        executing = await session.scalar(
            select(func.count()).select_from(Action).where(Action.state == "executing")
        )
        assert executing == 0


async def test_approval_expiry_follows_action_age_and_never_extends_it(env: Env) -> None:
    account, conversation, _, request_id = await seed_request(env)
    deadline = env.clock.now() + timedelta(hours=2)
    async with env.factory() as session, session.begin():
        action = Action(
            account_id=account,
            conversation_id=conversation,
            kind="send_message",
            origin="owner_dictated",
            state="awaiting_approval",
            text="Synthetic body",
            expires_at=deadline,
        )
        session.add(action)
        await session.flush()
        action_id = action.id
        linked = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=LinkAction(action_id=action_id, version=1),
            now=env.clock.now(),
        )
        assert linked.status == "applied"
        staged = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=2,
            update=SetState(state="awaiting_approval", reason=None),
            now=env.clock.now(),
        )
        assert staged.status == "applied"
        assert staged.view.expires_at == deadline
    env.clock.advance(hours=2)
    async with env.factory() as session, session.begin():
        assert await expire_requests(session, account_id=account, now=env.clock.now()) == 1
    async with env.factory() as session:
        action = await session.get(Action, action_id)
        view = await load_request(session, account_id=account, request_id=request_id)
        assert action is not None
        assert view is not None
        assert action.expires_at == deadline
        assert action.state == "expired"
        assert view.state == "suspended"
        assert view.expires_at == view.requested_at + timedelta(hours=24)


@pytest.mark.parametrize(
    "state", ["awaiting_approval", "executing", "succeeded", "delivery_uncertain"]
)
async def test_cancel_never_resets_a_claim_or_receipt(env: Env, state: str) -> None:
    account, conversation, _, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        action = Action(
            account_id=account,
            conversation_id=conversation,
            kind="send_message",
            origin="owner_dictated",
            state="awaiting_approval",
            text="Synthetic body",
        )
        session.add(action)
        await session.flush()
        action_id = action.id
        linked = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=LinkAction(action_id=action_id, version=1),
            now=env.clock.now(),
        )
        assert linked.status == "applied"
        action.state = state
    async with env.factory() as session, session.begin():
        cancelled = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=2,
            update=SetState(state="cancelled", reason="owner_cancelled"),
            now=env.clock.now(),
        )
        assert cancelled.status == "applied"
    async with env.factory() as session:
        action = await session.get(Action, action_id)
        assert action is not None
        assert action.state == ("cancelled" if state == "awaiting_approval" else state)


async def test_source_hash_role_offsets_and_span_limit_fail_closed(env: Env) -> None:
    account, _, turn_id, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        body = source(turn_id, "Synthetic X1 — Oʻzbek 🙂")
        for invalid in (
            body.model_copy(
                update={"span": body.span.model_copy(update={"field_sha256": "0" * 64})}
            ),
            body.model_copy(update={"span": body.span.model_copy(update={"end": 1000})}),
        ):
            result = await load_source(session, account_id=account, source=invalid)
            assert result.status == "unavailable"
        turn = await session.get(ControlTurn, turn_id)
        assert turn is not None
        turn.role = "assistant"
        # Source validation reloads persisted state; this session disables autoflush.
        await session.flush()
        assert (await load_source(session, account_id=account, source=body)).status == "unavailable"
        suggestion = body.model_copy(update={"purpose": "suggestion"})
        selected = await load_source(session, account_id=account, source=suggestion)
        assert selected.status == "validated"
        assert selected.untrusted
        turn.role = "owner"
        turn.third_party = True
        await session.flush()
        assert (await load_source(session, account_id=account, source=body)).status == "unavailable"
        turn.third_party = False
        await session.flush()
        too_many = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=AddSources(sources=(body,) * 64),
            now=env.clock.now(),
        )
        assert too_many.status == "unavailable"
        assert too_many.code == "too_many_sources"


async def test_question_is_account_local_and_unknown_target_cannot_be_selected(env: Env) -> None:
    account, _, _, request_id = await seed_request(env)
    other, _, _, _ = await seed_request(env)
    async with env.factory() as session, session.begin():
        question = PendingInput(
            account_id=other,
            kind="owner_request",
            ref_id=request_id,
            expires_at=env.clock.now() + timedelta(minutes=30),
        )
        session.add(question)
        await session.flush()
        result = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=SetQuestion(slot="recipient", input_id=question.id),
            now=env.clock.now(),
        )
        assert result.status == "unavailable"
        target = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=SetTarget(message_id=999999),
            now=env.clock.now(),
        )
        assert target.status == "unavailable"


def test_constraints_never_accept_prose_or_invalid_word_bounds() -> None:
    with pytest.raises(ValidationError):
        RequestConstraints(min_words=20, max_words=10, reply_language=None)
    with pytest.raises(ValidationError):
        RequestConstraints.model_validate({"instruction": "Synthetic prose"})


async def test_constraints_are_typed_and_foreign_recipient_is_unavailable(env: Env) -> None:
    account, conversation, _, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        result = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=SetConstraints(
                constraints=RequestConstraints(min_words=10, max_words=20, reply_language="uz-Latn")
            ),
            now=env.clock.now(),
        )
        assert result.status == "applied"
        chat = await session.get(Conversation, conversation)
        assert chat is not None
        selected = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=2,
            update=SelectRecipient(
                contact_id=chat.contact_id, conversation_id=conversation, basis="owner_choice"
            ),
            now=env.clock.now(),
        )
        assert selected.status == "applied"
        # A foreign conversation is indistinguishable from a missing reference.
        rejected = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=3,
            update=SelectRecipient(
                contact_id=chat.contact_id, conversation_id=999999, basis="owner_choice"
            ),
            now=env.clock.now(),
        )
        assert rejected.status == "unavailable"


async def test_restore_cancels_request_attempt_without_resuming_it(env: Env) -> None:
    account, _, _, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        question = PendingInput(
            account_id=account,
            kind="owner_request",
            ref_id=request_id,
            expires_at=env.clock.now() + timedelta(minutes=30),
        )
        session.add(question)
        await session.flush()
        question_id = question.id
        result = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=SetQuestion(slot="recipient", input_id=question_id),
            now=env.clock.now(),
        )
        assert result.status == "applied"
        await cancel_restored_requests(session, account_id=account, now=env.clock.now())
    async with env.factory() as session:
        row = await session.get(OwnerRequest, request_id)
        assert row is not None
        assert row.state == "cancelled"
        assert row.reason == "restored_from_backup"
        assert row.focused is False
        assert await session.get(PendingInput, question_id) is None


@pytest.mark.parametrize("remove_account", [False, True])
async def test_physical_action_deletion_clears_both_link_columns(
    env: Env, *, remove_account: bool
) -> None:
    account, conversation, _, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        action = Action(
            account_id=account,
            conversation_id=conversation,
            kind="send_message",
            origin="owner_dictated",
            state="awaiting_approval",
            text="Synthetic body",
        )
        session.add(action)
        await session.flush()
        result = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=LinkAction(action_id=action.id, version=1),
            now=env.clock.now(),
        )
        assert result.status == "applied"
        if remove_account:
            await session.execute(delete(Account).where(Account.id == account))
        else:
            await session.execute(delete(Action).where(Action.account_id == account))
    async with env.factory() as session:
        row = await session.get(OwnerRequest, request_id)
        if remove_account:
            assert row is None
        else:
            assert row is not None
            assert row.action_id is None
            assert row.action_version is None


async def test_every_recipient_target_and_action_edge_is_account_local(env: Env) -> None:
    account, _, _, request_id = await seed_request(env)
    foreign, foreign_conversation, _, _ = await seed_request(env)
    async with env.factory() as session, session.begin():
        chat = await session.get(Conversation, foreign_conversation)
        assert chat is not None
        target_id = await session.scalar(select(Message.id).where(Message.account_id == foreign))
        assert target_id is not None
        action = Action(
            account_id=foreign,
            conversation_id=foreign_conversation,
            kind="send_message",
            origin="owner_dictated",
            state="awaiting_approval",
            text="Synthetic foreign body",
        )
        session.add(action)
        await session.flush()
        for change in (
            SelectRecipient(
                contact_id=chat.contact_id,
                conversation_id=foreign_conversation,
                basis="owner_choice",
            ),
            SetTarget(message_id=target_id),
            LinkAction(action_id=action.id, version=1),
        ):
            result = await update_request(
                session,
                account_id=account,
                request_id=request_id,
                expected_version=1,
                update=change,
                now=env.clock.now(),
            )
            assert result.status == "unavailable"
        view = await load_request(session, account_id=account, request_id=request_id)
        assert view is not None
        assert view.version == 1
        assert view.conversation_id is None
        assert view.action_id is None


async def test_target_source_loss_never_selects_a_newer_message(env: Env) -> None:
    account, conversation, _, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        chat = await session.get(Conversation, conversation)
        assert chat is not None
        target_id = await session.scalar(select(Message.id).where(Message.account_id == account))
        assert target_id is not None
        selected = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=SelectRecipient(
                contact_id=chat.contact_id, conversation_id=conversation, basis="owner_choice"
            ),
            now=env.clock.now(),
        )
        assert selected.status == "applied"
        targeted = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=2,
            update=SetTarget(message_id=target_id),
            now=env.clock.now(),
        )
        assert targeted.status == "applied"
        await purge_request_sources(
            session, account_id=account, message_ids=(target_id,), now=env.clock.now(), locked=True
        )
    async with env.factory() as session:
        view = await load_request(session, account_id=account, request_id=request_id)
        assert view is not None
        assert view.state == "cancelled"
        assert view.target_message_id is None


@pytest.mark.parametrize("action_state", ["awaiting_approval", "executing"])
async def test_telegram_target_deletion_invalidates_request_and_body_preserving_claim(
    env: Env, action_state: str
) -> None:
    account, conversation, turn_id, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        chat = await session.get(Conversation, conversation)
        assert chat is not None
        target = await session.scalar(select(Message).where(Message.account_id == account))
        assert target is not None
        assert target.telegram_message_id is not None
        target_id = target.id
        event = ObservedDeletion(chat.business_chat_id, (target.telegram_message_id,))
        selected = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=SelectRecipient(
                contact_id=chat.contact_id, conversation_id=conversation, basis="owner_choice"
            ),
            now=env.clock.now(),
        )
        assert selected.status == "applied"
        targeted = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=selected.view.version,
            update=SetTarget(message_id=target_id),
            now=env.clock.now(),
        )
        assert targeted.status == "applied"
        action = Action(
            account_id=account,
            conversation_id=conversation,
            kind="send_message",
            origin="owner_dictated",
            state="awaiting_approval",
            text="Synthetic derived reply",
            base_message_id=target_id,
        )
        session.add(action)
        await session.flush()
        action_id = action.id
        linked = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=targeted.view.version,
            update=LinkAction(action_id=action.id, version=action.version),
            now=env.clock.now(),
        )
        assert linked.status == "applied"
        question = PendingInput(
            account_id=account,
            kind="owner_request",
            ref_id=request_id,
            expires_at=env.clock.now() + timedelta(minutes=30),
        )
        session.add(question)
        await session.flush()
        asked = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=linked.view.version,
            update=SetQuestion(slot="constraints", input_id=question.id),
            now=env.clock.now(),
        )
        assert asked.status == "applied"
        action.state = action_state
        newer_id = await add_message(
            session, account, conversation, direction="contact", sent_at=env.clock.now()
        )
    async with env.factory() as session, session.begin():
        outcome = await record_deletion(session, account_id=account, event=event, clock=env.clock)
        assert outcome.purged_message_ids == (target_id,)
    async with env.factory() as session:
        view = await load_request(session, account_id=account, request_id=request_id)
        assert view is not None
        assert view.state == "cancelled"
        assert view.target_message_id is None
        assert view.action_id is None
        assert view.action_version is None
        assert view.sources == ()
        request = await session.get(OwnerRequest, request_id)
        assert request is not None
        assert request.question_input_id is None
        assert request.focused is False
        questions = await session.scalar(
            select(func.count()).select_from(PendingInput).where(PendingInput.ref_id == request_id)
        )
        assert questions == 0
        action = await session.get(Action, action_id)
        assert action is not None
        assert action.text is None
        expected_state = "cancelled" if action_state == "awaiting_approval" else action_state
        assert action.state == expected_state
        target = await session.get(Message, target_id)
        assert target is not None
        assert target.text is None
        assert target.deleted_at == env.clock.now()
        newer = await session.get(Message, newer_id)
        assert newer is not None
        assert newer.text == "Synthetic message"
        continuation = await resolve_continuation(
            session,
            account_id=account,
            turn_id=turn_id,
            reply_to_message_id=None,
            now=env.clock.now(),
        )
        assert continuation.status == "none"


@pytest.mark.parametrize("action_first", [False, True])
@pytest.mark.parametrize("same_recipient", [False, True])
async def test_recipient_and_linked_action_must_match_in_both_update_orders(
    env: Env, *, action_first: bool, same_recipient: bool
) -> None:
    account, conversation, _, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        chat = await session.get(Conversation, conversation)
        assert chat is not None
        contact = Contact(account_id=account, telegram_user_id=chat.business_chat_id + 1)
        session.add(contact)
        await session.flush()
        other = Conversation(
            account_id=account, contact_id=contact.id, business_chat_id=contact.telegram_user_id
        )
        session.add(other)
        action = Action(
            account_id=account,
            conversation_id=conversation,
            kind="send_message",
            origin="owner_dictated",
            state="awaiting_approval",
            text="Synthetic original draft",
        )
        session.add(action)
        await session.flush()
        recipient = chat if same_recipient else other
        selection = SelectRecipient(
            contact_id=recipient.contact_id, conversation_id=recipient.id, basis="owner_choice"
        )
        linkage = LinkAction(action_id=action.id, version=action.version)
        first, second = (linkage, selection) if action_first else (selection, linkage)
        applied = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=first,
            now=env.clock.now(),
        )
        assert applied.status == "applied"
        result = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=applied.view.version,
            update=second,
            now=env.clock.now(),
        )
        if same_recipient:
            assert result.status == "applied"
            assert result.view.conversation_id == conversation
            assert result.view.action_id == action.id
        else:
            assert result.status == "unavailable"
            assert result.code == "invalid_reference"
            unchanged = await load_request(session, account_id=account, request_id=request_id)
            assert unchanged == applied.view
        await session.refresh(action)
        assert action.conversation_id == conversation
        assert action.state == "awaiting_approval"
        assert action.text == "Synthetic original draft"


@pytest.mark.parametrize("state", ["collecting", "ready", "preparing"])
@pytest.mark.parametrize("after_deadline", [False, True])
async def test_leaving_approval_restores_and_enforces_original_request_ttl(
    env: Env, state: str, *, after_deadline: bool
) -> None:
    account, conversation, turn_id, request_id = await seed_request(env)
    original_deadline = env.clock.now() + timedelta(hours=24)
    env.clock.advance(hours=23)
    action_deadline = env.clock.now() + timedelta(hours=6)
    async with env.factory() as session, session.begin():
        chat = await session.get(Conversation, conversation)
        assert chat is not None
        selected = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=SelectRecipient(
                contact_id=chat.contact_id, conversation_id=conversation, basis="owner_choice"
            ),
            now=env.clock.now(),
        )
        assert selected.status == "applied"
        action = Action(
            account_id=account,
            conversation_id=conversation,
            kind="send_message",
            origin="owner_dictated",
            state="awaiting_approval",
            text="Synthetic body",
            expires_at=action_deadline,
        )
        session.add(action)
        await session.flush()
        action_id = action.id
        linked = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=selected.view.version,
            update=LinkAction(action_id=action.id, version=action.version),
            now=env.clock.now(),
        )
        assert linked.status == "applied"
        staged = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=linked.view.version,
            update=SetState(state="awaiting_approval", reason=None),
            now=env.clock.now(),
        )
        assert staged.status == "applied"
        assert staged.view.expires_at == action_deadline
    env.clock.advance(hours=2 if after_deadline else 0, minutes=30)
    async with env.factory() as session, session.begin():
        resumed = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=staged.view.version,
            update=SetState.model_validate({"state": state, "reason": None}),
            now=env.clock.now(),
        )
        if after_deadline:
            assert resumed.status == "unavailable"
            assert resumed.code == "not_available"
            unchanged = await load_request(session, account_id=account, request_id=request_id)
            assert unchanged == staged.view
        else:
            assert resumed.status == "applied"
            assert resumed.view.expires_at == original_deadline
            assert resumed.view.requested_at == original_deadline - timedelta(hours=24)
    if after_deadline:
        return
    env.clock.advance(hours=1)
    async with env.factory() as session, session.begin():
        assert resumed.status == "applied"
        continued = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=resumed.view.version,
            update=SetConstraints(
                constraints=RequestConstraints(min_words=None, max_words=20, reply_language=None)
            ),
            now=env.clock.now(),
        )
        assert continued.status == "unavailable"
        assert continued.code == "not_available"
        continuation = await resolve_continuation(
            session,
            account_id=account,
            turn_id=turn_id,
            reply_to_message_id=None,
            now=env.clock.now(),
        )
        assert continuation.status == "none"
        assert await expire_requests(session, account_id=account, now=env.clock.now()) == 1
    async with env.factory() as session:
        view = await load_request(session, account_id=account, request_id=request_id)
        assert view is not None
        assert view.state == "expired"
        assert view.expires_at == original_deadline
        action = await session.get(Action, action_id)
        assert action is not None
        assert action.expires_at == action_deadline
