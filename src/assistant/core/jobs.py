"""The `JobScheduler` port (ARCHITECTURE §12.1).

Callers defer work by task name with **ids and enums only** (AGENTS §13): a
job row is not a place for content. Handlers reload state and re-check
conditions, so running a job twice is safe.

`connection` lets a job be deferred inside the caller's transaction — the
domain row and the job commit or roll back together (M00 jobs spike E1). Pass
the raw driver connection from `UnitOfWork.raw_connection()` /
`db.driver_connection(session)`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

JobArgs = Mapping[str, int | str]

#: Task names callers defer by. Defined here so callers need not import the
#: jobs adapter that registers them.
OWNER_REQUEST_TASK = "agent.owner_request"


class JobScheduler(Protocol):
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
        """Defer a job. Returns its id, or None if a queueing lock already holds one."""
        ...


def owner_control_lock(account_id: int) -> str:
    """Serializes one account's owner turns (ARCHITECTURE §12.2)."""
    return f"acct:{account_id}:control"
