"""Single-action transitions: approval, editing, expiry (S04.1 §3.6; R7, R15).

Every change is conditional, version/token-bound where it matters, audited
content-free and account-scoped. Real PostgreSQL; synthetic chats.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update

from assistant.actions.lifecycle import (
    ApproveStatus,
    IllegalTransitionError,
    VersionStatus,
    apply_new_version,
    approve,
    cancel_edit,
    expire_due,
    reject,
    revision_failed,
    set_revision_turn,
    start_edit,
    text_hash,
    transition,
)
from assistant.actions.models import Action
from assistant.actions.proposals import propose
from assistant.actions.states import TRANSITIONS, ActionState, StateReason
from assistant.conversations.models import Conversation
from assistant.core.reply_window import REPLY_WINDOW, SAFETY_MARGIN
from tests.integration.actions_env import (
    CONFIG,
    DRAFT_TEXT,
    Chat,
    action,
    audit,
    request,
    seed_chat,
    seed_turn,
)
from tests.integration.business_env import Env

pytestmark = pytest.mark.integration

REVISED = "Synthetic revised reply"


async def card(env: Env, **chat_fields: Any) -> tuple[Chat, int, str]:
    """A chat with one awaiting card: (chat, action id, token)."""
    async with env.factory() as session, session.begin():
        chat = await seed_chat(session, env.clock.now(), **chat_fields)
        result = await propose(session, request(chat), clock=env.clock, config=CONFIG)
    row = await action(env, result.action_id)
    assert row.approval_token is not None
    return chat, result.action_id, row.approval_token


async def approve_in(env: Env, account_id: int, token: str) -> Any:
    async with env.factory() as session, session.begin():
        return await approve(session, account_id=account_id, token=token, now=env.clock.now())


async def editing(env: Env) -> tuple[Chat, int, str]:
    chat, action_id, token = await card(env)
    async with env.factory() as session, session.begin():
        version = await start_edit(
            session, account_id=chat.account_id, token=token, now=env.clock.now()
        )
    assert version == 1
    return chat, action_id, token


async def new_version(env: Env, chat: Chat, action_id: int, **fields: Any) -> Any:
    values: dict[str, Any] = {
        "expected_version": 1,
        "text": REVISED,
        "draft_run_id": None,
        "draft_flags": None,
        "shown_through_message_id": chat.contact_message_id,
        "base_message_id": chat.contact_message_id,
        **fields,
    }
    async with env.factory() as session, session.begin():
        return await apply_new_version(
            session,
            account_id=chat.account_id,
            action_id=action_id,
            now=env.clock.now(),
            **values,
        )


async def events(env: Env, action_id: int) -> list[str]:
    return [entry.event for entry in await audit(env, action_id)]


async def test_approve_needs_the_current_token(env: Env) -> None:
    chat, action_id, token = await card(env)

    wrong = await approve_in(env, chat.account_id, "not-the-token")
    right = await approve_in(env, chat.account_id, token)

    assert wrong.status is ApproveStatus.NOT_ACTIVE
    assert (right.status, right.action_id, right.version) == (ApproveStatus.APPROVED, action_id, 1)
    row = await action(env, action_id)
    assert row.state == "approved"
    assert row.approval_token is None
    assert row.decided_at == env.clock.now()
    assert row.approved_text_hash == text_hash(DRAFT_TEXT)


async def test_a_double_approve_applies_once(env: Env) -> None:
    chat, action_id, token = await card(env)

    first = await approve_in(env, chat.account_id, token)
    second = await approve_in(env, chat.account_id, token)

    assert first.status is ApproveStatus.APPROVED
    assert second.status is ApproveStatus.NOT_ACTIVE
    assert (await events(env, action_id)).count("action_approved") == 1


@pytest.mark.parametrize(
    ("contact_wrote", "later", "reason"),
    [
        (timedelta(hours=1), timedelta(hours=7), "max_age"),
        (timedelta(hours=20), timedelta(hours=4), "window_closed"),
    ],
)
async def test_approve_after_expiry_expires_instead(
    env: Env, contact_wrote: timedelta, later: timedelta, reason: str
) -> None:
    chat, action_id, token = await card(env, contact_wrote=contact_wrote)
    env.clock.advance(seconds=later.total_seconds())

    result = await approve_in(env, chat.account_id, token)

    assert result.status is ApproveStatus.EXPIRED
    row = await action(env, action_id)
    assert (row.state, row.state_reason) == ("expired", reason)
    assert row.approved_text_hash is None


async def test_start_edit_moves_to_revising_and_voids_the_token(env: Env) -> None:
    _, action_id, _ = await editing(env)

    row = await action(env, action_id)
    assert row.state == "revising"
    assert row.approval_token is None
    assert (await events(env, action_id))[-1] == "action_revision_requested"


async def test_approve_is_refused_while_revising(env: Env) -> None:
    chat, action_id, token = await editing(env)

    result = await approve_in(env, chat.account_id, token)

    assert result.status is ApproveStatus.NOT_ACTIVE
    assert (await action(env, action_id)).state == "revising"


async def test_apply_new_version_bumps_the_version_and_rotates_the_token(env: Env) -> None:
    chat, action_id, old_token = await editing(env)
    flags = {"needs_reply": True, "language": "en"}

    result = await new_version(env, chat, action_id, draft_flags=flags)

    assert result.status is VersionStatus.APPLIED
    assert result.version == 2
    assert result.token not in (None, old_token)
    row = await action(env, action_id)
    assert (row.state, row.version, row.text) == ("awaiting_approval", 2, REVISED)
    assert row.approval_token == result.token
    assert row.draft_flags == flags
    assert (await events(env, action_id))[-1] == "action_edited"
    assert (await approve_in(env, chat.account_id, old_token)).status is ApproveStatus.NOT_ACTIVE
    approved = await approve_in(env, chat.account_id, result.token)
    assert (approved.status, approved.version) == (ApproveStatus.APPROVED, 2)
    assert (await action(env, action_id)).approved_text_hash == text_hash(REVISED)


async def test_a_stale_version_cannot_be_applied(env: Env) -> None:
    chat, action_id, _ = await editing(env)
    before = len(await audit(env, action_id))

    result = await new_version(env, chat, action_id, expected_version=2)

    assert result.status is VersionStatus.STALE
    row = await action(env, action_id)
    assert (row.state, row.version, row.text) == ("revising", 1, DRAFT_TEXT)
    assert len(await audit(env, action_id)) == before


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("x" * 4097, VersionStatus.TOO_LONG),
        # 2049 emoji are 4098 UTF-16 code units: too long though 2049 characters.
        ("\N{GRINNING FACE}" * 2049, VersionStatus.TOO_LONG),
        ("", VersionStatus.EMPTY),
        ("  \n ", VersionStatus.EMPTY),
    ],
)
async def test_a_too_long_version_stays_revising(
    env: Env, text: str, status: VersionStatus
) -> None:
    chat, action_id, _ = await editing(env)

    result = await new_version(env, chat, action_id, text=text)

    assert result.status is status
    row = await action(env, action_id)
    assert (row.state, row.version, row.text) == ("revising", 1, DRAFT_TEXT)


async def test_a_new_version_never_extends_expiry(env: Env) -> None:
    chat, action_id, _ = await editing(env)
    original = (await action(env, action_id)).expires_at
    env.clock.advance(hours=1)

    later = await new_version(env, chat, action_id)
    assert later.status is VersionStatus.APPLIED
    assert (await action(env, action_id)).expires_at == original

    # The window now closes before the original expiry: it shortens to it.
    anchor = env.clock.now() - timedelta(hours=23)
    async with env.factory() as session, session.begin():
        await session.execute(
            update(Conversation)
            .where(Conversation.id == chat.conversation_id)
            .values(last_contact_msg_at=anchor)
        )
        assert later.token is not None
        await start_edit(
            session, account_id=chat.account_id, token=later.token, now=env.clock.now()
        )
    shorter = await new_version(env, chat, action_id, expected_version=2)
    assert shorter.status is VersionStatus.APPLIED
    assert (await action(env, action_id)).expires_at == anchor + REPLY_WINDOW - SAFETY_MARGIN


async def test_set_revision_turn_needs_the_current_version(env: Env) -> None:
    chat, action_id, _ = await editing(env)
    async with env.factory() as session, session.begin():
        turn_id = await seed_turn(session, chat.account_id)
        stale = await set_revision_turn(
            session,
            account_id=chat.account_id,
            action_id=action_id,
            expected_version=2,
            turn_id=turn_id,
        )
        current = await set_revision_turn(
            session,
            account_id=chat.account_id,
            action_id=action_id,
            expected_version=1,
            turn_id=turn_id,
        )

    assert (stale, current) == (False, True)
    assert (await action(env, action_id)).revision_turn_id == turn_id
    async with env.factory() as session, session.begin():
        await set_revision_turn(
            session,
            account_id=chat.account_id,
            action_id=action_id,
            expected_version=1,
            turn_id=None,
        )
    assert (await action(env, action_id)).revision_turn_id is None


@pytest.mark.parametrize("back", [cancel_edit, revision_failed])
async def test_cancel_edit_restores_the_same_version_with_a_new_token(env: Env, back: Any) -> None:
    chat, action_id, old_token = await editing(env)
    async with env.factory() as session, session.begin():
        turn_id = await seed_turn(session, chat.account_id)
        await set_revision_turn(
            session,
            account_id=chat.account_id,
            action_id=action_id,
            expected_version=1,
            turn_id=turn_id,
        )
        token = await back(
            session,
            account_id=chat.account_id,
            action_id=action_id,
            expected_version=1,
            now=env.clock.now(),
        )

    assert token not in (None, old_token)
    row = await action(env, action_id)
    assert (row.state, row.version, row.text) == ("awaiting_approval", 1, DRAFT_TEXT)
    assert (row.approval_token, row.revision_turn_id) == (token, None)
    assert (await events(env, action_id))[-1] == "action_revision_cancelled"


#: The §3.6 event table (R15), pair by pair. `None`: no P2 event (M06 pairs).
EXPECTED_EVENTS: dict[tuple[ActionState, ActionState], str | None] = {
    (ActionState.PROPOSED, ActionState.AWAITING_APPROVAL): "action_proposed",
    (ActionState.PROPOSED, ActionState.APPROVED): None,
    (ActionState.PROPOSED, ActionState.BLOCKED): "action_blocked",
    (ActionState.PROPOSED, ActionState.CANCELLED): "action_cancelled",
    (ActionState.AWAITING_APPROVAL, ActionState.APPROVED): "action_approved",
    (ActionState.AWAITING_APPROVAL, ActionState.REVISING): "action_revision_requested",
    (ActionState.AWAITING_APPROVAL, ActionState.REJECTED): "action_rejected",
    (ActionState.AWAITING_APPROVAL, ActionState.OUTDATED): "action_outdated",
    (ActionState.AWAITING_APPROVAL, ActionState.EXPIRED): "action_expired",
    (ActionState.AWAITING_APPROVAL, ActionState.CANCELLED): "action_cancelled",
    (ActionState.REVISING, ActionState.AWAITING_APPROVAL): "action_revision_cancelled",
    (ActionState.REVISING, ActionState.OUTDATED): "action_outdated",
    (ActionState.REVISING, ActionState.EXPIRED): "action_expired",
    (ActionState.REVISING, ActionState.CANCELLED): "action_cancelled",
    (ActionState.APPROVED, ActionState.SCHEDULED): None,
    (ActionState.APPROVED, ActionState.EXECUTING): "action_claimed",
    (ActionState.APPROVED, ActionState.OUTDATED): "action_outdated",
    (ActionState.APPROVED, ActionState.EXPIRED): "action_expired",
    (ActionState.APPROVED, ActionState.BLOCKED): "action_blocked",
    (ActionState.APPROVED, ActionState.CANCELLED): "action_cancelled",
    (ActionState.SCHEDULED, ActionState.APPROVED): None,
    (ActionState.SCHEDULED, ActionState.CANCELLED): "action_cancelled",
    (ActionState.EXECUTING, ActionState.SUCCEEDED): "action_succeeded",
    (ActionState.EXECUTING, ActionState.FAILED): "action_failed",
    (ActionState.EXECUTING, ActionState.APPROVED): "action_rate_limited",
    (ActionState.EXECUTING, ActionState.DELIVERY_UNCERTAIN): "action_delivery_uncertain",
    (ActionState.EXECUTING, ActionState.EXPIRED): "action_expired",
    (ActionState.DELIVERY_UNCERTAIN, ActionState.SUCCEEDED): "action_late_result",
    (ActionState.DELIVERY_UNCERTAIN, ActionState.FAILED): "action_late_result",
}


async def seeded(env: Env, state: ActionState) -> tuple[Chat, int]:
    """A fresh chat with one action already in `state` (one live card per chat)."""
    async with env.factory() as session, session.begin():
        chat = await seed_chat(session, env.clock.now())
        row = Action(
            account_id=chat.account_id,
            conversation_id=chat.conversation_id,
            kind="send_message",
            origin="agent_proposal",
            state=state.value,
            text=DRAFT_TEXT,
            approval_token="synthetic-token" if state is ActionState.AWAITING_APPROVAL else None,
            created_at=env.clock.now(),
            updated_at=env.clock.now(),
        )
        session.add(row)
        await session.flush()
        return chat, row.id


def test_the_event_table_covers_every_transition() -> None:
    assert set(EXPECTED_EVENTS) == set(TRANSITIONS)


@pytest.mark.parametrize(
    ("pair", "expected"), sorted(EXPECTED_EVENTS.items()), ids=lambda value: str(value)
)
async def test_each_transition_writes_its_mapped_event(
    env: Env, pair: tuple[ActionState, ActionState], expected: str | None
) -> None:
    from_state, to_state = pair
    chat, action_id = await seeded(env, from_state)
    reason = StateReason.SUPERSEDED if to_state is ActionState.OUTDATED else None
    values = {"attempt_id": "attempt-1"} if from_state is ActionState.EXECUTING else {}
    token = "synthetic-token" if from_state is ActionState.AWAITING_APPROVAL else None

    async with env.factory() as session, session.begin():
        if expected is None:
            with pytest.raises(IllegalTransitionError):
                await transition(
                    session,
                    account_id=chat.account_id,
                    action_id=action_id,
                    from_state=from_state,
                    to_state=to_state,
                    reason=reason,
                    now=env.clock.now(),
                )
            return
        applied = await transition(
            session,
            account_id=chat.account_id,
            action_id=action_id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
            now=env.clock.now(),
            token=token,
            values=values,
        )

    assert applied is True
    expected = "action_superseded" if expected == "action_outdated" else expected
    entries = await audit(env, action_id)
    assert [entry.event for entry in entries] == [expected]
    details = entries[0].details
    assert (details["from"], details["to"]) == (from_state.value, to_state.value)
    assert details.get("attempt_id") == values.get("attempt_id")
    row = await action(env, action_id)
    assert row.state == to_state.value
    if to_state is not ActionState.AWAITING_APPROVAL:
        assert row.approval_token is None


async def test_only_the_cards_token_can_approve_it(env: Env) -> None:
    chat, action_id = await seeded(env, ActionState.AWAITING_APPROVAL)

    async with env.factory() as session, session.begin():
        with pytest.raises(IllegalTransitionError):
            await transition(
                session,
                account_id=chat.account_id,
                action_id=action_id,
                from_state=ActionState.AWAITING_APPROVAL,
                to_state=ActionState.APPROVED,
                reason=None,
                now=env.clock.now(),
            )

    row = await action(env, action_id)
    assert row.state == "awaiting_approval"
    assert await audit(env, action_id) == []


async def test_reject_is_terminal(env: Env) -> None:
    chat, action_id, token = await card(env)
    async with env.factory() as session, session.begin():
        rejected = await reject(
            session, account_id=chat.account_id, token=token, now=env.clock.now()
        )

    assert rejected is True
    row = await action(env, action_id)
    assert (row.state, row.state_reason) == ("rejected", "owner_rejected")
    assert (await approve_in(env, chat.account_id, token)).status is ApproveStatus.NOT_ACTIVE
    assert not any(source is ActionState.REJECTED for source, _ in TRANSITIONS)


async def test_expire_due_distinguishes_window_close_from_max_age_and_skips_executing(
    env: Env,
) -> None:
    fresh, fresh_id, _ = await card(env)
    late, late_id, late_token = await card(env, contact_wrote=timedelta(hours=20))
    approved = await approve_in(env, late.account_id, late_token)
    claimed_chat, claimed_id = await seeded(env, ActionState.EXECUTING)
    async with env.factory() as session, session.begin():
        await session.execute(
            update(Action)
            .where(Action.id == claimed_id)
            .values(expires_at=env.clock.now() + timedelta(minutes=1))
        )
    env.clock.advance(hours=7)

    async with env.factory() as session, session.begin():
        expired = await expire_due(session, now=env.clock.now())

    assert approved.status is ApproveStatus.APPROVED
    assert sorted(expired) == sorted([fresh_id, late_id])
    assert ((await action(env, fresh_id)).state_reason) == "max_age"
    assert ((await action(env, late_id)).state, (await action(env, late_id)).state_reason) == (
        "expired",
        "window_closed",
    )
    assert (await action(env, claimed_id)).state == "executing"
    assert fresh.account_id != claimed_chat.account_id


async def test_an_illegal_transition_raises(env: Env) -> None:
    chat, action_id = await seeded(env, ActionState.SUCCEEDED)
    async with env.factory() as session, session.begin():
        for source, target in [
            (ActionState.SUCCEEDED, ActionState.APPROVED),
            (ActionState.DELIVERY_UNCERTAIN, ActionState.APPROVED),
            (ActionState.DELIVERY_UNCERTAIN, ActionState.EXECUTING),
            (ActionState.REJECTED, ActionState.AWAITING_APPROVAL),
        ]:
            with pytest.raises(IllegalTransitionError):
                await transition(
                    session,
                    account_id=chat.account_id,
                    action_id=action_id,
                    from_state=source,
                    to_state=target,
                    reason=None,
                    now=env.clock.now(),
                )
        with pytest.raises(IllegalTransitionError):
            await transition(
                session,
                account_id=chat.account_id,
                action_id=action_id,
                from_state=ActionState.EXECUTING,
                to_state=ActionState.SUCCEEDED,
                reason=None,
                now=env.clock.now(),
                values={"state": "approved"},
            )
    assert (await action(env, action_id)).state == "succeeded"


async def test_every_transition_writes_one_content_free_audit_row(env: Env) -> None:
    chat, action_id, token = await card(
        env,
    )
    canary = "CANARY-transition-text-41c9"
    async with env.factory() as session, session.begin():
        await start_edit(session, account_id=chat.account_id, token=token, now=env.clock.now())
    revised = await new_version(env, chat, action_id, text=canary)
    assert revised.token is not None
    await approve_in(env, chat.account_id, revised.token)

    entries = await audit(env, action_id)
    assert [entry.event for entry in entries] == [
        "action_proposed",
        "action_revision_requested",
        "action_edited",
        "action_approved",
    ]
    for entry in entries:
        assert canary not in json.dumps(entry.details)
        assert DRAFT_TEXT not in json.dumps(entry.details)
        assert set(entry.details) == {"from", "to", "version", "reason"}
    assert [entry.actor for entry in entries] == ["system", "owner", "system", "owner"]


async def test_transitions_never_cross_accounts(env: Env) -> None:
    mine, action_id, token = await card(env)
    theirs, _, _ = await card(env)
    now: datetime = env.clock.now()

    async with env.factory() as session, session.begin():
        foreign_approve = await approve(session, account_id=theirs.account_id, token=token, now=now)
        foreign_reject = await reject(session, account_id=theirs.account_id, token=token, now=now)
        foreign_edit = await start_edit(session, account_id=theirs.account_id, token=token, now=now)
        foreign_move = await transition(
            session,
            account_id=theirs.account_id,
            action_id=action_id,
            from_state=ActionState.AWAITING_APPROVAL,
            to_state=ActionState.CANCELLED,
            reason=StateReason.DATA_DELETED,
            now=now,
        )
        foreign_turn = await set_revision_turn(
            session,
            account_id=theirs.account_id,
            action_id=action_id,
            expected_version=1,
            turn_id=None,
        )

    assert foreign_approve.status is ApproveStatus.NOT_ACTIVE
    assert (foreign_reject, foreign_edit, foreign_move, foreign_turn) == (False, None, False, False)
    row = await action(env, action_id)
    assert (row.state, row.approval_token) == ("awaiting_approval", token)
    assert await events(env, action_id) == ["action_proposed"]
    assert mine.account_id != theirs.account_id
