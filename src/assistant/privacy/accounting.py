"""Retention, reconciliation and export of priced admissions (I16, C7).

`llm_admissions` rows are content-free accounting. The agent layer owns the
model (`assistant.llm.models`), which a domain package may not import
(import-linter), so the table is named here in SQL, as `retention.py` does
for `llm_calls`.

- **Retention:** a settled admission (`completed`, `released`) follows the
  1-year accounting retention. An unresolved one (`reserved`, `dispatched`,
  `billing_uncertain`) is never deleted or freed by time, month rollover or
  content deletion; it stays held and visible until reconciled.
- **Reconciliation (hourly, with the sweep):** a `reserved` row older than
  `STALE_ADMISSION_AGE` provably never reached the provider (dispatch is
  committed first), so it is released. A `dispatched` row that old lost its
  process mid-call: it becomes `billing_uncertain` (still held) so the
  owner-visible count shows it; it is never released or replayed.
- **Deletion:** `delete_everything` and scoped deletions keep these rows;
  removing a run or call only nulls the link (`ON DELETE SET NULL`).
  Physical account removal is refused while any admission remains
  (`ON DELETE RESTRICT`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: Far beyond any call timeout plus its explicit retries.
STALE_ADMISSION_AGE = timedelta(hours=1)

_COLUMNS = (
    "id",
    "account_id",
    "operation_id",
    "attempt_id",
    "run_id",
    "llm_call_id",
    "model",
    "price_version",
    "month_start",
    "input_bound",
    "output_bound",
    "reserved_usd",
    "charged_usd",
    "status",
    "error_code",
    "created_at",
    "updated_at",
)


@dataclass(frozen=True, slots=True)
class Reconciled:
    released: int = 0
    uncertain: int = 0


def _count(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


async def reconcile_stale_admissions(session: AsyncSession, *, now: datetime) -> Reconciled:
    cutoff = now - STALE_ADMISSION_AGE
    released = await session.execute(
        text(
            "UPDATE app.llm_admissions SET status = 'released', charged_usd = 0, "
            "error_code = 'not_dispatched', updated_at = :now "
            "WHERE status = 'reserved' AND updated_at < :cutoff"
        ),
        {"now": now, "cutoff": cutoff},
    )
    uncertain = await session.execute(
        text(
            "UPDATE app.llm_admissions SET status = 'billing_uncertain', "
            "error_code = 'stale_dispatch', updated_at = :now "
            "WHERE status = 'dispatched' AND updated_at < :cutoff"
        ),
        {"now": now, "cutoff": cutoff},
    )
    return Reconciled(_count(released), _count(uncertain))


async def sweep_admissions(session: AsyncSession, *, cutoff: datetime) -> int:
    """Delete settled admissions older than the accounting cutoff."""
    result = await session.execute(
        text(
            "DELETE FROM app.llm_admissions "
            "WHERE status IN ('completed', 'released') AND updated_at < :cutoff"
        ),
        {"cutoff": cutoff},
    )
    return _count(result)


async def export_admissions(session: AsyncSession, *, account_id: int) -> list[dict[str, Any]]:
    """The account's admissions, content-free, for a data export."""
    rows = await session.execute(
        text(
            f"SELECT {', '.join(_COLUMNS)} FROM app.llm_admissions "  # noqa: S608 - fixed columns
            "WHERE account_id = :account_id ORDER BY id"
        ),
        {"account_id": account_id},
    )
    exported: list[dict[str, Any]] = []
    for row in rows:
        values: dict[str, Any] = {}
        for name, value in row._mapping.items():
            if isinstance(value, datetime):
                values[name] = value.isoformat()
            elif name in {"operation_id", "attempt_id"} or name.endswith("_usd"):
                values[name] = None if value is None else str(value)
            else:
                values[name] = value
        exported.append(values)
    return exported
