"""Restore gate queries exercise PostgreSQL on synthetic data only."""

import asyncio
from datetime import timedelta

import pytest
from tools.acceptance.p2_verify import open_database
from tools.acceptance.restore_verify import verify

from assistant.privacy.post_restore import post_restore
from tests.integration.business_env import Env
from tests.integration.test_action_lifecycle_bulk import seed_actions

pytestmark = pytest.mark.integration


async def test_restore_gate_fails_before_post_restore_and_reports_its_notice_afterwards(
    env: Env, migrated_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    account, _, ids = await seed_actions(env)

    def check() -> int:
        with open_database(migrated_url) as conn:
            return verify(conn)

    assert await asyncio.to_thread(check) == 1
    capsys.readouterr()
    await post_restore(env.factory, env.clock, backup_taken_at=env.clock.now() - timedelta(hours=1))
    assert await asyncio.to_thread(check) == 0
    output = capsys.readouterr().out
    assert "pending_actions=0" in output
    assert "pending_jobs=0" in output
    assert "restore_notice_pending=yes" in output
    assert f"account={account} cancelled=4 uncertain=1" in output
    assert f"account={account} cancelled_action={ids['awaiting_approval']}" in output
    assert f"account={account} uncertain_action={ids['executing']}" in output
    assert "state_reason=restored_from_backup count=5" in output
