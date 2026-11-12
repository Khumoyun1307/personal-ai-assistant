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
from tests.fakes.sender import network_error, telegram_error
from tests.integration.p2_factories import seed_action
from tests.scenarios.p2_drive import DRAFT, Scenario, advance, m04_environment, press_action

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


@pytest.mark.parametrize("reason", [None, "source_edited", "source_deleted", "new_contact_message"])
async def test_legacy_outdated_card_is_upgraded_even_when_its_display_state_matches(
    s: Scenario, reason: str | None
) -> None:
    row = await s.seed(
        state="outdated",
        state_reason=reason,
        freshness_warning=None,
        approval_token=None,
    )
    assert row.card_state == "outdated"
    # Simulate an already rendered pre-H10 card; preparation itself would
    # otherwise mask the reconciler's selection bug in this fixture.
    async with transaction(s.env.factory) as uow:
        action = await uow.session.get(Action, row.id)
        assert action is not None
        action.freshness_warning = None
        action.approval_token = None
        action.card_state = "outdated"
    s.env.sender.edits.clear()
    await advance(s, 60, periodic=SYNC)
    upgraded = await s.action(row.id)
    assert upgraded.approval_token is not None
    assert upgraded.freshness_warning == (reason or "source_unavailable")
    assert "Warning:" in s.final_card(upgraded)["text"]
    assert [b["text"] for b in s.card_buttons(upgraded)] == [
        "Approve anyway",
        "Re-draft",
        "Discard",
    ]
    token = upgraded.approval_token
    s.env.sender.edits.clear()
    await advance(s, 60, periodic=SYNC)
    assert s.env.sender.edits == []
    assert (await s.action(row.id)).approval_token == token
    await press_action(s, row.id, "ok")
    assert (await s.action(row.id)).state == "approved"


@pytest.mark.parametrize("reason", [None, "source_edited", "source_deleted", "new_contact_message"])
async def test_replacement_before_legacy_reconciliation_retires_the_old_card(
    s: Scenario, reason: str | None
) -> None:
    row = await s.seed(state="outdated", state_reason=reason)
    async with transaction(s.env.factory) as uow:
        legacy = await uow.session.get(Action, row.id)
        assert legacy is not None
        legacy.freshness_warning = None
        legacy.approval_token = None
        legacy.card_state = "outdated"
    replacement = await s.propose(dictated=True)
    retired = await s.action(row.id)
    assert retired.state_reason == "superseded"
    assert retired.approval_token is None
    await advance(s, 60, periodic=SYNC)
    retired = await s.action(row.id)
    assert retired.approval_token is None
    assert retired.freshness_warning is None
    approvable = [row.id for row in await s.rows() if row.approval_token is not None]
    assert approvable == [replacement.id]
    await press_action(s, replacement.id, "ok")
    assert (await s.action(replacement.id)).state == "approved"


async def test_legacy_reconciliation_never_grants_a_token_with_a_live_replacement(
    s: Scenario,
) -> None:
    row = await s.seed(state="outdated", state_reason="source_edited")
    replacement = await s.propose(dictated=True)
    # Simulate a pre-fix pair of rows, including a queued direct sync.
    async with transaction(s.env.factory) as uow:
        legacy = await uow.session.get(Action, row.id)
        assert legacy is not None
        legacy.state_reason = "source_edited"
        legacy.freshness_warning = None
        legacy.approval_token = None
        legacy.card_state = "outdated"
    await advance(s, 60, periodic=SYNC)
    assert (await s.action(row.id)).approval_token is None
    cards = s.wiring.approval_cards
    assert isinstance(cards, approvals.ApprovalCards)
    await cards.sync(row.id)
    assert (await s.action(row.id)).approval_token is None
    assert (await s.action(replacement.id)).approval_token is not None


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


async def test_an_unchanged_card_is_settled_not_retried(s: Scenario) -> None:
    """Only "message is not modified" confirms the requested card is shown."""
    row = await s.seed()
    await cancel_outside_the_card_flow(s)
    s.env.sender.edits.clear()
    s.env.sender.fail_with = telegram_error("Bad Request: message is not modified", status_code=400)
    await advance(s, 60, periodic=SYNC)
    assert (await s.action(row.id)).card_state == "cancelled"
    await advance(s, 60, periodic=SYNC)
    assert len(s.env.sender.edits) == 1
    assert s.env.sender.business_calls == 0


@pytest.mark.parametrize(
    "description",
    [
        "Bad Request: can't parse entities",
        "Bad Request: message to edit not found",
        "Bad Request: synthetic other refusal",
    ],
)
async def test_a_refused_card_edit_stays_recoverable_with_content_free_logs(
    s: Scenario, description: str
) -> None:
    """A definite edit refusal must never record an unseen card as shown."""
    row = await s.seed()
    await cancel_outside_the_card_flow(s)
    s.env.sender.edits.clear()
    sends_before = len(s.env.sender.sends)
    s.env.sender.fail_with = telegram_error(description, status_code=400)
    with capture_logs() as logs:
        await advance(s, 60, periodic=SYNC)
    stale = await s.action(row.id)
    assert stale.card_message_id == row.card_message_id
    assert stale.card_state == "awaiting_approval"
    assert stale.state == "cancelled"
    refused = [entry for entry in logs if entry["event"] == "approval_card_edit_failed"]
    assert len(refused) == 1
    [entry] = refused
    assert entry["action_id"] == row.id
    assert "error" in entry
    assert entry["failures"] == 1
    assert "parse" not in str(entry)
    assert description not in str(logs)
    await advance(s, 60, periodic=SYNC)
    assert len(s.env.sender.edits) == 2
    assert (await s.action(row.id)).card_state == "awaiting_approval"
    s.env.sender.fail_with = None
    await advance(s, 60, periodic=SYNC)
    assert len(s.env.sender.edits) == 3
    assert (await s.action(row.id)).card_state == "cancelled"
    assert len(s.env.sender.sends) == sends_before
    assert s.env.sender.business_calls == 0


@pytest.mark.parametrize(
    "description",
    ["Bad Request: can't parse entities", "Bad Request: message to edit not found"],
)
async def test_refused_card_edits_stop_at_the_existing_retry_bound(
    s: Scenario, description: str
) -> None:
    row = await s.seed()
    await cancel_outside_the_card_flow(s)
    s.env.sender.edits.clear()
    sends_before = len(s.env.sender.sends)
    s.env.sender.fail_with = telegram_error(description, status_code=400)
    for _ in range(SYNC_MAX_FAILURES + 2):
        await advance(s, 60, periodic=SYNC)
    assert len(s.env.sender.edits) == SYNC_MAX_FAILURES
    assert (await s.action(row.id)).card_state == "awaiting_approval"
    assert len(s.env.sender.sends) == sends_before
    assert s.env.sender.business_calls == 0


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
