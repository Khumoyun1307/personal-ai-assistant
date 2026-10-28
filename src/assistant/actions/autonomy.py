"""The account-level kill switch (PRD J9, FR-AUT-06; ARCHITECTURE §9.4; S04.4 §3.7).

`/pause` in P2 stops **autonomous** sending only (P2-T23): the account flag
is set and every pending `delegation_auto` action is cancelled; Tx A denies
that origin while paused (rule `autonomy_not_paused`). Owner-approved actions
carry the owner's explicit approval of that text and are not affected.

Both functions take the account row `FOR UPDATE` first — the §9.4 lock order
that Tx A follows with `FOR SHARE` — so a pause either commits before a claim
reads the account, or waits for that claim's transaction: a claim is never
made after a committed pause. Idempotent; the audit row is written only when
the flag changes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.accounts.models import Account, AuditLogEntry
from assistant.actions.lifecycle import cancel_autonomous_pending

EVENT_PAUSED = "autonomy_paused"
EVENT_RESUMED = "autonomy_resumed"
#: Audit `source` codes.
SOURCE_COMMAND = "command"
SOURCE_STOP_WORD = "stop_word"
SOURCE_FAST_LANE = "fast_lane"
SOURCE_TOOL = "tool"
SOURCE_CARD = "card"


async def pause(
    session: AsyncSession,
    *,
    account_id: int,
    now: datetime,
    source: str,
    details: dict[str, Any] | None = None,
) -> bool:
    """Set `autonomy_paused` and cancel pending autonomous actions. Returns
    whether the flag changed. `source` (a code: `command`, `stop_word`,
    `fast_lane`, `tool`) and `details` (ids only) go to the audit row."""
    account = await _lock(session, account_id)
    if account is None:
        return False
    changed = not account.autonomy_paused
    account.autonomy_paused = True
    await session.flush()
    await cancel_autonomous_pending(session, account_id=account_id, now=now)
    if changed:
        _audit(session, account_id, EVENT_PAUSED, now, source, details)
        await session.flush()
    return changed


async def resume(
    session: AsyncSession,
    *,
    account_id: int,
    now: datetime,
    source: str,
    details: dict[str, Any] | None = None,
) -> bool:
    """Clear the account flag only: per-conversation pauses (takeover, ingest
    error) stay (R-09). Only `/resume` or the owner's tap on a card calls this."""
    account = await _lock(session, account_id)
    if account is None:
        return False
    changed = account.autonomy_paused
    account.autonomy_paused = False
    await session.flush()
    if changed:
        _audit(session, account_id, EVENT_RESUMED, now, source, details)
        await session.flush()
    return changed


async def active_account_id(session: AsyncSession, telegram_user_id: int) -> int | None:
    """The active account of an owner's Telegram user id, or None."""
    return await session.scalar(
        select(Account.id).where(
            Account.telegram_user_id == telegram_user_id, Account.status == "active"
        )
    )


async def _lock(session: AsyncSession, account_id: int) -> Account | None:
    return await session.scalar(
        select(Account)
        .where(Account.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _audit(
    session: AsyncSession,
    account_id: int,
    event: str,
    now: datetime,
    source: str,
    details: dict[str, Any] | None,
) -> None:
    session.add(
        AuditLogEntry(
            account_id=account_id,
            at=now,
            # A model call pausing is the agent acting on the owner's word.
            actor="agent" if source == SOURCE_TOOL else "owner",
            event=event,
            subject_type="account",
            subject_id=account_id,
            details={"source": source, **(details or {})},
        )
    )
