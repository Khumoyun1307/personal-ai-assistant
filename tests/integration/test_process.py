"""The assembled process (bootstrap.running_process), without any network (S01.5).

Telegram is never contacted: the token is synthetic and only roles that do not
call the Bot API at startup are started (`worker`, `system`, `inbox`; the
poller calls `getMe`).

- the worker and system roles start, register with procrastinate, and stop
  cleanly (exit 0) on a stop request;
- a second inbox processor cannot start while one holds the advisory lock.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text

from assistant.bootstrap import InstanceAlreadyRunningError, running_process
from assistant.config import Settings
from assistant.core.supervisor import EXIT_CLEAN
from assistant.db.advisory_lock import NAMESPACE_INBOX, AdvisoryLock
from assistant.db.engine import build_engine

pytestmark = pytest.mark.integration

SYNTHETIC_BOT_ID = 123456
RESET = (
    "TRUNCATE procrastinate.procrastinate_jobs, procrastinate.procrastinate_events, "
    "procrastinate.procrastinate_periodic_defers, procrastinate.procrastinate_workers CASCADE"
)


@pytest.fixture
async def settings(migrated_url: str, clean_env: None) -> AsyncIterator[Settings]:
    engine = build_engine(migrated_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(RESET)
    values: dict[str, Any] = {
        "database_url": migrated_url,
        "bot_token": f"{SYNTHETIC_BOT_ID}:synthetic-test-token",
        "openai_api_key": "sk-test-synthetic",
        "owner_telegram_ids": "7000000001",
    }
    yield Settings(_env_file=None, **values)  # type: ignore[arg-type]
    async with engine.begin() as conn:
        await conn.exec_driver_sql(RESET)
    await engine.dispose()


async def registered_workers(url: str) -> int:
    engine = build_engine(url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT count(*) FROM procrastinate.procrastinate_workers")
            )
            return int(result.scalar_one())
    finally:
        await engine.dispose()


async def test_worker_and_system_roles_start_and_stop_cleanly(
    settings: Settings, migrated_url: str
) -> None:
    async with running_process(settings, frozenset({"worker", "system"})) as supervisor:
        running = asyncio.create_task(supervisor.run())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        while await registered_workers(migrated_url) < 3:  # system, outbound, llm
            assert not running.done(), supervisor.exit_reason
            assert loop.time() < deadline, "workers did not register"
            await asyncio.sleep(0.05)

        supervisor.request_stop()
        assert await running == EXIT_CLEAN

    assert await registered_workers(migrated_url) == 0  # unregistered on the way out


async def test_a_second_inbox_processor_cannot_start(settings: Settings, migrated_url: str) -> None:
    engine = build_engine(migrated_url)
    holder = AdvisoryLock(engine, NAMESPACE_INBOX, SYNTHETIC_BOT_ID)
    assert await holder.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunningError):
            async with running_process(settings, frozenset({"inbox"})):
                pass
    finally:
        await holder.release()
        await engine.dispose()
