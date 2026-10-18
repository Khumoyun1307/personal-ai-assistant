"""In-process supervisor (ARCHITECTURE §4.3, S01.5).

Pinned claims:

- killing any role task makes the process exit non-zero (a role that returns
  is a failure too: roles are meant to run forever);
- an unhealthy liveness probe makes the process exit non-zero;
- SIGTERM lets an in-flight control-chat send complete within the grace
  period, then exits 0;
- a role that ignores `stop()` is cancelled after its grace period.

Synchronisation uses events, never sleeps-for-timing (AGENTS §11).
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest

from assistant.core.supervisor import EXIT_CLEAN, EXIT_ROLE_FAILED, Role, Supervisor


class LoopingRole:
    """Runs until stopped; can be told to crash, to return, or to be mid-send."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.started = asyncio.Event()
        self._stop = asyncio.Event()
        self.crash = asyncio.Event()
        self.finish = asyncio.Event()

    async def run(self) -> None:
        self.started.set()
        stop = asyncio.create_task(self._stop.wait())
        crash = asyncio.create_task(self.crash.wait())
        finish = asyncio.create_task(self.finish.wait())
        done, pending = await asyncio.wait(
            {stop, crash, finish}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if crash in done:
            raise RuntimeError("synthetic role crash")

    def stop(self) -> None:
        self._stop.set()

    def role(self, **overrides: object) -> Role:
        values: dict[str, object] = {"name": self.name, "run": self.run, "stop": self.stop}
        values.update(overrides)
        return Role(**values)  # type: ignore[arg-type]


async def _wait_started(*roles: LoopingRole) -> None:
    await asyncio.gather(*(role.started.wait() for role in roles))


async def test_a_crashing_role_makes_the_process_exit_non_zero() -> None:
    poller, inbox = LoopingRole("poller"), LoopingRole("inbox")
    supervisor = Supervisor([poller.role(), inbox.role()], install_signal_handlers=False)
    running = asyncio.create_task(supervisor.run())
    await _wait_started(poller, inbox)

    inbox.crash.set()

    assert await running == EXIT_ROLE_FAILED
    assert supervisor.exit_reason == "role inbox ended (RuntimeError)"
    assert poller._stop.is_set()  # the others were stopped, not abandoned


async def test_a_role_that_simply_returns_is_also_a_failure() -> None:
    worker = LoopingRole("worker:llm")
    supervisor = Supervisor([worker.role()], install_signal_handlers=False)
    running = asyncio.create_task(supervisor.run())
    await _wait_started(worker)

    worker.finish.set()

    assert await running == EXIT_ROLE_FAILED
    assert supervisor.exit_reason == "role worker:llm ended"


async def test_an_unhealthy_role_makes_the_process_exit_non_zero() -> None:
    poller = LoopingRole("poller")
    healthy = asyncio.Event()

    async def probe() -> str | None:
        if healthy.is_set():
            return None
        return "no successful getUpdates for 3 minutes"

    supervisor = Supervisor(
        [poller.role(liveness=probe)], liveness_interval=0, install_signal_handlers=False
    )

    assert await supervisor.run() == EXIT_ROLE_FAILED
    assert supervisor.exit_reason == (
        "role poller unhealthy: no successful getUpdates for 3 minutes"
    )


async def test_sigterm_lets_an_in_flight_send_finish_then_exits_cleanly() -> None:
    send_started, send_may_finish = asyncio.Event(), asyncio.Event()
    delivered: list[str] = []
    stopping = asyncio.Event()

    async def inbox_run() -> None:
        # Mid-way through a control-chat send when the signal arrives.
        send_started.set()
        await send_may_finish.wait()
        delivered.append("reply")
        await stopping.wait()

    def inbox_stop() -> None:
        stopping.set()
        # The transport completes the request it already started.
        send_may_finish.set()

    supervisor = Supervisor([Role("inbox", inbox_run, inbox_stop, grace_seconds=5)])
    running = asyncio.create_task(supervisor.run())
    await send_started.wait()

    os.kill(os.getpid(), signal.SIGTERM)

    assert await running == EXIT_CLEAN
    assert supervisor.exit_reason == "stop_requested"
    assert delivered == ["reply"]


async def test_a_role_that_ignores_stop_is_cancelled_after_its_grace_period() -> None:
    cancelled = asyncio.Event()

    async def stubborn() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    supervisor = Supervisor(
        [Role("stubborn", stubborn, lambda: None, grace_seconds=0)],
        install_signal_handlers=False,
    )
    running = asyncio.create_task(supervisor.run())
    await asyncio.sleep(0)

    supervisor.request_stop()

    assert await running == EXIT_CLEAN
    assert cancelled.is_set()


@pytest.mark.parametrize("role_name", ["poller", "inbox", "worker:system", "worker:llm"])
async def test_any_role_ending_ends_the_process(role_name: str) -> None:
    roles = [LoopingRole(name) for name in ("poller", "inbox", "worker:system", "worker:llm")]
    supervisor = Supervisor([role.role() for role in roles], install_signal_handlers=False)
    running = asyncio.create_task(supervisor.run())
    await _wait_started(*roles)

    next(role for role in roles if role.name == role_name).crash.set()

    assert await running == EXIT_ROLE_FAILED
    assert role_name in (supervisor.exit_reason or "")
