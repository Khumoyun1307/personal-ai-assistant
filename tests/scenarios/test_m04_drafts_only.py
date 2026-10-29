"""P2-T21: sending off still allows proposals, with an honest approval outcome."""

import pytest

from tests.scenarios.p2_drive import FLAGS, approve, m04_environment, run_queued

pytestmark = pytest.mark.integration


async def test_with_sending_off_approve_ends_blocked_with_copy_ready_text(
    migrated_url: str,
) -> None:
    async with m04_environment(migrated_url, flags=FLAGS - {"actions.send"}) as s:
        row = await s.propose()
        assert row.state == "awaiting_approval"
        await approve(s, row.id)
        await run_queued(s, "actions.execute")
        final = await s.action(row.id)
        assert (final.state, final.state_reason) == ("blocked", "sending_disabled")
        s.assert_copy_ready(final)
        assert "sending is turned off" in s.final_card(final)["text"].lower()
        assert "sending is off" in str(s.env.sender.answered_callbacks).lower()
        assert s.business_sends() == []
