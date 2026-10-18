"""`JobScheduler` over procrastinate (ARCHITECTURE §12.1).

`connection` defers inside the caller's transaction (M00 spike E1): the domain
row and the job commit or roll back together, so a crash between "write the
turn" and "queue the work" cannot lose the work.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from procrastinate import App
from procrastinate.exceptions import AlreadyEnqueued

from assistant.core.jobs import JobArgs
from assistant.observability.logging import get_logger

logger = get_logger(__name__)


class ProcrastinateScheduler:
    def __init__(self, app: App) -> None:
        self._app = app

    async def defer(
        self,
        task: str,
        *,
        args: JobArgs,
        lock: str | None = None,
        queueing_lock: str | None = None,
        schedule_at: datetime | None = None,
        connection: Any | None = None,
    ) -> int | None:
        if lock is not None and schedule_at is not None:
            # A future job holding a lock blocks every later job with that
            # lock until it runs (AGENTS §13, M00 spike E5).
            raise ValueError("a lock must never be put on a future-scheduled job")
        options: dict[str, Any] = {}
        if lock is not None:
            options["lock"] = lock
        if queueing_lock is not None:
            options["queueing_lock"] = queueing_lock
        if schedule_at is not None:
            options["schedule_at"] = schedule_at
        deferrer = self._app.configure_task(name=task, allow_unknown=False, **options)
        job = deferrer.make_new_job(**dict(args))
        try:
            deferred = await self._app.job_manager.defer_job_async(job, connection=connection)
        except AlreadyEnqueued:
            logger.info("job_already_enqueued", task=task)
            return None
        return deferred.id
