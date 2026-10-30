"""PF-12: a target the owner chose survives the card's Edit revision.

Before, every revision re-anchored the draft on the chat's newest contact
message (`approvals.py`, revise apply), so a reply to an earlier message
the owner named, or a standalone new message, moved. Through the
production wiring (`_wire_m04`), the real inbox and the `replies.revise`
job; only the clock, the transport and the model are fake. Synthetic text.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from assistant.conversations.models import Message
from tests.integration.business_env import business_message, deliver
from tests.integration.test_approvals import start_edit
from tests.scenarios.p2_drive import Scenario, draft_result, m04_environment, run_queued

pytestmark = pytest.mark.integration


@pytest.fixture
async def s(migrated_url: str) -> AsyncIterator[Scenario]:
    async with m04_environment(migrated_url) as scenario:
        yield scenario


async def contact_ids(s: Scenario) -> list[int]:
    async with s.env.factory() as session:
        return list(
            await session.scalars(
                select(Message.id)
                .where(Message.conversation_id == s.conversation_id, Message.direction == "contact")
                .order_by(Message.id)
            )
        )


def owner_flags(target: str, mode: str = "reply") -> dict[str, Any]:
    return {
        "needs_reply": True,
        "confidence": "high",
        "sensitive_topic": False,
        "makes_commitment": False,
        "should_escalate": None,
        "uses_only_permitted_information": None,
        "language": "en",
        "source": "draft_owner",
        "mode": mode,
        "target": target,
    }


@pytest.mark.parametrize(
    ("target", "mode", "base", "kept"),
    [
        ("answered", "reply", "earlier", "earlier"),
        ("none", "new_message", None, None),
        ("newest", "reply", "earlier", "newest"),  # today's default: re-anchored
    ],
)
async def test_a_revision_keeps_an_explicit_target_and_re_anchors_the_default(
    s: Scenario, target: str, mode: str, base: str | None, kept: str | None
) -> None:
    await deliver(
        s.env, business_message(message_id=4401, message_text="SYNTH-LATER", date=s.date + 1)
    )
    earlier, *_, newest = await contact_ids(s)
    named = {"earlier": earlier, "newest": newest, None: None}
    row = await s.seed(
        base_message_id=named[base],
        shown_through_message_id=newest,
        draft_flags=owner_flags(target, mode),
        trigger_turn_id=None,
    )
    s.fake.parsed.append(draft_result(text="Synthetic shorter reply.", revision_kind="instruction"))
    await start_edit(s, row, "Synthetic: shorter please.")
    assert await run_queued(s, "replies.revise") == 1
    revised = await s.action(row.id)
    assert (revised.state, revised.version, revised.text) == (
        "awaiting_approval",
        2,
        "Synthetic shorter reply.",
    )
    assert revised.base_message_id == named[kept]
    assert revised.draft_flags is not None
    if target == "newest":
        assert "target" not in revised.draft_flags  # a revision's own flags, as before
    else:
        assert revised.draft_flags["target"] == target
