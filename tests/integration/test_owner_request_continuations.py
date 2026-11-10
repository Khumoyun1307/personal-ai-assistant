"""H2 continuations: synthetic owner words survive routing and restart."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from assistant.accounts.models import AuditLogEntry, ControlTurn, PendingInput
from assistant.actions.models import Action
from assistant.actions.request_models import OwnerRequest
from assistant.actions.requests import (
    LinkAction,
    SetState,
    SourceUnavailable,
    load_request,
    load_source,
    open_request,
    resolve_continuation,
    update_request,
)
from assistant.agent.persistence import load_owner_continuation
from assistant.conversations.models import Contact, Conversation
from assistant.privacy.deletion import delete_everything
from assistant.privacy.owner_requests import purge_request_sources
from assistant.privacy.requests import export_owner_requests
from tests.integration.business_env import Env
from tests.integration.test_owner_requests import seed_request, source

pytestmark = pytest.mark.integration


async def answer(env: Env, account_id: int, *, reply: int | None = None) -> int:
    async with env.factory() as session, session.begin():
        turn = ControlTurn(
            account_id=account_id,
            role="owner",
            text="Kamola Ziyo",
            telegram_message_id=902,
            reply_to_telegram_message_id=reply,
            created_at=env.clock.now(),
        )
        session.add(turn)
        await session.flush()
        return turn.id


async def test_i01_name_answer_reloads_original_dictation_after_restart(env: Env) -> None:
    account, _, original, request_id = await seed_request(env)
    turn_id = await answer(env, account)
    async with env.factory() as session, session.begin():
        turn = await session.get(ControlTurn, turn_id)
        assert turn is not None
        loaded = await load_owner_continuation(
            session,
            account_id=account,
            turn=turn,
            now=env.clock.now(),
            managed_allowed=True,
        )
        assert loaded.selection.status == "selected"
        assert loaded.request is not None
        assert loaded.request.request_id == request_id
        assert loaded.request.missing_slot == "recipient"
        assert loaded.request.sources[0].span.turn_id == original
        assert loaded.sources[0].text == "Synthetic X1 — Oʻzbek 🙂"
        version = loaded.request.version
    # A new session is the restart boundary. UI recovery does not rewrite
    # source spans, advance request/action versions or extend the task TTL.
    async with env.factory() as session:
        request = await load_request(session, account_id=account, request_id=request_id)
        assert request is not None
        assert request.version == version
        assert request.action_id is None
        assert request.expires_at == request.requested_at + timedelta(hours=24)
        question = await session.scalar(
            select(PendingInput).where(
                PendingInput.account_id == account, PendingInput.ref_id == request_id
            )
        )
        assert question is not None
        assert question.kind == "owner_request"


async def test_j03_reply_to_original_turn_beats_other_focused_request(env: Env) -> None:
    account, _, original, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        first = await session.get(ControlTurn, original)
        assert first is not None
        first.telegram_message_id = 901
        other = ControlTurn(
            account_id=account,
            role="owner",
            text="New synthetic topic",
            created_at=env.clock.now(),
        )
        session.add(other)
        await session.flush()
        new = await open_request(
            session,
            account_id=account,
            turn_id=other.id,
            operation="compose",
            sources=(source(other.id, other.text or "", purpose="instruction"),),
            now=env.clock.now(),
        )
        new_id = new.request_id
    turn_id = await answer(env, account, reply=901)
    async with env.factory() as session:
        selected = await resolve_continuation(
            session,
            account_id=account,
            turn_id=turn_id,
            reply_to_message_id=901,
            now=env.clock.now(),
        )
        assert selected.request_id == request_id
        old = await load_request(session, account_id=account, request_id=request_id)
        new = await load_request(session, account_id=account, request_id=new_id)
        assert old is not None
        assert new is not None
        assert old.state == "suspended"
        assert new.sources[0].span.turn_id != original
        assert new.contact_id is None


async def test_j06_cancelled_request_never_answers_a_later_name(env: Env) -> None:
    account, _, _, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        changed = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=SetState(state="cancelled", reason="owner_cancelled"),
            now=env.clock.now(),
        )
        assert changed.status == "applied"
    turn_id = await answer(env, account)
    async with env.factory() as session:
        selected = await resolve_continuation(
            session,
            account_id=account,
            turn_id=turn_id,
            reply_to_message_id=None,
            now=env.clock.now(),
        )
        assert selected.status == "none"


async def test_j19_missing_question_recovers_from_facts_without_changing_sources(env: Env) -> None:
    account, _, _, request_id = await seed_request(env)
    turn_id = await answer(env, account)
    for _ in range(2):
        async with env.factory() as session, session.begin():
            turn = await session.get(ControlTurn, turn_id)
            assert turn is not None
            loaded = await load_owner_continuation(
                session,
                account_id=account,
                turn=turn,
                now=env.clock.now(),
                managed_allowed=True,
            )
            assert loaded.request is not None
            assert loaded.request.version == 1
            assert loaded.request.request_id == request_id
            assert loaded.question_input_id is not None
            # With autoflush disabled, recovery must persist the link before
            # a later bulk deletion can remove its target in this transaction.
            linked_question = await session.scalar(
                select(OwnerRequest.question_input_id).where(
                    OwnerRequest.account_id == account, OwnerRequest.id == request_id
                )
            )
            assert linked_question == loaded.question_input_id
            await session.execute(delete(PendingInput).where(PendingInput.account_id == account))


async def test_explicit_missing_and_foreign_targets_do_not_fall_back(env: Env) -> None:
    account, _, _, _ = await seed_request(env)
    other, _, original, _ = await seed_request(env)
    async with env.factory() as session, session.begin():
        foreign = await session.get(ControlTurn, original)
        assert foreign is not None
        foreign.telegram_message_id = 991
    turn_id = await answer(env, account, reply=991)
    async with env.factory() as session:
        selected = await resolve_continuation(
            session,
            account_id=account,
            turn_id=turn_id,
            reply_to_message_id=991,
            now=env.clock.now(),
        )
        assert selected.status == "unavailable"
        assert other != account


async def test_reply_to_card_selects_presented_version_then_rejects_stale(env: Env) -> None:
    account, conversation, _, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        action = Action(
            account_id=account,
            conversation_id=conversation,
            kind="send_message",
            origin="owner_dictated",
            state="awaiting_approval",
            text="Synthetic card K",
            version=1,
            card_message_id=990,
            card_state="awaiting_approval",
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
    turn_id = await answer(env, account, reply=990)
    async with env.factory() as session, session.begin():
        selected = await resolve_continuation(
            session,
            account_id=account,
            turn_id=turn_id,
            reply_to_message_id=990,
            now=env.clock.now(),
        )
        assert selected.action_id == action_id
        assert selected.action_version == 1
        action = await session.get(Action, action_id)
        assert action is not None
        action.version = 2
        action.card_state = None
    async with env.factory() as session:
        selected = await resolve_continuation(
            session,
            account_id=account,
            turn_id=turn_id,
            reply_to_message_id=990,
            now=env.clock.now(),
        )
        assert selected.status == "unavailable"
        assert selected.code == "stale"


async def test_j26_purged_source_is_unavailable_never_reconstructed(env: Env) -> None:
    account, _, original, _ = await seed_request(env)
    async with env.factory() as session, session.begin():
        turn = await session.get(ControlTurn, original)
        assert turn is not None
        turn.text = None
    turn_id = await answer(env, account)
    async with env.factory() as session, session.begin():
        turn = await session.get(ControlTurn, turn_id)
        assert turn is not None
        loaded = await load_owner_continuation(
            session,
            account_id=account,
            turn=turn,
            now=env.clock.now(),
            managed_allowed=True,
        )
        assert loaded.selection.status == "unavailable"
        assert loaded.selection.code == "source_unavailable"
        assert loaded.sources == ()
        assert loaded.request is None


async def test_forward_is_not_a_pending_answer(env: Env) -> None:
    account, _, _, _ = await seed_request(env)
    turn_id = await answer(env, account)
    async with env.factory() as session, session.begin():
        turn = await session.get(ControlTurn, turn_id)
        assert turn is not None
        turn.third_party = True
        turn.owner_text_verified = True  # Cannot turn a forward into an owner answer.
    async with env.factory() as session:
        result = await resolve_continuation(
            session,
            account_id=account,
            turn_id=turn_id,
            reply_to_message_id=None,
            now=env.clock.now(),
        )
        assert result.status != "selected"


async def test_stale_question_does_not_hijack_unrelated_owner_input(env: Env) -> None:
    account, _, _, request_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        suspended = await update_request(
            session,
            account_id=account,
            request_id=request_id,
            expected_version=1,
            update=SetState(state="suspended", reason="topic_switch"),
            now=env.clock.now(),
        )
        assert suspended.status == "applied"
        stale = PendingInput(
            account_id=account,
            kind="owner_request",
            ref_id=request_id,
            expires_at=env.clock.now() + timedelta(hours=1),
        )
        session.add(stale)
        await session.flush()
        row = await session.get(OwnerRequest, request_id)
        assert row is not None
        row.question_input_id = stale.id
    turn_id = await answer(env, account)
    async with env.factory() as session:
        result = await resolve_continuation(
            session,
            account_id=account,
            turn_id=turn_id,
            reply_to_message_id=None,
            now=env.clock.now(),
        )
        assert result.status == "none"


async def test_legacy_mixed_text_is_not_promoted_to_owner_source(env: Env) -> None:
    account, _, original, _ = await seed_request(env)
    async with env.factory() as session, session.begin():
        row = await session.get(ControlTurn, original)
        assert row is not None
        row.third_party = True
        row.quoted_text = "Synthetic legacy quote"
        row.owner_text_verified = False
        legacy_source = source(original, row.text or "", purpose="instruction")
    # Source validation deliberately reloads persisted provenance. Use a new
    # session so the fixture cannot be overwritten by populate_existing before
    # its legacy flags have reached the database (autoflush is disabled).
    async with env.factory() as session:
        checked = await load_source(
            session,
            account_id=account,
            source=legacy_source,
        )
        assert isinstance(checked, SourceUnavailable)


async def test_explicit_return_resumes_old_request_without_transferring_body(env: Env) -> None:
    account, _, original, old_id = await seed_request(env)
    async with env.factory() as session, session.begin():
        turn = await session.get(ControlTurn, original)
        assert turn is not None
        turn.telegram_message_id = 901
        session.add(
            ControlTurn(
                account_id=account,
                role="assistant",
                text="Synthetic earlier clarification",
                telegram_message_id=903,
                refs={"owner_request_ids": [old_id]},
                created_at=env.clock.now(),
            )
        )
        new_turn = ControlTurn(
            account_id=account,
            role="owner",
            text="A different synthetic task",
            created_at=env.clock.now(),
        )
        session.add(new_turn)
        await session.flush()
        new = await open_request(
            session,
            account_id=account,
            turn_id=new_turn.id,
            operation="compose",
            sources=(source(new_turn.id, new_turn.text or "", purpose="instruction"),),
            now=env.clock.now(),
        )
        new_id = new.request_id
    turn_id = await answer(env, account, reply=903)
    async with env.factory() as session, session.begin():
        turn = await session.get(ControlTurn, turn_id)
        assert turn is not None
        loaded = await load_owner_continuation(
            session,
            account_id=account,
            turn=turn,
            now=env.clock.now(),
            managed_allowed=True,
        )
        assert loaded.request is not None
        assert loaded.request.request_id == old_id
        assert loaded.request.state == "collecting"
        new = await load_request(session, account_id=account, request_id=new_id)
        assert new is not None
        assert new.state == "suspended"
        assert new.sources[0].span.turn_id != original


async def test_two_cards_and_replaced_presentation_keep_exact_version(env: Env) -> None:
    account, first_chat, _, _ = await seed_request(env)
    async with env.factory() as session, session.begin():
        contact = Contact(account_id=account, telegram_user_id=871001)
        session.add(contact)
        await session.flush()
        chat = Conversation(account_id=account, contact_id=contact.id, business_chat_id=871001)
        session.add(chat)
        await session.flush()
        k = Action(
            account_id=account,
            conversation_id=first_chat,
            kind="send_message",
            origin="owner_dictated",
            state="awaiting_approval",
            text="Synthetic K",
            version=2,
            card_message_id=904,
            card_state="awaiting_approval",
        )
        n = Action(
            account_id=account,
            conversation_id=chat.id,
            kind="send_message",
            origin="owner_dictated",
            state="awaiting_approval",
            text="Synthetic N",
            version=1,
            card_message_id=905,
            card_state="awaiting_approval",
        )
        session.add_all([k, n])
        await session.flush()
        k_id, n_id = k.id, n.id
        for message_id, version in ((903, 1), (904, 1), (904, 2)):
            session.add(
                AuditLogEntry(
                    account_id=account,
                    at=env.clock.now(),
                    actor="system",
                    event="card_presented",
                    subject_type="action",
                    subject_id=k.id,
                    details={
                        "action_id": k.id,
                        "version": version,
                        "state": "awaiting_approval",
                        "control_message_id": message_id,
                        "claim_id": None,
                    },
                )
            )
    turn_id = await answer(env, account, reply=904)
    async with env.factory() as session:
        current = await resolve_continuation(
            session,
            account_id=account,
            turn_id=turn_id,
            reply_to_message_id=904,
            now=env.clock.now(),
        )
        assert current.action_id == k_id
        assert current.action_version == 2
        stale = await resolve_continuation(
            session,
            account_id=account,
            turn_id=turn_id,
            reply_to_message_id=903,
            now=env.clock.now(),
        )
        assert stale.status == "unavailable"
        assert stale.code == "stale"
        untouched = await session.get(Action, n_id)
        assert untouched is not None
        assert untouched.version == 1
        assert untouched.text == "Synthetic N"


async def test_classified_outer_owner_instruction_survives_quote(env: Env) -> None:
    account, _, _, _ = await seed_request(env)
    turn_id = await answer(env, account)
    async with env.factory() as session, session.begin():
        turn = await session.get(ControlTurn, turn_id)
        assert turn is not None
        turn.third_party = True
        turn.quoted_text = "Synthetic untrusted quote"
        turn.owner_text_verified = True
        turn.contains_managed = True
        request = await open_request(
            session,
            account_id=account,
            turn_id=turn.id,
            operation="compose",
            sources=(source(turn.id, turn.text or "", purpose="instruction"),),
            now=env.clock.now(),
        )
        request_id = request.request_id
    next_id = await answer(env, account)
    async with env.factory() as session, session.begin():
        turn = await session.get(ControlTurn, next_id)
        assert turn is not None
        loaded = await load_owner_continuation(
            session,
            account_id=account,
            turn=turn,
            now=env.clock.now(),
            managed_allowed=False,
        )
        assert loaded.request is not None
        assert loaded.request.request_id == request_id
        assert loaded.sources[0].text == "Kamola Ziyo"
        assert loaded.sources[0].untrusted is False
        assert loaded.sources[0].managed is False


async def test_reply_metadata_export_and_deletion_are_scoped(env: Env) -> None:
    account, _, _, _ = await seed_request(env)
    other, _, _, _ = await seed_request(env)
    turn_id = await answer(env, account, reply=999)
    foreign_id = await answer(env, other, reply=999)
    async with env.factory() as session, session.begin():
        exported = await export_owner_requests(session, account_id=account)
        assert [row["turn_id"] for row in exported["control_reply_metadata"]] == [turn_id]
        await delete_everything(session, account_id=account, actor="owner", clock=env.clock)
    async with env.factory() as session:
        erased = await session.get(ControlTurn, turn_id)
        foreign = await session.get(ControlTurn, foreign_id)
        assert erased is not None
        assert foreign is not None
        assert erased.reply_to_telegram_message_id is None
        assert erased.owner_text_verified is False
        assert foreign.reply_to_telegram_message_id == 999


async def test_presentation_mapping_removed_on_scope_purge(env: Env) -> None:
    account, conversation, _, _ = await seed_request(env)
    async with env.factory() as session, session.begin():
        action = Action(
            account_id=account,
            conversation_id=conversation,
            kind="send_message",
            origin="owner_dictated",
            state="awaiting_approval",
            version=1,
        )
        session.add(action)
        await session.flush()
        session.add(
            AuditLogEntry(
                account_id=account,
                at=env.clock.now(),
                actor="system",
                event="card_presented",
                subject_type="action",
                subject_id=action.id,
                details={
                    "action_id": action.id,
                    "version": 1,
                    "state": "awaiting_approval",
                    "control_message_id": 990,
                    "claim_id": None,
                },
            )
        )
    turn_id = await answer(env, account, reply=990)
    async with env.factory() as session, session.begin():
        exported = await export_owner_requests(session, account_id=account)
        assert exported["card_presentations"][0]["control_message_id"] == 990
        await purge_request_sources(
            session,
            account_id=account,
            conversation_ids=(conversation,),
            now=env.clock.now(),
        )
        assert (
            await session.scalar(
                select(AuditLogEntry.id).where(
                    AuditLogEntry.account_id == account, AuditLogEntry.event == "card_presented"
                )
            )
            is None
        )
        turn = await session.get(ControlTurn, turn_id)
        assert turn is not None
        assert turn.reply_to_telegram_message_id is None
