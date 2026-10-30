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


async def test_the_window_closing_expires_an_autonomous_draft_with_copy_ready_text(
    p2: Scenario,
) -> None:
    """Autonomous actions keep the conservative pre-check (invariant 8)."""
    from datetime import timedelta

    await advance(p2, 23 * 3600 + 40 * 60)
    row = await p2.seed(
        origin="delegation_auto", expires_at=p2.env.clock.now() + timedelta(minutes=15)
    )
    await advance(p2, 16 * 60, periodic=("system.expire_actions",))
    result = await p2.action(row.id)
    assert (result.state, result.state_reason) == ("expired", "window_closed")
    p2.assert_copy_ready(result)
    assert "expired" in p2.final_card(result)["text"].lower()
    assert p2.business_sends() == []


async def test_an_owner_draft_outlives_the_window_estimate_and_telegram_decides(
    p2: Scenario,
) -> None:
    """Invariant 8 as amended (owner decision 2026-10-30, P2-O20): our 24 h
    estimate does not expire an owner-approved draft. Approve makes exactly
    one attempt; Telegram's PEER_ID_INVALID is a definite not-sent
    (`expired`/`window_closed`), never uncertain or retried, and the final
    card says so with copy-ready text."""
    from datetime import timedelta

    from tests.fakes.sender import telegram_error

    await advance(p2, 23 * 3600 + 40 * 60)
    row = await p2.seed(expires_at=p2.env.clock.now() + timedelta(hours=2))
    await advance(p2, 16 * 60, periodic=("system.expire_actions",))
    assert (await p2.action(row.id)).state == "awaiting_approval"

    p2.env.sender.plain_script = [telegram_error("Bad Request: PEER_ID_INVALID", status_code=400)]
    await press_action(p2, row.id, "ok", token=row.approval_token)
    await run_queued(p2, "actions.execute")
    await run_queued(p2, "actions.execute")

    result = await p2.action(row.id)
    assert (result.state, result.state_reason) == ("expired", "window_closed")
    assert result.error_class == "window_closed"
    assert len(p2.env.sender.plain_sends) == 1
    card = p2.final_card(result)["text"]
    assert "Telegram didn't allow this send" in card
    assert "uncertain" not in card.lower()
    p2.assert_copy_ready(result)


async def test_a_contact_message_ingested_after_approval_blocks_the_send(p2: Scenario) -> None:
    row = await p2.seed(state="approved")
    await deliver(p2.env, business_message(message_id=102, date=p2.date + 1))
    await p2.execute(row.id)
    changed = await p2.action(row.id)
    assert (changed.state, changed.state_reason) == ("outdated", "new_contact_message")
    assert p2.business_sends() == []
