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
CONNECTION_NOTIFY_TASK = "connections.notify_owner"
#: Runs a held tool call the owner confirmed (S03.1). Deferred with the
#: owner's control lock, never scheduled (AGENTS §13).
APPLY_CONFIRMATION_TASK = "agent.apply_confirmation"
#: Refreshes one conversation's rolling summary (S03.2). Debounced: deferred
#: with `schedule_at` and a queueing lock, never a `lock` (AGENTS §13).
REFRESH_SUMMARY_TASK = "memory.refresh_summary"
#: An owner's button deletion (S03.5): deferred with the owner's control
#: lock, so it never interleaves with an owner run or an applied
#: confirmation (M03 D83). Args: account_id, scope, subject_id, card_id.
DELETE_DATA_TASK = "privacy.delete_data"


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


def summary_queueing_lock(conversation_id: int) -> str:
    """At most one queued summary refresh per conversation (ARCHITECTURE §12.2)."""
    return f"sum:{conversation_id}"
