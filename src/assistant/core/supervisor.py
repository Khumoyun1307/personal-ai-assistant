"""In-process supervisor (ARCHITECTURE §4.3).

Compose restarts a container only when its process exits, never when it is
merely unhealthy. So the process itself must exit, non-zero, when:

- any role task ends — a role is meant to run forever, so ending at all (by
  return or by exception) is a failure;
- a role's liveness probe reports a problem (poller not polling, inbox stuck,
  worker heartbeat stale).

On SIGTERM it stops roles in order (poller first, so nothing new arrives; then
the inbox drains; then workers finish in-flight jobs) and waits for each up to
its grace period, then exits 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from assistant.observability.logging import get_logger

logger = get_logger(__name__)

EXIT_CLEAN = 0
EXIT_ROLE_FAILED = 1

LivenessProbe = Callable[[], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class Role:
    """A long-running part of the process."""

    name: str
    run: Callable[[], Coroutine[Any, Any, None]]
    #: Ask the role to finish what it is doing and return. Must not block.
    stop: Callable[[], None]
    #: Returns a short problem description, or None while healthy.
    liveness: LivenessProbe | None = None
    #: How long to wait for the role to return after `stop()` on shutdown.
    grace_seconds: float = 30.0


class Supervisor:
    def __init__(
        self,
        roles: list[Role],
        *,
        liveness_interval: float = 15.0,
        install_signal_handlers: bool = True,
    ) -> None:
        self._roles = roles
        self._liveness_interval = liveness_interval
        self._install_signal_handlers = install_signal_handlers
        self._stopping = asyncio.Event()
        self.exit_reason: str | None = None

    def request_stop(self) -> None:
        """What SIGTERM/SIGINT do. Safe to call more than once."""
        self._stopping.set()

    async def run(self) -> int:
        if self._install_signal_handlers:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, self.request_stop)

        tasks: dict[asyncio.Task[None], Role] = {
            asyncio.create_task(role.run(), name=f"role:{role.name}"): role for role in self._roles
        }
        stop_wait = asyncio.create_task(self._stopping.wait(), name="supervisor:stop")
        liveness = asyncio.create_task(self._watch_liveness(), name="supervisor:liveness")
        logger.info("supervisor_started", roles=[role.name for role in self._roles])

        done, _ = await asyncio.wait(
            {*tasks, stop_wait, liveness}, return_when=asyncio.FIRST_COMPLETED
        )

        if stop_wait in done:
            code = EXIT_CLEAN
            self.exit_reason = "stop_requested"
        elif liveness in done:
            code = EXIT_ROLE_FAILED
            self.exit_reason = liveness.result()
        else:
            ended = next(task for task in tasks if task in done)
            code = EXIT_ROLE_FAILED
            error = None if ended.cancelled() else ended.exception()
            self.exit_reason = f"role {tasks[ended].name} ended" + (
                f" ({type(error).__name__})" if error is not None else ""
            )

        if code != EXIT_CLEAN:
            logger.error("supervisor_exiting", reason=self.exit_reason)
        await self._shutdown(tasks)
        for helper in (stop_wait, liveness):
            helper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await helper
        logger.info("supervisor_stopped", exit_code=code, reason=self.exit_reason)
        return code

    async def _watch_liveness(self) -> str:
        """Returns (and so ends) only when a probe reports a problem."""
        while True:
            await asyncio.sleep(self._liveness_interval)
            for role in self._roles:
                if role.liveness is None:
                    continue
                problem = await role.liveness()
                if problem is not None:
                    return f"role {role.name} unhealthy: {problem}"

    async def _shutdown(self, tasks: dict[asyncio.Task[None], Role]) -> None:
        """Stop roles in declaration order, each within its grace period."""
        for task, role in tasks.items():
            if task.done():
                continue
            role.stop()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=role.grace_seconds)
            except TimeoutError:
                logger.error("role_stop_timeout", role=role.name, grace_seconds=role.grace_seconds)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except asyncio.CancelledError:
                pass
            except Exception as error:  # the role failed while stopping; we are exiting anyway
                logger.error("role_stop_failed", role=role.name, exc_type=type(error).__name__)
