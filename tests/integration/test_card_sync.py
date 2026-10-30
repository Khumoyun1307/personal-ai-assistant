"""The card is eventually true (S04.3 §3.2, §3.5): `system.sync_action_cards`.

Production wiring (`_wire_m04`) over the real inbox; Telegram is the fake
sender, scripted to fail where a test needs it. Texts are synthetic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from sqlalchemy import select
from structlog.testing import capture_logs

from assistant.actions import lifecycle
from assistant.actions.models import Action
from assistant.actions.states import StateReason
from assistant.db.uow import transaction
from assistant.telegram.control import approvals
from assistant.telegram.control.approvals import SYNC_MAX_FAILURES
from tests.fakes.sender import network_error
from tests.integration.p2_factories import seed_action
from tests.scenarios.p2_drive import DRAFT, Scenario, advance, m04_environment

pytestmark = pytest.mark.integration

SYNC = ("system.sync_action_cards",)


@pytest.fixture
async def s(migrated_url: str) -> AsyncIterator[Scenario]:
    async with m04_environment(migrated_url) as scenario:
        yield scenario


async def cancel_outside_the_card_flow(s: Scenario) -> None:
    """A state change made by code that has no presenter (F0's bulk helper)."""
    async with transaction(s.env.factory) as uow:
        await lifecycle.cancel_pending(
            uow.session,
            account_id=s.account_id,
            conversation_ids=[s.conversation_id],
            reason=StateReason.DATA_DELETED,
            now=s.env.clock.now(),
        )


async def test_a_state_change_outside_the_card_flow_is_shown_within_a_minute(
    s: Scenario,
) -> None:
    row = await s.seed()
    assert (row.card_message_id, row.card_state) != (None, None)
    assert row.card_state == "awaiting_approval"
    await cancel_outside_the_card_flow(s)
    stale = await s.action(row.id)
    assert (stale.state, stale.card_state) == ("cancelled", "awaiting_approval")
    s.env.sender.edits.clear()
    await advance(s, 60, periodic=SYNC)
    synced = await s.action(row.id)
    assert synced.card_state == "cancelled"
    card = s.final_card(synced)
    assert "Cancelled" in card["text"]
    assert "data was deleted" in card["text"]
    assert card["reply_markup"] is None
    # Nothing left to do: the next run edits nothing.
    s.env.sender.edits.clear()
    await advance(s, 60, periodic=SYNC)
    assert s.env.sender.edits == []


async def test_a_card_lost_between_proposal_and_send_is_presented_by_the_sync(
    s: Scenario,
) -> None:
    async with transaction(s.env.factory) as uow:
        row = await seed_action(
            uow.session,
            account_id=s.account_id,
            conversation_id=s.conversation_id,
            state="awaiting_approval",
            text=DRAFT,
            approval_token="LostCard12",
            created_at=s.env.clock.now(),
            updated_at=s.env.clock.now(),
            expires_at=s.env.clock.now() + timedelta(hours=1),
        )
        lost = row.id
    # Not before the grace period: the after-commit presentation goes first.
    await advance(s, 10, periodic=SYNC)
    assert (await s.action(lost)).card_message_id is None
    assert s.env.sender.sends == []
    await advance(s, 30, periodic=SYNC)
    shown = await s.action(lost)
    assert shown.card_message_id is not None
    assert shown.card_state == "awaiting_approval"
    [card] = s.env.sender.sends
    assert DRAFT in card["text"]
    assert "LostCard12" in str(card["reply_markup"])
    # Presented once.
    await advance(s, 60, periodic=SYNC)
    assert len(s.env.sender.sends) == 1


async def test_sync_retries_a_failed_edit_and_records_card_state(s: Scenario) -> None:
    row = await s.seed()
    await cancel_outside_the_card_flow(s)
    s.env.sender.edits.clear()
    s.env.sender.fail_with = network_error()
    await advance(s, 60, periodic=SYNC)
    assert len(s.env.sender.edits) == 1
    assert (await s.action(row.id)).card_state == "awaiting_approval"  # retried next minute
    s.env.sender.fail_with = None
    await advance(s, 60, periodic=SYNC)
    assert len(s.env.sender.edits) == 2
    assert (await s.action(row.id)).card_state == "cancelled"


async def test_the_sync_stops_after_ten_failed_edits_of_one_card(s: Scenario) -> None:
    row = await s.seed()
    await cancel_outside_the_card_flow(s)
    s.env.sender.edits.clear()
    s.env.sender.fail_with = network_error()
    for _ in range(SYNC_MAX_FAILURES + 2):
        await advance(s, 60, periodic=SYNC)
    assert len(s.env.sender.edits) == SYNC_MAX_FAILURES
    async with s.env.factory() as session:
        state = await session.scalar(select(Action.card_state).where(Action.id == row.id))
    assert state == "awaiting_approval"


async def test_a_card_telegram_refuses_to_edit_is_settled_not_retried(s: Scenario) -> None:
    """ "Message is not modified" (or a card the owner deleted) is a 400:
    repeating the edit cannot help, so the card counts as synced."""
    from tests.fakes.sender import telegram_error

    row = await s.seed()
    await cancel_outside_the_card_flow(s)
    s.env.sender.edits.clear()
    s.env.sender.fail_with = telegram_error("Bad Request: message is not modified", status_code=400)
    await advance(s, 60, periodic=SYNC)
    assert (await s.action(row.id)).card_state == "cancelled"
    await advance(s, 60, periodic=SYNC)
    assert len(s.env.sender.edits) == 1


async def test_a_refused_card_edit_leaves_a_content_free_log_line(s: Scenario) -> None:
    """A 400 settles the card, so a refusal that is not "not modified" (a
    parse error) would otherwise leave a stale card and no trace of it."""
    from tests.fakes.sender import telegram_error

    row = await s.seed()
    await cancel_outside_the_card_flow(s)
    s.env.sender.fail_with = telegram_error("Bad Request: can't parse entities", status_code=400)
    with capture_logs() as logs:
        await advance(s, 60, periodic=SYNC)
    refused = [entry for entry in logs if entry["event"] == "approval_card_edit_refused"]
    assert len(refused) == 1
    [entry] = refused
    assert entry["action_id"] == row.id
    assert entry["state"] == "cancelled"
    assert "error" in entry
    assert "code" in entry
    assert "parse" not in str(entry)


async def test_the_sync_tries_again_once_the_state_moves_on_after_giving_up(
    s: Scenario,
) -> None:
    """Failures count per state shown: a card given up on is edited again
    when its action reaches a new state (e.g. after a long outage)."""
    row = await s.seed()
    async with transaction(s.env.factory) as uow:
        approved = await lifecycle.approve(
            uow.session,
            account_id=s.account_id,
            token=row.approval_token or "",
            now=s.env.clock.now(),
        )
    assert approved.status is lifecycle.ApproveStatus.APPROVED
    s.env.sender.edits.clear()
    s.env.sender.fail_with = network_error()
    for _ in range(SYNC_MAX_FAILURES + 2):
        await advance(s, 60, periodic=SYNC)
    assert len(s.env.sender.edits) == SYNC_MAX_FAILURES
    await cancel_outside_the_card_flow(s)
    s.env.sender.fail_with = None
    await advance(s, 60, periodic=SYNC)
    assert len(s.env.sender.edits) == SYNC_MAX_FAILURES + 1
    assert (await s.action(row.id)).card_state == "cancelled"


async def test_a_card_the_sync_gave_up_on_never_holds_a_batch_slot(
    s: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(approvals, "SYNC_BATCH", 1)
    first = await s.seed()
    second = await s.seed(state="approved")
    await cancel_outside_the_card_flow(s)
    s.env.sender.edits.clear()
    s.env.sender.fail_with = network_error()
    for _ in range(SYNC_MAX_FAILURES):
        await advance(s, 60, periodic=SYNC)
    assert {edit["message_id"] for edit in s.env.sender.edits} == {first.card_message_id}
    s.env.sender.fail_with = None
    await advance(s, 60, periodic=SYNC)
    assert (await s.action(second.id)).card_state == "cancelled"


async def test_pf06_a_dictated_card_superseding_an_agent_card_edits_the_old_one_once(
    s: Scenario,
) -> None:
    """ISSUE-006: prod logged one refused edit (400) of the replaced card in the
    second a newer owner_dictated proposal replaced an older agent_proposal card.
    Telegram refuses an edit that changes nothing ("message is not modified"), so
    the replaced card must be edited once, with a text and keyboard of its own."""
    older = await s.propose()
    s.env.sender.edits.clear()

    newer = await s.propose(dictated=True)

    assert newer.id != older.id
    stale = await s.action(older.id)
    assert stale.state == "outdated"
    old_edits = [e for e in s.env.sender.edits if e["message_id"] == stale.card_message_id]
    assert len(old_edits) == 1, "the replaced card is edited exactly once"
    assert "Outdated" in old_edits[0]["text"]
    # Nothing is left for the periodic sync to repeat.
    s.env.sender.edits.clear()
    await advance(s, 60, periodic=SYNC)
    assert s.env.sender.edits == []
