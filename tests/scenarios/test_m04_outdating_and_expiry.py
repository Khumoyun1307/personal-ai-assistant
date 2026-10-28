"""PRD R1 #4: every stale/forbidden proposal stays unsent."""

import pytest

from assistant.conversations.modes import set_mode
from assistant.db.uow import transaction
from tests.integration.business_env import (
    CONTACT,
    OWNER,
    business_message,
    connection_update,
    deleted_messages,
    deliver,
    private_message,
)
from tests.scenarios.p2_drive import Scenario, advance, press_action, run_queued

pytestmark = pytest.mark.integration


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S04.2/S04.3")
async def test_a_new_contact_message_outdates_a_pending_draft(p2: Scenario) -> None:
    row = await p2.seed()
    await deliver(p2.env, business_message(message_id=102, date=p2.date + 1))
    changed = await p2.action(row.id)
    assert (changed.state, changed.state_reason) == ("outdated", "new_contact_message")
    await press_action(p2, row.id, "ok", token=row.approval_token)
    await run_queued(p2, "actions.execute")
    assert p2.business_sends() == []
    assert "no longer" in str(p2.env.sender.answered_callbacks).lower()
    assert any(str(b.get("callback_data", "")).endswith(":rd") for b in p2.card_buttons(changed))


async def test_an_owner_manual_reply_outdates_a_pending_draft(p2: Scenario) -> None:
    row = await p2.seed()
    await deliver(
        p2.env, business_message(from_id=OWNER, chat_id=CONTACT, message_id=102, date=p2.date + 1)
    )
    changed = await p2.action(row.id)
    assert (changed.state, changed.state_reason) == ("outdated", "takeover")
    assert p2.business_sends() == []


async def test_deleting_the_source_message_outdates_the_draft(p2: Scenario) -> None:
    row = await p2.seed()
    await deliver(p2.env, deleted_messages(message_ids=[101]))
    changed = await p2.action(row.id)
    assert (changed.state, changed.state_reason) == ("outdated", "source_deleted")
    assert p2.business_sends() == []


async def test_editing_the_source_message_outdates_the_draft(p2: Scenario) -> None:
    row = await p2.seed()
    await deliver(
        p2.env,
        business_message(
            message_id=101, date=p2.date, edit=True, message_text="P2 synthetic changed request."
        ),
    )
    changed = await p2.action(row.id)
    assert (changed.state, changed.state_reason) == ("outdated", "source_edited")
    await press_action(p2, row.id, "ok", token=row.approval_token)
    await run_queued(p2, "actions.execute")
    assert p2.business_sends() == []
    assert "no longer" in str(p2.env.sender.answered_callbacks).lower()


async def test_switching_to_ignore_cancels_a_pending_draft(p2: Scenario) -> None:
    row = await p2.seed()
    async with transaction(p2.env.factory) as uow:
        await set_mode(
            uow.session,
            account_id=p2.account_id,
            conversation_id=p2.conversation_id,
            mode="ignore",
            actor="owner",
            clock=p2.env.clock,
        )
    changed = await p2.action(row.id)
    assert (changed.state, changed.state_reason) == ("cancelled", "ignore_mode")
    await press_action(p2, row.id, "ok", token=row.approval_token)
    await run_queued(p2, "actions.execute")
    assert p2.business_sends() == []
    assert "no longer" in str(p2.env.sender.answered_callbacks).lower()


async def test_revoking_managed_consent_cancels_a_queued_ai_draft(p2: Scenario) -> None:
    row = await p2.seed(state="approved")
    dictated = await p2.seed(state="approved", origin="owner_dictated", base_message_id=None)
    await deliver(p2.env, private_message(OWNER, "/privacy revoke"))
    await p2.execute(row.id)
    assert (await p2.action(row.id)).state == "cancelled"
    assert (await p2.action(row.id)).state_reason == "consent_revoked"
    assert p2.business_sends() == []
    assert (await p2.action(dictated.id)).state == "approved"
    await p2.execute(dictated.id)
    assert (await p2.action(dictated.id)).state == "succeeded"
    assert len(p2.business_sends()) == 1


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S04.3/S04.4")
async def test_removing_the_reply_right_blocks_an_approved_draft(p2: Scenario) -> None:
    row = await p2.seed(state="approved")
    await deliver(p2.env, connection_update(rights=()))
    await p2.execute(row.id)
    result = await p2.action(row.id)
    assert result.state in {"blocked", "outdated"}
    assert result.state_reason == "rights_changed"
    assert p2.business_sends() == []
    await advance(p2, 0, periodic=("system.sync_action_cards",))
    p2.assert_copy_ready(result)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="awaiting S04.3")
async def test_the_window_closing_expires_the_draft_with_copy_ready_text(p2: Scenario) -> None:
    from datetime import timedelta

    await advance(p2, 23 * 3600 + 40 * 60)
    row = await p2.seed(expires_at=p2.env.clock.now() + timedelta(minutes=15))
    await advance(p2, 16 * 60, periodic=("system.expire_actions",))
    result = await p2.action(row.id)
    assert (result.state, result.state_reason) == ("expired", "window_closed")
    p2.assert_copy_ready(result)
    assert "expired" in p2.final_card(result)["text"].lower()
    assert p2.business_sends() == []


async def test_a_contact_message_ingested_after_approval_blocks_the_send(p2: Scenario) -> None:
    row = await p2.seed(state="approved")
    await deliver(p2.env, business_message(message_id=102, date=p2.date + 1))
    await p2.execute(row.id)
    changed = await p2.action(row.id)
    assert (changed.state, changed.state_reason) == ("outdated", "new_contact_message")
    assert p2.business_sends() == []
