"""Approval cards through the production wiring (S04.3 §3.3-§3.5).

`_wire_m04` builds the cards, the `act`/`draft_edit` routes and the jobs; the
real inbox processes button presses and the owner's messages; jobs run with
their registered bodies (`tests/scenarios/p2_drive.py`). Only the clock, the
Telegram transport and the model are fake; every text is synthetic.
"""

from __future__ import annotations

import ast
import inspect
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.accounts import service
from assistant.accounts.callbacks import parse
from assistant.accounts.models import Account, ControlTurn, PendingInput
from assistant.actions.executor import ActionExecutor
from assistant.actions.models import Action
from assistant.conversations.models import Conversation
from assistant.db.uow import transaction
from assistant.inbox.notify import OwnerAlerts
from assistant.telegram.business_targets import BusinessTargetResolver
from assistant.telegram.control import approvals
from assistant.telegram.control.agent_channel import GatewayControlChannel
from assistant.telegram.control.approvals import (
    ANSWER_APPROVED_SENDING_OFF,
    ANSWER_EDIT,
    ANSWER_EXPIRED,
    ANSWER_REDRAFTING,
    ANSWER_SENDING,
    DRAFT_EDIT,
)
from assistant.telegram.control.handlers import CALLBACK_EXPIRED_TEXT
from assistant.telegram.gateway import OutboundGateway
from tests.fakes.clock import FakeSleep
from tests.integration.business_env import (
    OTHER_CONTACT,
    OWNER,
    business_message,
    callback,
    deliver,
    private_message,
)
from tests.scenarios.p2_drive import (
    DRAFT,
    FLAGS,
    REPLACEMENT,
    Scenario,
    advance,
    draft_result,
    m04_environment,
    press_action,
    run_queued,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def s(migrated_url: str) -> AsyncIterator[Scenario]:
    async with m04_environment(migrated_url) as scenario:
        scenario.env.sender.answered_callbacks.clear()  # onboarding's presses
        yield scenario


# ------------------------------------------------------------ helpers
def answers(s: Scenario) -> list[str | None]:
    return [answer["text"] for answer in s.env.sender.answered_callbacks]


async def jobs(s: Scenario, task: str) -> list[dict[str, Any]]:
    async with s.env.factory() as session:
        rows = await session.execute(
            text(
                "SELECT status, args, queueing_lock FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = :task ORDER BY id"
            ),
            {"task": task},
        )
        return [dict(row._mapping) for row in rows.all()]


def button_token(s: Scenario, row: Action, op: str) -> str:
    """The token of the `op` button on the card as last shown."""
    for button in s.card_buttons(row):
        data = button.get("callback_data")
        if data and parse(data).op == op:
            return parse(data).token
    raise AssertionError(f"no {op} button on the card")


async def press(s: Scenario, token: str, op: str, *, message_id: int | None = 1) -> None:
    await deliver(s.env, callback(f"act:{token}:{op}", message_id=message_id))


async def owner_writes(s: Scenario, words: str, **message: Any) -> None:
    update = private_message(OWNER, words)
    update["message"].update(message)
    await deliver(s.env, update)


async def start_edit(s: Scenario, row: Action, words: str) -> Action:
    await press_action(s, row.id, "ed")
    await owner_writes(s, words)
    return await s.action(row.id)


async def run_execute_jobs(s: Scenario) -> int:
    """The queued `actions.execute` jobs, run once each by S04.4's executor
    over a gateway that resolves business targets (the scenario harness's
    gateway refuses every business send locally)."""
    gateway = OutboundGateway(
        s.env.sender,
        s.env.clock,
        sleep=FakeSleep(),
        targets=BusinessTargetResolver(s.env.factory),
    )
    config = s.wiring.actions_config
    assert config is not None
    executor = ActionExecutor(
        session_factory=s.env.factory,
        clock=s.env.clock,
        config=config,
        sender=gateway,
        alerts=OwnerAlerts(s.env.factory, GatewayControlChannel(gateway), s.env.clock),
        scheduler=None,
        presenter=s.wiring.approval_cards,
    )
    queued = [job["args"]["action_id"] for job in await jobs(s, "actions.execute")]
    for action_id in queued:
        await executor.execute(action_id)
    return len(queued)


async def edit_inputs(s: Scenario) -> list[PendingInput]:
    async with s.env.factory() as session:
        return list(
            await session.scalars(
                select(PendingInput).where(
                    PendingInput.account_id == s.account_id, PendingInput.kind == DRAFT_EDIT
                )
            )
        )


# ------------------------------------------------------------ who may press
async def test_a_callback_from_a_non_owner_is_rejected(s: Scenario) -> None:
    row = await s.seed()
    # Someone not on the allowlist: routing never reaches a handler.
    await deliver(
        s.env,
        callback(f"act:{row.approval_token}:ok", from_id=OTHER_CONTACT, chat_id=OTHER_CONTACT),
    )
    # Another allowlisted owner's account: the token is looked up in their
    # account only, so it is "no longer active" for them (invariant 12).
    async with transaction(s.env.factory) as uow:
        other, _ = await service.get_or_create_account(
            uow.session, telegram_user_id=7000000077, clock=s.env.clock
        )
        other.status = "active"
        await uow.session.flush()
        assert s.env.dispatcher is not None
        result = await s.env.dispatcher.dispatch_callback(
            uow.session,
            callback_id="cb-foreign",
            from_id=7000000077,
            data=f"act:{row.approval_token}:ok",
            chat_id=7000000077,
            message_id=1,
        )
    assert result.handled is False
    assert answers(s)[-1] == CALLBACK_EXPIRED_TEXT
    after = await s.action(row.id)
    assert (after.state, after.approval_token) == ("awaiting_approval", row.approval_token)
    assert await jobs(s, "actions.execute") == []
    assert "action_approved" not in await s.events(row.id)


async def test_a_stale_token_answers_no_longer_active(s: Scenario) -> None:
    row = await s.seed()
    for op in ("ok", "ed", "rj", "cx", "rd"):
        await press(s, "Zz9Zz9Zz9Z", op)
    await press(s, row.approval_token or "", "cx")  # an approval token is not a card token
    assert answers(s) == [CALLBACK_EXPIRED_TEXT] * 6
    assert (await s.action(row.id)).state == "awaiting_approval"


# ------------------------------------------------------------ approve
@pytest.mark.parametrize("one_batch", [True, False])
async def test_rg04_double_tap_approve_sends_once(s: Scenario, one_batch: bool) -> None:
    row = await s.seed()
    taps = [callback(f"act:{row.approval_token}:ok") for _ in range(2)]
    if one_batch:
        await deliver(s.env, *taps)
    else:
        for tap in taps:
            await deliver(s.env, tap)
    assert (await s.action(row.id)).state == "approved"
    assert [job["args"] for job in await jobs(s, "actions.execute")] == [{"action_id": row.id}]
    assert (await s.events(row.id)).count("action_approved") == 1
    assert answers(s) == [ANSWER_SENDING, CALLBACK_EXPIRED_TEXT]
    assert await run_execute_jobs(s) == 1
    assert (await s.action(row.id)).state == "succeeded"
    assert len(s.env.sender.plain_sends) == 1
    assert s.env.sender.plain_sends[0]["text"] == DRAFT


async def test_approve_defers_execute_in_the_same_transaction(s: Scenario) -> None:
    row = await s.seed()
    assert s.env.dispatcher is not None
    async with s.env.factory() as session:
        await session.begin()
        await s.env.dispatcher.dispatch_callback(
            session,
            callback_id="cb-rolled-back",
            from_id=OWNER,
            data=f"act:{row.approval_token}:ok",
            chat_id=OWNER,
            message_id=1,
        )
        # Inside the press's transaction: approved, and its job is there too.
        inside = await session.scalar(select(Action.state).where(Action.id == row.id))
        queued = await session.scalar(
            text(
                "SELECT count(*) FROM procrastinate.procrastinate_jobs "
                "WHERE task_name = 'actions.execute'"
            )
        )
        assert (inside, queued) == ("approved", 1)
        await session.rollback()
    # Rolled back: neither the approval nor the job, and nothing announced.
    assert (await s.action(row.id)).state == "awaiting_approval"
    assert await jobs(s, "actions.execute") == []
    assert answers(s) == []
    # Committed: both together.
    await press(s, row.approval_token or "", "ok")
    assert (await s.action(row.id)).state == "approved"
    assert len(await jobs(s, "actions.execute")) == 1


async def test_with_sending_off_approve_ends_blocked_with_copy_ready_text(
    migrated_url: str,
) -> None:
    async with m04_environment(migrated_url, flags=FLAGS - {"actions.send"}) as off:
        row = await off.seed()
        await press(off, row.approval_token or "", "ok", message_id=row.card_message_id)
        assert [job["args"] for job in await jobs(off, "actions.execute")] == [
            {"action_id": row.id}
        ]
        await run_queued(off, "actions.execute")
        final = await off.action(row.id)
        assert (final.state, final.state_reason) == ("blocked", "sending_disabled")
        off.assert_copy_ready(final)
        card = off.final_card(final)["text"]
        assert "Not sendable</b> — sending is turned off" in card
        assert off.business_sends() == []


async def test_approve_with_sending_off_answers_approved_sending_is_off_never_sending(
    migrated_url: str,
) -> None:
    async with m04_environment(migrated_url, flags=FLAGS - {"actions.send"}) as off:
        off.env.sender.answered_callbacks.clear()  # onboarding's presses
        row = await off.seed()
        await press(off, row.approval_token or "", "ok")
        assert answers(off) == [ANSWER_APPROVED_SENDING_OFF]
        assert ANSWER_SENDING not in answers(off)

        def card_texts() -> list[str]:
            return [
                str(edit["text"])
                for edit in off.env.sender.edits
                if edit["message_id"] == row.card_message_id
            ]

        # The card edited after the press says so too (spec §3.3, R17).
        assert (await off.action(row.id)).state == "approved"
        assert card_texts()
        assert "Approved — sending is off" in card_texts()[-1]
        # So does any redraw before the executor runs (the sync job's path).
        assert off.wiring.approval_cards is not None
        await off.wiring.approval_cards.sync(row.id)
        assert "Approved — sending is off" in card_texts()[-1]
        await run_queued(off, "actions.execute")
        final = await off.action(row.id)
        assert (final.state, final.state_reason) == ("blocked", "sending_disabled")
        assert "Not sendable</b> — sending is turned off" in card_texts()[-1]
        assert not [text for text in card_texts() if ANSWER_SENDING in text]


async def test_approve_during_revising_is_rejected(s: Scenario) -> None:
    row = await s.seed()
    await press_action(s, row.id, "ed")
    assert (await s.action(row.id)).state == "revising"
    await press(s, row.approval_token or "", "ok")
    assert answers(s)[-1] == CALLBACK_EXPIRED_TEXT
    assert (await s.action(row.id)).state == "revising"
    assert await jobs(s, "actions.execute") == []


async def test_approve_after_expiry_shows_expired_with_copy_ready_text(s: Scenario) -> None:
    row = await s.seed(expires_at=s.env.clock.now() + timedelta(minutes=1))
    await advance(s, 120)  # no expiry job ran: the press finds it past due
    await press(s, row.approval_token or "", "ok")
    final = await s.action(row.id)
    assert (final.state, final.state_reason) == ("expired", "max_age")
    assert answers(s) == [ANSWER_EXPIRED]
    assert await jobs(s, "actions.execute") == []
    s.assert_copy_ready(final)
    assert "Expired" in s.final_card(final)["text"]


async def test_a_contact_message_ingested_after_approval_blocks_the_send(s: Scenario) -> None:
    row = await s.seed()
    await press(s, row.approval_token or "", "ok")
    await deliver(s.env, business_message(message_id=102, date=s.date + 1))
    await run_queued(s, "actions.execute")
    final = await s.action(row.id)
    assert final.state in {"outdated", "blocked"}
    assert final.state_reason == "new_contact_message"
    assert s.business_sends() == []


# ------------------------------------------------------------ edit
async def test_the_edit_loop_replacement_keeps_the_owners_words_verbatim(s: Scenario) -> None:
    row = await s.seed()
    s.fake.parsed.append(
        draft_result(text="Synthetic model rewrite, not the owner's.", revision_kind="replacement")
    )
    await start_edit(s, row, REPLACEMENT)
    assert answers(s) == [ANSWER_EDIT]
    assert await run_queued(s, "replies.revise") == 1
    revised = await s.action(row.id)
    assert (revised.state, revised.version, revised.text) == ("awaiting_approval", 2, REPLACEMENT)
    assert REPLACEMENT in s.final_card(revised)["text"]
    assert "Synthetic model rewrite" not in s.control_text()


async def test_the_revision_turn_is_recorded_on_the_action(s: Scenario) -> None:
    row = await s.seed()
    editing = await start_edit(s, row, "Synthetic: make it warmer.")
    assert editing.state == "revising"
    async with s.env.factory() as session:
        turn = await session.scalar(
            select(ControlTurn).where(ControlTurn.id == editing.revision_turn_id)
        )
        contact_id = await session.scalar(
            select(Conversation.contact_id).where(Conversation.id == s.conversation_id)
        )
    assert turn is not None
    assert (turn.role, turn.text, turn.contains_managed) == (
        "owner",
        "Synthetic: make it warmer.",
        False,
    )
    assert turn.refs == {"conversation_ids": [s.conversation_id], "contact_ids": [contact_id]}
    [job] = await jobs(s, "replies.revise")
    assert job["args"] == {"action_id": row.id, "turn_id": turn.id}
    assert job["queueing_lock"] == f"revise:{row.id}"
    assert await edit_inputs(s) == []
    assert "Revising…" in s.control_text()


async def test_the_edit_loop_instruction_produces_a_new_version_and_token(s: Scenario) -> None:
    row = await s.seed()
    s.fake.parsed.append(draft_result(text="Synthetic shorter reply.", revision_kind="instruction"))
    await start_edit(s, row, "Synthetic: shorter please.")
    await run_queued(s, "replies.revise")
    revised = await s.action(row.id)
    assert (revised.state, revised.version, revised.text) == (
        "awaiting_approval",
        2,
        "Synthetic shorter reply.",
    )
    assert revised.approval_token
    assert revised.approval_token != row.approval_token
    assert revised.revision_turn_id is None
    assert button_token(s, revised, "ok") == revised.approval_token
    # The old card's Approve no longer works; the new one does.
    await press(s, row.approval_token or "", "ok")
    assert (await s.action(row.id)).state == "awaiting_approval"
    await press(s, revised.approval_token, "ok")
    assert (await s.action(row.id)).state == "approved"


async def test_a_too_long_replacement_stays_revising_and_asks_again(s: Scenario) -> None:
    row = await s.seed()
    too_long = "Synthetic-" + "x" * 4090  # 4100 characters
    s.fake.parsed.append(draft_result(text="Synthetic short.", revision_kind="replacement"))
    await start_edit(s, row, too_long)
    await run_queued(s, "replies.revise")
    after = await s.action(row.id)
    assert (after.state, after.version, after.text) == ("revising", 1, DRAFT)
    assert after.revision_turn_id is None
    assert "action_edited" not in await s.events(row.id)
    [question] = await edit_inputs(s)
    assert question.ref_id == row.id
    assert "too long (4100 characters; the limit is 4096)" in s.control_text()
    # The owner tries again with a short text: a new version.
    s.fake.parsed.append(draft_result(text="unused", revision_kind="replacement"))
    await owner_writes(s, "Synthetic short enough.")
    await run_queued(s, "replies.revise")
    assert (await s.action(row.id)).text == "Synthetic short enough."


async def test_a_new_version_never_extends_the_cards_expiry(s: Scenario) -> None:
    expires = s.env.clock.now() + timedelta(minutes=20)
    row = await s.seed(expires_at=expires)
    s.fake.parsed.append(draft_result(text="Synthetic new version.", revision_kind="instruction"))
    await start_edit(s, row, "Synthetic: change it.")
    await advance(s, 300)
    await run_queued(s, "replies.revise")
    revised = await s.action(row.id)
    assert revised.version == 2
    assert revised.expires_at == expires


async def test_a_revision_finishing_after_the_action_left_revising_changes_nothing(
    s: Scenario,
) -> None:
    row = await s.seed()
    await start_edit(s, row, "Synthetic: first wish.")
    [first] = await jobs(s, "replies.revise")
    # The owner cancels and edits again before the first revision runs.
    await press(s, button_token(s, await s.action(row.id), "cx"), "cx")
    cancelled = await s.action(row.id)
    assert (cancelled.state, cancelled.version) == ("awaiting_approval", 1)
    await start_edit(s, cancelled, "Synthetic: second wish.")
    second_turn = (await s.action(row.id)).revision_turn_id
    # The second edit's job met the first one's queueing lock; the first job
    # runs late: its turn is no longer the action's, so it changes nothing,
    # calls no model and hands over to the current turn.
    assert [job["args"] for job in await jobs(s, "replies.revise")] == [first["args"]]
    assert await run_queued(s, "replies.revise", limit=1) == 1
    late = await s.action(row.id)
    assert (late.state, late.version, late.text) == ("revising", 1, DRAFT)
    assert [r for r in s.fake.requests if r.purpose == "draft_reply"] == []
    queued = [job for job in await jobs(s, "replies.revise") if job["status"] == "todo"]
    assert [job["args"] for job in queued] == [{"action_id": row.id, "turn_id": second_turn}]
    # Outdated meanwhile: that revision runs and changes nothing either.
    await deliver(s.env, business_message(message_id=102, date=s.date + 1))
    s.fake.parsed.append(draft_result(text="Synthetic late text.", revision_kind="instruction"))
    await run_queued(s, "replies.revise")
    final = await s.action(row.id)
    assert (final.state, final.version, final.text) == ("outdated", 1, DRAFT)
    assert "action_edited" not in await s.events(row.id)


async def test_a_forwarded_message_never_answers_the_edit_question(s: Scenario) -> None:
    row = await s.seed()
    await press_action(s, row.id, "ed")
    await owner_writes(
        s,
        "Synthetic forwarded words.",
        forward_origin={"type": "hidden_user", "date": s.date, "sender_user_name": "Someone"},
    )
    after = await s.action(row.id)
    assert (after.state, after.revision_turn_id) == ("revising", None)
    assert await jobs(s, "replies.revise") == []
    assert len(await jobs(s, "agent.owner_request")) == 1  # a tainted agent turn instead
    assert len(await edit_inputs(s)) == 1  # the question still waits for the owner


async def test_cancel_edit_restores_the_same_version_with_a_new_token(s: Scenario) -> None:
    row = await s.seed()
    await press_action(s, row.id, "ed")
    editing = await s.action(row.id)
    assert editing.approval_token is None
    await press(s, button_token(s, editing, "cx"), "cx")
    back = await s.action(row.id)
    assert (back.state, back.version, back.text) == ("awaiting_approval", 1, DRAFT)
    assert back.approval_token
    assert back.approval_token != row.approval_token
    assert await edit_inputs(s) == []
    assert button_token(s, back, "ok") == back.approval_token


async def test_the_revising_reconciler_redefers_with_the_stored_revision_turn(
    s: Scenario,
) -> None:
    row = await s.seed()
    editing = await start_edit(s, row, "Synthetic: change it.")
    turn_id = editing.revision_turn_id

    async def crash_queued() -> None:
        async with transaction(s.env.factory) as uow:
            await uow.session.execute(
                text(
                    "UPDATE procrastinate.procrastinate_jobs SET status = 'failed' "
                    "WHERE task_name = 'replies.revise' AND status = 'todo'"
                )
            )

    await crash_queued()
    await advance(s, 4 * 60)
    await s.handlers.reconcile_pending()
    assert [job["status"] for job in await jobs(s, "replies.revise")] == ["failed"]
    await advance(s, 2 * 60)
    await s.handlers.reconcile_pending()
    redeferred = await jobs(s, "replies.revise")
    assert [job["status"] for job in redeferred] == ["failed", "todo"]
    assert redeferred[1]["args"] == {"action_id": row.id, "turn_id": turn_id}
    # Re-deferred once: a second crash is not retried again ...
    await crash_queued()
    await advance(s, 10 * 60)
    await s.handlers.reconcile_pending()
    assert len(await jobs(s, "replies.revise")) == 2
    assert (await s.action(row.id)).state == "revising"
    # ... and after thirty minutes the revision is given up.
    await advance(s, 20 * 60)
    await s.handlers.reconcile_pending()
    given_up = await s.action(row.id)
    assert (given_up.state, given_up.version, given_up.text) == ("awaiting_approval", 1, DRAFT)
    assert given_up.approval_token
    assert given_up.approval_token != row.approval_token
    assert "took too long" in s.control_text()


# ------------------------------------------------------------ reject, re-draft
async def test_reject_ends_the_card(s: Scenario) -> None:
    row = await s.seed()
    await press_action(s, row.id, "rj")
    final = await s.action(row.id)
    assert (final.state, final.state_reason) == ("rejected", "owner_rejected")
    card = s.final_card(final)
    assert "Rejected" in card["text"]
    assert card["reply_markup"] is None
    await press(s, row.approval_token or "", "ok")
    assert (await s.action(row.id)).state == "rejected"


async def test_redraft_is_deferred_in_the_press_transaction_without_a_flag(s: Scenario) -> None:
    row = await s.seed()
    await deliver(s.env, business_message(message_id=102, date=s.date + 1))
    outdated = await s.action(row.id)
    assert outdated.state == "outdated"
    await press(s, button_token(s, outdated, "rd"), "rd")
    assert answers(s)[-1] == ANSWER_REDRAFTING
    [job] = await jobs(s, "replies.draft")
    assert job["args"] == {"conversation_id": s.conversation_id, "reason": "redraft"}
    assert job["queueing_lock"] == f"redraft:{s.conversation_id}"
    async with s.env.factory() as session:
        flag = await session.scalar(
            select(Conversation.pending_draft_at).where(Conversation.id == s.conversation_id)
        )
    assert flag is None
    # The old card keeps its final state; Re-draft is gone, copy stays.
    card = s.final_card(outdated)
    assert "Re-draft requested" in card["text"]
    assert not any(b.get("callback_data") for b in s.card_buttons(outdated))
    s.assert_copy_ready(outdated)


async def test_redraft_on_an_outdated_card_requests_a_new_draft(s: Scenario) -> None:
    row = await s.seed()
    await deliver(s.env, business_message(message_id=102, date=s.date + 1))
    outdated = await s.action(row.id)
    await press(s, button_token(s, outdated, "rd"), "rd")
    s.fake.parsed.append(draft_result(text="Synthetic fresh draft."))
    await run_queued(s, "replies.draft")
    old, new = await s.rows()
    assert (old.id, old.state) == (row.id, "outdated")
    assert (new.state, new.text, new.origin) == (
        "awaiting_approval",
        "Synthetic fresh draft.",
        "agent_proposal",
    )
    assert new.card_message_id is not None
    assert new.card_message_id != old.card_message_id


async def test_a_second_redraft_tap_while_one_is_queued_keeps_the_press_transaction_alive(
    s: Scenario,
) -> None:
    """Two outdated cards of one chat: the second Re-draft meets the first's
    queueing lock. Only its SAVEPOINT rolls back, so the press still commits
    (its single-use card token is spent) and is answered (spec §3.3, R17)."""
    first = await s.seed()
    await deliver(s.env, business_message(message_id=102, date=s.date + 1))
    second = await s.seed(approval_token="QAseed5678")
    await deliver(s.env, business_message(message_id=103, date=s.date + 2))
    first, second = await s.action(first.id), await s.action(second.id)
    assert (first.state, second.state) == ("outdated", "outdated")
    first_token, second_token = button_token(s, first, "rd"), button_token(s, second, "rd")
    await press(s, first_token, "rd")
    await press(s, second_token, "rd")
    assert answers(s) == [ANSWER_REDRAFTING, ANSWER_REDRAFTING]
    [job] = await jobs(s, "replies.draft")
    assert job["queueing_lock"] == f"redraft:{s.conversation_id}"
    # The second press committed: its token is spent, its card says so.
    await press(s, second_token, "rd")
    assert answers(s)[-1] == CALLBACK_EXPIRED_TEXT
    assert "Re-draft requested" in s.final_card(second)["text"]


# ------------------------------------------------------------ expiry
@pytest.mark.parametrize("closed", [True, False])
async def test_expiry_at_window_close_and_at_max_age(s: Scenario, closed: bool) -> None:
    if closed:
        await advance(s, 23 * 3600 + 50 * 60)
    # Expires 23 h 55 min after the contact wrote: the window (less its
    # 5-minute margin) is closed then.
    row = await s.seed(expires_at=s.env.clock.now() + timedelta(minutes=5))
    await press_action(s, row.id, "ed")  # a revising card expires too
    assert len(await edit_inputs(s)) == 1
    await advance(s, 60, periodic=("system.expire_actions",))
    assert (await s.action(row.id)).state == "revising"
    await advance(s, 300, periodic=("system.expire_actions",))
    final = await s.action(row.id)
    reason = "window_closed" if closed else "max_age"
    assert (final.state, final.state_reason) == ("expired", reason)
    assert await edit_inputs(s) == []
    assert "Expired" in s.final_card(final)["text"]
    s.assert_copy_ready(final)
    assert s.business_sends() == []


async def test_expiry_clears_only_the_expired_actions_own_edit_question(s: Scenario) -> None:
    """Invariant 12: the expiry job deletes the Edit question of the account
    whose action expired, never another account's row with the same ref."""
    row = await s.seed(expires_at=s.env.clock.now() + timedelta(minutes=1))
    await press_action(s, row.id, "ed")
    async with transaction(s.env.factory) as uow:
        other, _ = await service.get_or_create_account(
            uow.session, telegram_user_id=7000000078, clock=s.env.clock
        )
        await uow.session.flush()
        await service.set_pending_input(
            uow.session, account_id=other.id, kind=DRAFT_EDIT, clock=s.env.clock, ref_id=row.id
        )
        other_id = other.id
    await advance(s, 120, periodic=("system.expire_actions",))
    assert (await s.action(row.id)).state == "expired"
    assert await edit_inputs(s) == []
    async with s.env.factory() as session:
        left = list(
            await session.scalars(
                select(PendingInput.account_id).where(
                    PendingInput.kind == DRAFT_EDIT, PendingInput.ref_id == row.id
                )
            )
        )
    assert left == [other_id]


# ------------------------------------------------------------ discipline
async def test_state_changes_go_through_s04_1_transitions_only(s: Scenario) -> None:
    row = await s.seed()
    s.fake.parsed.append(draft_result(text="Synthetic edited.", revision_kind="instruction"))
    await start_edit(s, row, "Synthetic: change it.")
    await run_queued(s, "replies.revise")
    await press_action(s, row.id, "ed")
    await press(s, button_token(s, await s.action(row.id), "cx"), "cx")
    await press_action(s, row.id, "rj")
    assert await s.events(row.id) == [
        "action_revision_requested",
        "action_edited",
        "action_revision_requested",
        "action_revision_cancelled",
        "action_rejected",
    ]
    second = await s.seed()
    await press_action(s, second.id, "ok")
    assert await s.events(second.id) == ["action_approved"]
    # The module writes no state of its own: no `state=` in any UPDATE.
    tree = ast.parse(inspect.getsource(approvals))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "values":
            assert not {kw.arg for kw in node.keywords} & {"state", "state_reason"}


async def test_every_press_is_answered_and_telegram_calls_run_after_commit(s: Scenario) -> None:
    row = await s.seed()
    seen: list[str | None] = []

    async def observe() -> None:
        # A separate session sees only committed rows.
        async with s.env.factory() as session:
            seen.append(await session.scalar(select(Action.state).where(Action.id == row.id)))

    s.env.sender.on_call = observe
    await press_action(s, row.id, "ed")
    assert seen
    assert set(seen) == {"revising"}
    seen.clear()
    await press(s, button_token(s, await s.action(row.id), "cx"), "cx")
    assert seen
    assert set(seen) == {"awaiting_approval"}
    seen.clear()
    await press_action(s, row.id, "ok")
    assert seen
    assert seen[0] == "approved"
    s.env.sender.on_call = None
    await press(s, "Zz9Zz9Zz9Z", "ok")
    assert len(answers(s)) == 4


async def test_a_lost_card_of_another_account_is_never_shown_to_this_owner(
    s: Scenario,
) -> None:
    """Card rendering reads one account's rows (invariant 12)."""
    row = await s.seed()
    async with s.env.factory() as session:
        view = await approvals.load_card_view(
            session, account_id=s.account_id + 999, action_id=row.id, sending_enabled=True
        )
        mine = await approvals.load_card_view(
            session, account_id=s.account_id, action_id=row.id, sending_enabled=True
        )
        account = await session.get(Account, s.account_id)
    assert view is None
    assert mine is not None
    assert account is not None
    assert mine.control_chat_id == account.control_chat_id
    assert mine.contact_name == "Synthetic"
    assert mine.excerpts == ("synthetic contact text",)


async def test_an_away_message_does_not_cut_the_cards_excerpts(s: Scenario) -> None:
    """An automated owner message (away or greeting) answers nothing (M03),
    so the contact's message before it is still among what the card answers."""
    away = {"is_from_offline": True}
    await deliver(
        s.env,
        business_message(
            from_id=OWNER, message_id=102, date=s.date + 1, message_text="SYNTH-AWAY", extra=away
        ),
    )
    second = "Synthetic second question?"
    await deliver(s.env, business_message(message_id=103, date=s.date + 2, message_text=second))
    row = await s.seed()
    async with s.env.factory() as session:
        view = await approvals.load_card_view(
            session, account_id=s.account_id, action_id=row.id, sending_enabled=True
        )
    assert view is not None
    assert view.excerpts == ("synthetic contact text", second)
