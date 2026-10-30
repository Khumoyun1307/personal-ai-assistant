"""PRD J4 / R1 #3: approve only the version actually shown to the owner."""

import asyncio
import hashlib
from typing import Any

import pytest

from assistant.llm.port import LLMRequest
from tests.integration.business_env import CONNECTION, CONTACT, business_message, deliver
from tests.scenarios.p2_drive import (
    DRAFT,
    REPLACEMENT,
    Scenario,
    approve,
    edit,
    entered_or_finished,
    reject,
    revision_result,
    run_queued,
    tool_outputs,
)

pytestmark = pytest.mark.integration


async def test_j4_draft_edit_approve_sends_exactly_the_approved_text(p2: Scenario) -> None:
    row = await p2.propose()
    assert row.state == "awaiting_approval"
    assert DRAFT in p2.control_text()
    assert "Synthetic" in p2.control_text()
    assert "synthetic contact text" in p2.control_text()
    assert row.expires_at is not None
    assert row.expires_at.strftime("%H:%M") in p2.control_text()
    assert row.card_message_id is not None
    p2.fake.parsed.append(
        revision_result(
            text="P2 model text must not replace the owner's words.", revision_kind="replacement"
        )
    )
    await edit(p2, row.id, REPLACEMENT)
    assert await run_queued(p2, "replies.revise") == 1
    revised = await p2.action(row.id)
    assert revised.version == 2
    assert revised.text == REPLACEMENT
    assert revised.approval_token != row.approval_token
    assert REPLACEMENT in p2.final_card(revised)["text"]
    assert p2.business_sends() == []
    await approve(p2, row.id)
    assert await run_queued(p2, "actions.execute") == 1
    [sent] = p2.business_sends()
    assert (sent["chat_id"], sent["business_connection_id"], sent["text"]) == (
        CONTACT,
        CONNECTION,
        REPLACEMENT,
    )
    final = await p2.action(row.id)
    assert final.state == "succeeded"
    assert (
        final.approved_text_hash
        == final.sent_text_hash
        == hashlib.sha256(REPLACEMENT.encode()).hexdigest()
    )
    assert "Sent" in p2.control_text()


async def test_j4_a_revision_instruction_creates_a_new_version_shown_for_approval(
    p2: Scenario,
) -> None:
    row = await p2.propose()
    old_token = row.approval_token
    p2.fake.parsed.append(revision_result(text="P2 revised concise reply."))
    await edit(p2, row.id, "Make this shorter.")
    assert await run_queued(p2, "replies.revise") == 1
    revised = await p2.action(row.id)
    assert revised.state == "awaiting_approval"
    assert revised.version == 2
    assert revised.approval_token
    assert revised.approval_token != old_token
    assert revised.text == "P2 revised concise reply."
    assert revised.text in p2.control_text()
    assert p2.business_sends() == []


async def revision_interleaving(p2: Scenario, *, outdate: bool) -> None:
    row = await p2.propose()
    old_token = row.approval_token
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(_request: LLMRequest) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return revision_result(text=REPLACEMENT)

    p2.fake.parsed.append(held)
    await edit(p2, row.id, "Make this shorter.")
    task = asyncio.create_task(run_queued(p2, "replies.revise"))
    try:
        await entered_or_finished(task, entered)
        assert (await p2.action(row.id)).state == "revising"
        if outdate:
            await deliver(p2.env, business_message(message_id=102, date=p2.date + 1))
            before = await p2.action(row.id)
            assert before.state == "outdated"
        else:
            from tests.scenarios.p2_drive import press_action

            await press_action(p2, row.id, "ok", token=old_token)
            assert p2.business_sends() == []
            assert (await p2.action(row.id)).state == "revising"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
    # Propagate handler failures after the barrier has been cleaned up.
    await task
    result = await p2.action(row.id)
    if outdate:
        assert (result.state, result.version, result.text) == (
            before.state,
            before.version,
            before.text,
        )
    else:
        assert result.state == "awaiting_approval"
        assert result.version == 2
        assert result.approval_token != old_token
    assert p2.business_sends() == []


async def test_approve_during_revision_sends_nothing(p2: Scenario) -> None:
    await revision_interleaving(p2, outdate=False)


async def test_a_revision_finishing_after_outdating_cannot_reactivate_the_card(
    p2: Scenario,
) -> None:
    await revision_interleaving(p2, outdate=True)


async def test_j4_reject_sends_nothing_and_says_rejected(p2: Scenario) -> None:
    row = await p2.propose()
    await reject(p2, row.id)
    await run_queued(p2, "actions.execute")
    assert (await p2.action(row.id)).state == "rejected"
    assert p2.business_sends() == []
    assert "Rejected" in p2.control_text()


async def test_j4_owner_dictated_text_is_shown_exactly_with_the_resolved_recipient(
    p2: Scenario,
) -> None:
    row = await p2.propose(dictated=True)
    assert (row.origin, row.text, row.state) == ("owner_dictated", REPLACEMENT, "awaiting_approval")
    assert row.base_message_id is None
    assert row.conversation_id == p2.conversation_id
    assert REPLACEMENT in p2.control_text()
    assert "Synthetic" in p2.control_text()
    assert any(
        r.get("status") == "awaiting_approval" and r.get("action_id") == row.id
        for r in tool_outputs(p2)
    )
    assert p2.business_sends() == []
