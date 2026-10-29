"""S05.3b's `_wire_m05` entries: the three automation tasks, `/digest` and the
digest's "Open" buttons are always wired; flag `digest.daily` only decides
whether digest automations follow the preference and fire. No task carries
a procrastinate `lock` (AGENTS §13). No network, no database."""

from __future__ import annotations

from typing import Any

import pytest

from assistant import bootstrap
from assistant.config import Settings
from assistant.core.jobs import DIGEST_TASK, FIRE_TASK
from assistant.inbox.digest import DIGEST_CARD
from assistant.jobs.app import QUEUE_LLM, QUEUE_OUTBOUND, QUEUE_SYSTEM, build_app
from assistant.jobs.tasks_automation import RECONCILE_SCHEDULES, register_automation_tasks
from assistant.telegram.control.digest import handle_digest_command, handle_digest_press
from tests.fakes.channel import FakeChannel
from tests.fakes.clock import FakeClock

DB = "postgresql+psycopg://user:pw@db:5432/assistant"


def wired(flags: str) -> bootstrap._PhaseWiring:
    values: dict[str, Any] = {"database_url": DB, "feature_flags": flags}
    wiring = bootstrap._PhaseWiring()
    bootstrap._wire_m05(
        wiring,
        settings=Settings(_env_file=None, **values),  # type: ignore[arg-type]
        session_factory=None,  # type: ignore[arg-type]
        gateway=None,  # type: ignore[arg-type]
        channel=FakeChannel(),
        clock=FakeClock(),
        scheduler=None,  # type: ignore[arg-type]
        llm=None,
        roles=frozenset({"worker", "system"}),
    )
    return wiring


@pytest.mark.parametrize("flags", ["", "digest.daily"])
def test_m05_wires_the_schedule_jobs_the_digest_command_and_its_buttons(
    clean_env: None, flags: str
) -> None:
    wiring = wired(flags)
    assert wiring.automation is not None
    jobs = wiring.automation.schedules
    assert jobs is not None
    assert jobs.digest_enabled is bool(flags)
    assert wiring.routes.commands["digest"] is handle_digest_command
    assert wiring.routes.callbacks[DIGEST_CARD] is handle_digest_press

    app = build_app(DB)
    register_automation_tasks(app, wiring.automation)
    expected = {
        RECONCILE_SCHEDULES: (QUEUE_SYSTEM, RECONCILE_SCHEDULES),
        FIRE_TASK: (QUEUE_OUTBOUND, None),  # qlock `auto:{id}:{epoch}` set at defer
        DIGEST_TASK: (QUEUE_LLM, None),  # qlock `digest:{acct}:{epoch}` set at defer
    }
    for name, (queue, queueing_lock) in expected.items():
        task = app.tasks[name]
        assert (task.queue, task.queueing_lock) == (queue, queueing_lock)
        assert task.lock is None
