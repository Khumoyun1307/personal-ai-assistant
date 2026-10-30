"""Verified, at-most-once execution (S04.4 §3.2; ARCHITECTURE §9.4).

The real executor, gateway and Postgres; Telegram is `FakeTelegramSender`,
scripted per test. Each test asserts what reached Telegram (every argument
is recorded) and what the action, its audit trail, the card and the owner
were told. RG-05, RG-06, RG-18, RG-19 (owner manual message) and RG-20 live
here (IMPLEMENTATION_PLAN §10). Texts are synthetic.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select, text

from assistant.accounts import connections
from assistant.accounts.connections import ConnectionSnapshot, Source
from assistant.accounts.models import Account, AuditLogEntry, BusinessConnection, Consent
from assistant.actions.executor import (
    MAX_RATE_LIMITED_ATTEMPTS,
    ExecuteStatus,
    mark_stale_attempts,
)
from assistant.actions.lifecycle import clear_texts, text_hash
from assistant.actions.models import Action
from assistant.actions.states import (
    STATE_LABELS,
    STATE_REASON_EXPLANATIONS,
    ActionOrigin,
    ActionState,
    StateReason,
)
from assistant.conversations.models import Conversation, Message
from assistant.conversations.service import record_ingest_failure
from assistant.core.delivery import MAX_TEXT_LENGTH
from assistant.core.jobs import EXECUTE_TASK
from assistant.inbox.models import InboundUpdate
from assistant.inbox.notify import ALERT_TEXT, AlertKind
from assistant.jobs.reconcile_actions import unqueued_executions
from tests.fakes.sender import network_error, telegram_error
from tests.integration.actions_env import seed_turn
from tests.integration.business_env import Env
from tests.integration.execution_env import (
    Harness,
    action,
    add_message_now,
    approved,
    caught_up,
    connection,
    conversation,
    events,
    harness,
    inserted,
    second_chat,
    seed,
    sent_rows,
    update_rows,
)

pytestmark = pytest.mark.integration

TEXT = "Synthetic approved reply"


def too_many(retry_after: int | None = None) -> Exception:
    return telegram_error(
        "Too Many Requests: retry later", status_code=429, retry_after=retry_after
    )


def defers(h: Harness) -> list[Any]:
    return [item for item in h.scheduler.deferred if item.task == EXECUTE_TASK]


# ------------------------------------------------------------------ happy path
async def test_approved_text_is_sent_exactly_once_and_recorded(env: Env) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)

    outcome = await h.execute(action_id)
    again = await h.execute(action_id)

    assert outcome.status is ExecuteStatus.SENT
    assert again.status is ExecuteStatus.SETTLED
    assert [call["text"] for call in h.sender.plain_sends] == [TEXT]
    row = await action(env, action_id)
    assert row.state == ActionState.SUCCEEDED.value
    assert row.state_reason == StateReason.SENT.value
    assert row.sent_text_hash == row.approved_text_hash == text_hash(TEXT)
    assert row.telegram_message_id is not None
    assert row.executed_at == env.clock.now()
    messages = await sent_rows(env, chat.conversation_id)
    assert len(messages) == 1
    assert messages[0].action_id == action_id
    assert messages[0].telegram_message_id == row.telegram_message_id
    assert messages[0].text == TEXT
    chat_row = await conversation(env, chat.conversation_id)
    assert chat_row.last_assistant_msg_at == env.clock.now()
    assert action_id in h.presenter.synced
    assert h.channel.sends == []  # no alert for a confirmed send


# ------------------------------------------------------------------ RG tests
class Killed(BaseException):
    """The process dies: nothing in the executor catches this."""


async def test_rg05_restart_in_executing_is_uncertain_never_resent(env: Env) -> None:
    """RG-05 (invariant 6): killed between the committed claim and the send,
    the action stays `executing`; after a restart nothing resends it, and
    `mark_uncertain` settles it as `delivery_uncertain` and tells the owner."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)

    async def die() -> None:
        raise Killed

    h.sender.on_call = die
    with pytest.raises(Killed):
        await h.execute(action_id)
    assert (await action(env, action_id)).state == ActionState.EXECUTING.value

    restarted = harness(env)
    assert (await restarted.execute(action_id)).status is ExecuteStatus.SETTLED
    async with env.factory() as session:
        assert await unqueued_executions(session, env.clock.now() + timedelta(minutes=5)) == []

    env.clock.advance(seconds=CONFIG_THRESHOLD_SECONDS + 1)
    assert await restarted.executor.mark_uncertain() == 1
    assert await restarted.executor.mark_uncertain() == 0
    assert (await restarted.execute(action_id)).status is ExecuteStatus.SETTLED

    row = await action(env, action_id)
    assert row.state == ActionState.DELIVERY_UNCERTAIN.value
    assert row.state_reason == StateReason.STALE_EXECUTING.value
    assert h.sender.business_calls == restarted.sender.business_calls == 0
    assert len(restarted.channel.sends) == 1
    assert "not sure" in restarted.channel.sends[0]
    assert action_id in restarted.presenter.synced


#: The test config's threshold (`actions_env.CONFIG.mark_uncertain_after`).
CONFIG_THRESHOLD_SECONDS = 120


async def test_rg06_a_stale_draft_never_sends(env: Env) -> None:
    """RG-06 (invariant 1): a contact message after the approval outdates the
    action at Tx A; Telegram is never called."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    env.clock.advance(seconds=30)
    await add_message_now(env, chat, direction="contact")

    outcome = await h.execute(action_id)

    assert outcome.status is ExecuteStatus.DENIED
    row = await action(env, action_id)
    assert (row.state, row.state_reason) == ("outdated", "new_contact_message")
    assert h.sender.business_calls == 0
    assert action_id in h.presenter.synced


PRECONDITIONS = (
    "rights_missing",
    "not_enabled",
    "unknown_conversation",
    "missing_consent",
    "ignore_mode",
    "sending_disabled",
)


@pytest.mark.parametrize("precondition", PRECONDITIONS)
async def test_rg18_no_telegram_call_when_a_precondition_fails(env: Env, precondition: str) -> None:
    """RG-18 (invariants 1, 7, 8): the Tx A re-check runs on fresh rows; any
    failed send precondition means no Telegram call at all."""
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    h = harness(env, sending_enabled=precondition != "sending_disabled")
    match precondition:
        case "rights_missing":
            await update_rows(
                env,
                BusinessConnection,
                BusinessConnection.account_id == chat.account_id,
                rights=[],
                capability_tier="observe_only",
            )
        case "not_enabled":
            await update_rows(
                env,
                BusinessConnection,
                BusinessConnection.account_id == chat.account_id,
                is_enabled=False,
            )
        case "unknown_conversation":
            other = await seed(env)
            await update_rows(
                env, Action, Action.id == action_id, conversation_id=other.conversation_id
            )
        case "missing_consent":
            await update_rows(
                env, Consent, Consent.account_id == chat.account_id, revoked_at=env.clock.now()
            )
        case "ignore_mode":
            await update_rows(
                env, Conversation, Conversation.id == chat.conversation_id, mode="ignore"
            )

    outcome = await h.execute(action_id)

    assert outcome.status is ExecuteStatus.DENIED
    assert outcome.state in {ActionState.BLOCKED, ActionState.EXPIRED, ActionState.OUTDATED}
    assert h.sender.business_calls == 0
    assert h.sender.sends == []
    row = await action(env, action_id)
    assert row.state == outcome.state.value
    assert row.policy_decision is not None
    assert row.policy_decision["phase"] == "execution"


async def close_window_estimate(env: Env, chat: Any) -> None:
    """Our own estimate says closed: the contact's last message is 25 h old."""
    await update_rows(
        env,
        Conversation,
        Conversation.id == chat.conversation_id,
        last_contact_msg_at=env.clock.now() - timedelta(hours=25),
    )


async def test_rg18_an_autonomous_send_with_the_window_closed_never_reaches_telegram(
    env: Env,
) -> None:
    """RG-18 (invariant 8, as amended 2026-10-30, P2-O20): autonomous sends
    keep the conservative pre-check: a closed window means no Telegram call."""
    chat = await seed(env)
    await caught_up(env)
    action_id = await inserted(env, chat, origin=ActionOrigin.DELEGATION_AUTO)
    await close_window_estimate(env, chat)
    h = harness(env)

    outcome = await h.execute(action_id)

    assert outcome.status is ExecuteStatus.DENIED
    assert (outcome.state, outcome.reason) == (ActionState.EXPIRED, StateReason.WINDOW_CLOSED)
    assert h.sender.business_calls == 0
    row = await action(env, action_id)
    assert row.policy_decision is not None
    failed = [c["reason"] for c in row.policy_decision["checks"] if not c["passed"]]
    assert "window_closed" in failed


@pytest.mark.parametrize("origin", [ActionOrigin.OWNER_DICTATED, ActionOrigin.AGENT_PROPOSAL])
async def test_rg18_an_owner_approved_send_with_the_window_closed_is_attempted_once(
    env: Env, origin: ActionOrigin
) -> None:
    """RG-18 (invariant 8, as amended 2026-10-30, P2-O20): our estimate does
    not block an owner-approved send; Telegram decides. Accepted: sent."""
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT, origin=origin)
    await close_window_estimate(env, chat)
    h = harness(env)

    outcome = await h.execute(action_id)

    assert outcome.status is ExecuteStatus.SENT
    assert (outcome.state, outcome.reason) == (ActionState.SUCCEEDED, StateReason.SENT)
    assert len(h.sender.plain_sends) == 1
    row = await action(env, action_id)
    assert row.policy_decision is not None
    assert "window_open" not in [c["name"] for c in row.policy_decision["checks"]]


async def test_rg18_telegram_refusing_the_window_is_a_definite_not_sent(env: Env) -> None:
    """RG-18 (P2-O20): with our estimate closed, Telegram's `PEER_ID_INVALID`
    ends the action `expired`/`window_closed` after exactly one attempt,
    never `delivery_uncertain`, never retried, and the card is re-rendered
    (copy-ready text: `test_approval_cards`)."""
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT, origin=ActionOrigin.OWNER_DICTATED)
    await close_window_estimate(env, chat)
    h = harness(env)
    h.sender.plain_script = [telegram_error("Bad Request: PEER_ID_INVALID", status_code=400)]

    outcome = await h.execute(action_id)
    again = await h.execute(action_id)

    assert outcome.status is ExecuteStatus.NOT_SENT
    assert (outcome.state, outcome.reason) == (ActionState.EXPIRED, StateReason.WINDOW_CLOSED)
    assert again.status is ExecuteStatus.SETTLED
    row = await action(env, action_id)
    assert (row.state, row.state_reason) == ("expired", "window_closed")
    assert (row.error_class, row.error_code) == ("window_closed", "peer_id_invalid")
    assert row.text == TEXT  # kept for the copy-ready card
    assert len(h.sender.plain_sends) == 1
    assert defers(h) == []
    assert action_id in h.presenter.synced


async def test_with_sending_off_an_approved_action_is_blocked_with_copy_ready_text(
    env: Env,
) -> None:
    """P2-T21: flag `actions.send` off — Approve ends `blocked` /
    `sending_disabled`, the text stays for the owner to copy, the card syncs."""
    h = harness(env, sending_enabled=False)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)

    outcome = await h.execute(action_id)

    assert (outcome.state, outcome.reason) == (ActionState.BLOCKED, StateReason.SENDING_DISABLED)
    row = await action(env, action_id)
    assert (row.state, row.state_reason, row.text) == ("blocked", "sending_disabled", TEXT)
    assert h.presenter.synced == [action_id]
    assert h.sender.business_calls == 0


async def test_rg19_an_owner_manual_message_blocks_the_send(env: Env) -> None:
    """RG-19 (invariant 10): the owner wrote in the chat after the draft's
    base message — the takeover outdates the approved action at Tx A."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    env.clock.advance(seconds=20)
    await add_message_now(env, chat, direction="owner_manual")

    outcome = await h.execute(action_id)

    assert (outcome.state, outcome.reason) == (ActionState.OUTDATED, StateReason.TAKEOVER)
    assert h.sender.business_calls == 0


# ------------------------------------------------------------------ outcomes
@pytest.mark.parametrize(("retry_after", "expected"), [(7, 7), (None, 3)])
async def test_a_rate_limited_send_returns_to_approved_and_retries_after_retry_after(
    env: Env, retry_after: int | None, expected: int
) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    h.sender.plain_script = [too_many(retry_after)]

    first = await h.execute(action_id)

    assert first.status is ExecuteStatus.RATE_LIMITED
    row = await action(env, action_id)
    retry_at = env.clock.now() + timedelta(seconds=expected)
    assert (row.state, row.attempt_id, row.retry_at) == ("approved", None, retry_at)
    assert [item.schedule_at for item in defers(h)] == [retry_at]
    assert all(item.lock is None and item.queueing_lock is None for item in defers(h))

    early = await h.execute(action_id)
    assert early.status is ExecuteStatus.NOT_YET
    assert len(h.sender.plain_sends) == 1

    env.clock.advance(seconds=expected)
    assert (await h.execute(action_id)).status is ExecuteStatus.SENT
    assert len(h.sender.plain_sends) == 2
    assert (await action(env, action_id)).state == "succeeded"


async def test_rate_limiting_gives_up_after_five_attempts(env: Env) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    h.sender.plain_script = [too_many(1) for _ in range(MAX_RATE_LIMITED_ATTEMPTS)]

    outcomes = []
    for _ in range(MAX_RATE_LIMITED_ATTEMPTS):
        outcomes.append(await h.execute(action_id))
        env.clock.advance(seconds=2)

    assert [o.status for o in outcomes[:-1]] == [ExecuteStatus.RATE_LIMITED] * 4
    assert outcomes[-1].status is ExecuteStatus.NOT_SENT
    row = await action(env, action_id)
    assert (row.state, row.state_reason) == ("failed", "not_delivered")
    assert len(h.sender.plain_sends) == MAX_RATE_LIMITED_ATTEMPTS
    assert len(defers(h)) == MAX_RATE_LIMITED_ATTEMPTS - 1
    assert (await h.execute(action_id)).status is ExecuteStatus.SETTLED


async def test_a_window_closed_send_expires_and_is_not_retried(env: Env) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    h.sender.plain_script = [telegram_error("Bad Request: PEER_ID_INVALID", status_code=400)]

    outcome = await h.execute(action_id)
    again = await h.execute(action_id)

    assert (outcome.state, outcome.reason) == (ActionState.EXPIRED, StateReason.WINDOW_CLOSED)
    assert again.status is ExecuteStatus.SETTLED
    row = await action(env, action_id)
    assert (row.error_class, row.error_code) == ("window_closed", "peer_id_invalid")
    assert len(h.sender.plain_sends) == 1
    assert defers(h) == []


async def test_peer_invalid_fails_and_marks_the_chat_possibly_paused(env: Env) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    h.sender.plain_script = [telegram_error("Bad Request: BUSINESS_PEER_INVALID", status_code=400)]

    outcome = await h.execute(action_id)

    assert (outcome.state, outcome.reason) == (ActionState.FAILED, StateReason.NOT_DELIVERED)
    assert (await conversation(env, chat.conversation_id)).availability == "possibly_paused"
    assert defers(h) == []


@pytest.mark.parametrize(
    "error",
    [
        telegram_error("Forbidden: bot was blocked by the user", status_code=403),
        telegram_error("Bad Request: message text is empty", status_code=400),
    ],
    ids=["forbidden", "bad_request"],
)
async def test_forbidden_and_bad_request_fail_without_retry(env: Env, error: Exception) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    h.sender.plain_script = [error]

    outcome = await h.execute(action_id)
    again = await h.execute(action_id)

    assert (outcome.state, outcome.reason) == (ActionState.FAILED, StateReason.NOT_DELIVERED)
    assert again.status is ExecuteStatus.SETTLED
    assert len(h.sender.plain_sends) == 1
    assert defers(h) == []
    assert h.channel.sends == []  # a definite failure is not "uncertain"


async def test_connection_invalid_marks_the_connection_stale_and_blocks_other_sends(
    env: Env,
) -> None:
    h = harness(env)
    chat = await seed(env)
    other = await second_chat(env, chat)
    await caught_up(env)
    first = await approved(env, chat, text=TEXT)
    second = await approved(env, other, text=TEXT)
    h.sender.plain_script = [
        telegram_error("Bad Request: BUSINESS_CONNECTION_INVALID", status_code=400)
    ]

    failed = await h.execute(first)
    blocked = await h.execute(second)

    assert (failed.state, failed.reason) == (ActionState.FAILED, StateReason.CONNECTION_INVALID)
    assert (await connection(env, chat.account_id)).status == "stale"
    assert blocked.status is ExecuteStatus.DENIED
    assert blocked.state is ActionState.BLOCKED
    assert len(h.sender.plain_sends) == 1
    row = await action(env, second)
    assert row.policy_decision is not None
    failed_codes = {c["reason"] for c in row.policy_decision["checks"] if not c["passed"]}
    assert "connection_stale" in failed_codes


async def test_a_new_connection_id_reactivates_a_stale_connection(env: Env) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    used = (await connection(env, chat.account_id)).connection_id
    async with env.factory() as session, session.begin():
        assert await connections.mark_stale(
            session, chat.account_id, env.clock.now(), connection_id=used
        )
        owner = await session.get(Account, chat.account_id)
        assert owner is not None
        row = await session.scalar(
            select(BusinessConnection).where(BusinessConnection.account_id == chat.account_id)
        )
        assert row is not None
        snapshot = ConnectionSnapshot(
            connection_id="synthetic-new-id",
            user_id=owner.telegram_user_id,
            user_chat_id=row.user_chat_id,
            date=env.clock.now(),
            is_enabled=True,
            rights=frozenset({"can_reply"}),
            payload_digest="synthetic",
        )
        result = await connections.apply_snapshot(
            session,
            snapshot,
            owner_ids=frozenset({owner.telegram_user_id}),
            source=Source.UPDATE,
            clock=env.clock,
        )
    assert result.change is connections.Change.RECONNECTED
    assert (await connection(env, chat.account_id)).status == "active"

    action_id = await approved(env, chat, text=TEXT)
    assert (await h.execute(action_id)).status is ExecuteStatus.SENT
    assert h.sender.plain_sends[0]["business_connection_id"] == "synthetic-new-id"


async def reconnect(env: Env, account_id: int, connection_id: str) -> None:
    """The owner reconnects: a `business_connection` update with a new id
    (TF §12 LV-05), applied through the real registry."""
    async with env.factory() as session, session.begin():
        owner = await session.get(Account, account_id)
        row = await session.scalar(
            select(BusinessConnection).where(BusinessConnection.account_id == account_id)
        )
        assert owner is not None
        assert row is not None
        result = await connections.apply_snapshot(
            session,
            ConnectionSnapshot(
                connection_id=connection_id,
                user_id=owner.telegram_user_id,
                user_chat_id=row.user_chat_id,
                date=env.clock.now(),
                is_enabled=True,
                rights=frozenset({"can_reply"}),
                payload_digest="synthetic",
            ),
            owner_ids=frozenset({owner.telegram_user_id}),
            source=Source.UPDATE,
            clock=env.clock,
        )
    assert result.change is connections.Change.RECONNECTED


def connection_invalid() -> Exception:
    return telegram_error("Bad Request: BUSINESS_CONNECTION_INVALID", status_code=400)


async def test_a_connection_invalid_for_a_replaced_connection_leaves_the_replacement_active(
    env: Env,
) -> None:
    """Antony #1 on #107: the owner reconnects while a send on the old id is
    in flight, and that call then fails with BUSINESS_CONNECTION_INVALID.
    Only the connection the call actually used may become stale; the
    replacement stays active and the next send goes out on it."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    used = (await connection(env, chat.account_id)).connection_id
    action_id = await approved(env, chat, text=TEXT)
    h.sender.plain_script = [connection_invalid()]

    async def reconnect_meanwhile() -> None:
        h.sender.on_call = None
        await reconnect(env, chat.account_id, "synthetic-replacement")

    h.sender.on_call = reconnect_meanwhile

    outcome = await h.execute(action_id)

    assert (outcome.state, outcome.reason) == (ActionState.FAILED, StateReason.CONNECTION_INVALID)
    assert h.sender.plain_sends[0]["business_connection_id"] == used
    current = await connection(env, chat.account_id)
    assert (current.connection_id, current.status) == ("synthetic-replacement", "active")
    async with env.factory() as session:
        marked = await session.scalar(
            select(AuditLogEntry).where(
                AuditLogEntry.account_id == chat.account_id,
                AuditLogEntry.event == "connection_marked_stale",
            )
        )
    assert marked is None
    following = await approved(env, chat, text=TEXT)
    assert (await h.execute(following)).status is ExecuteStatus.SENT
    assert h.sender.plain_sends[1]["business_connection_id"] == "synthetic-replacement"


@pytest.mark.parametrize("reconnected", [True, False], ids=["reconnected", "same_connection"])
async def test_a_late_connection_invalid_marks_only_the_connection_it_used_stale(
    env: Env, reconnected: bool
) -> None:
    """Viro on #107: a late BUSINESS_CONNECTION_INVALID arrives after
    `mark_uncertain`, when a reconnect has most likely landed. It fails the
    action (decisions R4) and stales the connection it was sent on only
    while that id is still the account's active one."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    used = (await connection(env, chat.account_id)).connection_id
    action_id = await approved(env, chat, text=TEXT)
    h.sender.plain_script = [connection_invalid()]

    async def sweep_then_maybe_reconnect() -> None:
        h.sender.on_call = None
        env.clock.advance(seconds=CONFIG_THRESHOLD_SECONDS + 1)
        await h.executor.mark_uncertain()
        if reconnected:
            await reconnect(env, chat.account_id, "synthetic-replacement")

    h.sender.on_call = sweep_then_maybe_reconnect

    outcome = await h.execute(action_id)

    assert (outcome.state, outcome.reason) == (ActionState.FAILED, StateReason.NOT_DELIVERED)
    assert h.sender.plain_sends[0]["business_connection_id"] == used
    current = await connection(env, chat.account_id)
    if reconnected:
        assert (current.connection_id, current.status) == ("synthetic-replacement", "active")
    else:
        assert (current.connection_id, current.status) == (used, "stale")


async def test_an_ambiguous_send_is_uncertain_alerts_once_and_is_never_retried(
    env: Env,
) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    h.sender.plain_script = [network_error()]

    outcome = await h.execute(action_id)

    assert (outcome.state, outcome.reason) == (
        ActionState.DELIVERY_UNCERTAIN,
        StateReason.AMBIGUOUS,
    )
    assert (await h.execute(action_id)).status is ExecuteStatus.SETTLED
    env.clock.advance(minutes=10)
    assert await h.executor.mark_uncertain() == 0
    async with env.factory() as session:
        assert await unqueued_executions(session, env.clock.now()) == []
    assert len(h.sender.plain_sends) == 1
    assert defers(h) == []
    assert len(h.channel.sends) == 1
    assert h.channel.sends[0].startswith("⚠️ I'm not sure a reply reached")


async def _mark_uncertain_during_the_send(h: Harness) -> None:
    """While Telegram is being called: the attempt runs past the threshold
    and the sweep settles it (a slow request, or a sweep after a restart)."""

    async def sweep() -> None:
        h.sender.on_call = None
        h.env.clock.advance(seconds=CONFIG_THRESHOLD_SECONDS + 1)
        await h.executor.mark_uncertain()

    h.sender.on_call = sweep


async def test_a_late_result_after_mark_uncertain_settles_the_action_and_updates_the_card(
    env: Env,
) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    await _mark_uncertain_during_the_send(h)

    outcome = await h.execute(action_id)

    assert outcome.status is ExecuteStatus.SENT
    row = await action(env, action_id)
    assert row.state == "succeeded"
    assert [entry.event for entry in await events(env, action_id)][-2:] == [
        "action_delivery_uncertain",
        "action_late_result",
    ]
    assert h.presenter.synced.count(action_id) >= 2  # uncertain, then sent
    assert len(await sent_rows(env, chat.conversation_id)) == 1


@pytest.mark.parametrize(
    "error",
    [too_many(5), telegram_error("Bad Request: PEER_ID_INVALID", status_code=400)],
    ids=["rate_limited", "window_closed"],
)
async def test_a_late_429_or_window_closed_after_mark_uncertain_fails_and_is_never_retried(
    env: Env, error: Exception
) -> None:
    """Decisions R4: a late definite result only ever fails the action."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    h.sender.plain_script = [error]
    await _mark_uncertain_during_the_send(h)

    outcome = await h.execute(action_id)

    assert (outcome.state, outcome.reason) == (ActionState.FAILED, StateReason.NOT_DELIVERED)
    assert defers(h) == []
    assert (await h.execute(action_id)).status is ExecuteStatus.SETTLED
    assert len(h.sender.plain_sends) == 1
    assert (await action(env, action_id)).retry_at is None


async def test_executor_transitions_write_their_mapped_events(env: Env) -> None:
    """Decisions R15: the claim, the 429 and the stale connection each write
    their mapped, content-free audit event."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    h.sender.plain_script = [
        too_many(1),
        telegram_error("Bad Request: BUSINESS_CONNECTION_INVALID", status_code=400),
    ]

    await h.execute(action_id)
    env.clock.advance(seconds=1)
    await h.execute(action_id)

    trail = await events(env, action_id)
    names = [entry.event for entry in trail]
    assert names == [
        "action_proposed",
        "action_approved",
        "action_claimed",
        "action_rate_limited",
        "action_claimed",
        "action_failed",
    ]
    claims = [entry for entry in trail if entry.event == "action_claimed"]
    assert all(entry.details["attempt_id"] for entry in claims)
    assert claims[0].details["attempt_id"] != claims[1].details["attempt_id"]
    limited = trail[3].details
    assert limited["attempt_id"] == claims[0].details["attempt_id"]
    assert limited["retry_after"] == 1
    assert trail[5].details["attempt_id"] == claims[1].details["attempt_id"]
    async with env.factory() as session:
        stale = await session.scalar(
            select(AuditLogEntry).where(AuditLogEntry.event == "connection_marked_stale")
        )
    assert stale is not None
    assert stale.subject_type == "connection"
    for entry in trail:
        assert TEXT not in str(entry.details)


async def test_an_echo_processed_before_tx_b_leaves_one_message_row(env: Env) -> None:
    """LV-03 found no echo, but if one is ingested before Tx B, the upsert
    absorbs it: one `assistant_sent` row, now linked to the action."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    h.sender.plain_script = [4242]

    async def echo() -> None:
        async with env.factory() as session, session.begin():
            session.add(
                Message(
                    account_id=chat.account_id,
                    conversation_id=chat.conversation_id,
                    telegram_message_id=4242,
                    direction="assistant_sent",
                    sent_at=env.clock.now(),
                    content_type="text",
                    text=TEXT,
                )
            )

    h.sender.on_call = echo
    await h.execute(action_id)

    rows = await sent_rows(env, chat.conversation_id)
    assert [(row.telegram_message_id, row.action_id) for row in rows] == [(4242, action_id)]


async def test_two_identity_pending_sends_in_one_chat_both_succeed(env: Env) -> None:
    """`message_id == 0` (ARCHITECTURE §6.8): succeeded, id NULL, identity
    pending — and NULLs are distinct, so two in one chat both record."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    first = await approved(env, chat, text=TEXT)
    second = await approved(env, chat, text=TEXT + " again")
    h.sender.plain_script = [0, 0]

    assert (await h.execute(first)).status is ExecuteStatus.SENT
    assert (await h.execute(second)).status is ExecuteStatus.SENT

    for action_id in (first, second):
        row = await action(env, action_id)
        assert (row.state, row.telegram_message_id, row.identity_pending) == (
            "succeeded",
            None,
            True,
        )
        identity = [
            e.details for e in await events(env, action_id) if e.event == "action_succeeded"
        ]
        assert identity[0]["identity_pending"] is True
    rows = await sent_rows(env, chat.conversation_id)
    assert [(row.telegram_message_id, row.action_id) for row in rows] == [
        (None, first),
        (None, second),
    ]


async def test_an_over_long_approved_text_is_blocked_with_no_partial_send(env: Env) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await inserted(
        env, chat, origin=ActionOrigin.OWNER_DICTATED, text="x" * (MAX_TEXT_LENGTH + 904)
    )

    outcome = await h.execute(action_id)

    assert (outcome.state, outcome.reason) == (ActionState.BLOCKED, StateReason.TOO_LONG)
    assert h.sender.business_calls == 0


async def test_two_execute_jobs_for_one_action_send_once(env: Env) -> None:
    """Two jobs at once (a double defer, a reconciler re-defer): Tx A's row
    lock and conditional claim let exactly one send."""
    h = harness(env)
    other = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)

    results = await asyncio.gather(h.execute(action_id), other.execute(action_id))

    assert sorted(result.status.value for result in results) == ["sent", "settled"]
    assert len(h.sender.plain_sends) + len(other.sender.plain_sends) == 1


async def test_a_lost_execute_job_is_recreated_by_the_reconciler_and_sends_once(
    env: Env,
) -> None:
    """Crash after the approval's commit, before its defer: the reconciler
    finds the approved action with no job and re-defers it; it sends once."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)  # no job deferred
    async with env.factory() as session:
        assert await unqueued_executions(session, env.clock.now()) == []  # grace
    env.clock.advance(seconds=11)
    async with env.factory() as session:
        requests = await unqueued_executions(session, env.clock.now())
    assert [(r.task, dict(r.args), r.lock, r.queueing_lock) for r in requests] == [
        (EXECUTE_TASK, {"action_id": action_id}, None, None)
    ]
    async with env.factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO procrastinate.procrastinate_jobs (queue_name, task_name, args) "
                "VALUES ('outbound', :task, CAST(:args AS jsonb))"
            ),
            {"task": EXECUTE_TASK, "args": f'{{"action_id": {action_id}}}'},
        )
    async with env.factory() as session:
        assert await unqueued_executions(session, env.clock.now()) == []  # queued now

    assert (await h.execute(action_id)).status is ExecuteStatus.SENT
    assert (await h.execute(action_id)).status is ExecuteStatus.SETTLED
    async with env.factory() as session:
        assert await unqueued_executions(session, env.clock.now() + timedelta(hours=1)) == []
    assert len(h.sender.plain_sends) == 1


async def test_a_late_ingested_owner_message_dated_after_the_base_blocks_the_send(
    env: Env,
) -> None:
    """Telegram dates on both sides: an owner message dated after the draft's
    base but ingested only after the action was made still blocks it."""
    h = harness(env)
    chat = await seed(env)  # the contact wrote an hour ago (the base)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    await add_message_now(
        env, chat, direction="owner_manual", sent_at=env.clock.now() - timedelta(minutes=30)
    )

    outcome = await h.execute(action_id)

    assert (outcome.state, outcome.reason) == (ActionState.OUTDATED, StateReason.TAKEOVER)
    assert h.sender.business_calls == 0


async def test_a_dead_lettered_update_outdates_the_chats_pending_actions(env: Env) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    chat_id = (await conversation(env, chat.conversation_id)).business_chat_id
    async with env.factory() as session, session.begin():
        update_row = InboundUpdate(
            bot_id=env.bot_id, update_id=987_654, kind="business_message", status="failed"
        )
        session.add(update_row)
        await session.flush()
        await record_ingest_failure(
            session,
            account_id=chat.account_id,
            business_chat_id=chat_id,
            telegram_user_id=chat_id,
            inbound_update_id=update_row.id,
            at=env.clock.now(),
            clock=env.clock,
        )

    row = await action(env, action_id)
    assert (row.state, row.state_reason) == ("outdated", "ingest_error")
    assert (await h.execute(action_id)).status is ExecuteStatus.SETTLED
    assert h.sender.business_calls == 0


async def test_tx_b_never_touches_a_row_of_another_attempt(env: Env) -> None:
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)

    async def another_attempt() -> None:
        await update_rows(env, Action, Action.id == action_id, attempt_id="another-attempt")

    h.sender.on_call = another_attempt
    outcome = await h.execute(action_id)

    assert outcome.status is ExecuteStatus.NO_MATCHING_ATTEMPT
    row = await action(env, action_id)
    assert (row.state, row.attempt_id, row.telegram_message_id) == (
        "executing",
        "another-attempt",
        None,
    )
    assert await sent_rows(env, chat.conversation_id) == []
    assert defers(h) == []


async def test_a_text_cleared_by_deletion_during_a_send_is_never_sent_again(env: Env) -> None:
    """P2-T37: a data deletion during the send clears the text and leaves
    `executing` alone; the 429 that follows must not make the NULL-text
    action executable again."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    h.sender.plain_script = [too_many(1)]

    async def delete_data() -> None:
        async with env.factory() as session, session.begin():
            await clear_texts(session, account_id=chat.account_id, conversation_ids=None)

    h.sender.on_call = delete_data
    outcome = await h.execute(action_id)
    env.clock.advance(seconds=5)
    again = await h.execute(action_id)

    assert (outcome.state, outcome.reason) == (ActionState.FAILED, StateReason.NOT_DELIVERED)
    assert again.status is ExecuteStatus.SETTLED
    assert defers(h) == []
    assert len(h.sender.plain_sends) == 1
    assert [e.event for e in await events(env, action_id)].count("action_claimed") == 1


async def test_an_approved_action_whose_text_was_deleted_is_never_claimed(env: Env) -> None:
    """P2-T37 at Tx A: `approved` with `text IS NULL` → `blocked` /
    `data_deleted` before any other rule."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    action_id = await approved(env, chat, text=TEXT)
    async with env.factory() as session, session.begin():
        await clear_texts(session, account_id=chat.account_id, conversation_ids=None)

    outcome = await h.execute(action_id)

    assert (outcome.state, outcome.reason) == (ActionState.BLOCKED, StateReason.DATA_DELETED)
    assert h.sender.business_calls == 0


async def test_rg20_uncertain_and_failed_outcomes_are_never_reported_as_sent(env: Env) -> None:
    """RG-20 (invariant 11): an uncertain or failed send is never shown or
    told as sent — not in its state, its card label and reason, nor the alert."""
    h = harness(env)
    chat = await seed(env)
    other = await second_chat(env, chat)
    await caught_up(env)
    uncertain = await approved(env, chat, text=TEXT)
    failed = await approved(env, other, text=TEXT)
    h.sender.plain_script = [
        network_error(),
        telegram_error("Forbidden: bot was blocked by the user", status_code=403),
    ]

    await h.execute(uncertain)
    await h.execute(failed)

    for action_id in (uncertain, failed):
        row = await action(env, action_id)
        state = ActionState(row.state)
        assert state is not ActionState.SUCCEEDED
        assert row.executed_at is None
        assert row.telegram_message_id is None
        assert not STATE_LABELS[state].startswith("Sent")
        reason = STATE_REASON_EXPLANATIONS[StateReason(row.state_reason)]
        assert "will not be resent" in reason or "did not send" in reason
        assert action_id in h.presenter.synced
    assert await sent_rows(env, chat.conversation_id) == []
    assert await sent_rows(env, other.conversation_id) == []
    assert h.channel.sends == [
        ALERT_TEXT[AlertKind.DELIVERY_UNCERTAIN].format(contact="this contact")
    ]
    assert " sent" not in h.channel.sends[0].replace("resend", "")


async def test_an_owner_requested_send_is_judged_from_its_creation(env: Env) -> None:
    """Decisions R3 at Tx A: an owner-dictated action is newer than the
    owner's own earlier message, so that message is no takeover."""
    h = harness(env)
    chat = await seed(env)
    await caught_up(env)
    await add_message_now(
        env, chat, direction="owner_manual", sent_at=env.clock.now() - timedelta(minutes=5)
    )
    async with env.factory() as session, session.begin():
        turn = await seed_turn(session, chat.account_id)
    action_id = await approved(
        env,
        chat,
        text=TEXT,
        origin=ActionOrigin.OWNER_DICTATED,
        trigger_turn_id=turn,
        shown_through_message_id=None,
    )

    assert (await h.execute(action_id)).status is ExecuteStatus.SENT


async def test_the_stale_sweep_settles_only_executing_rows(env: Env) -> None:
    """`mark_stale_attempts` is one conditional UPDATE: only `executing`
    attempts older than the threshold move, with the claim's audit shape."""
    chat = await seed(env)
    executing = await inserted(env, chat, state=ActionState.EXECUTING)
    fresh = await inserted(env, chat, state=ActionState.EXECUTING)
    approved_row = await inserted(env, chat, state=ActionState.APPROVED)
    await update_rows(
        env,
        Action,
        Action.id == executing,
        attempt_id="attempt-old",
        attempt_started_at=env.clock.now() - timedelta(minutes=5),
    )
    await update_rows(
        env,
        Action,
        Action.id == fresh,
        attempt_id="attempt-new",
        attempt_started_at=env.clock.now(),
    )
    async with env.factory() as session, session.begin():
        settled = await mark_stale_attempts(
            session, now=env.clock.now(), older_than=env.clock.now() - timedelta(minutes=2)
        )

    assert [item.action_id for item in settled] == [executing]
    assert (await action(env, executing)).state == "delivery_uncertain"
    assert (await action(env, fresh)).state == "executing"
    assert (await action(env, approved_row)).state == "approved"
    audit = [e for e in await events(env, executing) if e.event == "action_delivery_uncertain"]
    assert audit[0].details == {
        "from": "executing",
        "to": "delivery_uncertain",
        "version": 1,
        "reason": "stale_executing",
        "attempt_id": "attempt-old",
    }
