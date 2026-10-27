"""After a database restore (ARCHITECTURE §15.6; runbooks/restore.md).

A restore rewinds the database by up to 24 h. Everything decided after the
dump — pauses, consent changes, deletions, approvals — is undone, and
messages Telegram delivered in between are gone for good. `post_restore`
makes the restored state safe and honest in one transaction:

1. autonomy paused for every account;
2. open actions cancelled and executing ones made uncertain (from M04, when
   the `actions` table exists — recorded here so the step is not forgotten);
3. queued procrastinate jobs deleted (reconcilers re-create valid work);
4. schedules recomputed (from M05);
5. a `possible` coverage gap for [backup_taken_at, now] per active account;
6. the retention sweep;
7. `restore_pending` cleared, and a restore notice queued for the owner.

`assistant run` refuses to start any role while `restore_pending` is set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import Account, AuditLogEntry
from assistant.actions.lifecycle import cancel_pending
from assistant.actions.models import Action
from assistant.actions.states import PENDING, StateReason
from assistant.conversations.models import CoverageGap
from assistant.core.clock import Clock
from assistant.db.models import KEY_RESTORE_PENDING, SystemState
from assistant.db.uow import transaction
from assistant.privacy.retention import SweepReport, sweep_all

KEY_RESTORE_NOTICE = "restore_notice_pending"


class RestorePendingError(RuntimeError):
    """The database was restored and `post-restore` has not run yet."""


@dataclass(frozen=True, slots=True)
class PostRestoreReport:
    accounts_paused: int
    jobs_deleted: int
    gaps_recorded: int
    sweep: SweepReport
    actions_cancelled: int = 0
    actions_uncertain: int = 0


async def restore_pending(session: AsyncSession) -> dict[str, Any] | None:
    """The `restore_pending` marker, if the restore script left one."""
    row = await session.get(SystemState, KEY_RESTORE_PENDING)
    return None if row is None else dict(row.value)


async def assert_not_restore_pending(session: AsyncSession) -> None:
    marker = await restore_pending(session)
    if marker is not None:
        raise RestorePendingError(
            "the database was restored from a backup; run "
            "`python -m assistant post-restore --backup-taken-at <ts>` first"
        )


async def post_restore(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock, *, backup_taken_at: datetime
) -> PostRestoreReport:
    now = clock.now()
    if backup_taken_at > now:
        raise ValueError("backup_taken_at is in the future")
    async with transaction(session_factory) as uow:
        session = uow.session
        # 1. Autonomy off everywhere until the owner looks (§15.6).
        paused = await session.execute(
            update(Account).where(Account.status == "active").values(autonomy_paused=True)
        )
        accounts_paused = int(getattr(paused, "rowcount", 0) or 0)
        # 2. No action from a rewound backup may become executable again.
        action_notices: dict[str, dict[str, list[dict[str, int | str]]]] = {}
        actions_cancelled = actions_uncertain = 0
        for account_id in (await session.scalars(select(Account.id))).all():
            candidates = (
                await session.execute(
                    select(Action.id, Action.state, Action.created_at)
                    .where(Action.account_id == account_id)
                    .where(Action.state.in_(PENDING | {"executing"}))
                    .order_by(Action.id)
                )
            ).all()
            cancelled = await cancel_pending(
                session,
                account_id=account_id,
                conversation_ids=None,
                reason=StateReason.RESTORED_FROM_BACKUP,
                now=now,
            )
            uncertain = (
                await session.execute(
                    update(Action)
                    .where(Action.account_id == account_id, Action.state == "executing")
                    .values(
                        state="delivery_uncertain",
                        state_reason="restored_from_backup",
                        approval_token=None,
                        updated_at=now,
                    )
                    .returning(Action.id, Action.version, Action.attempt_id)
                )
            ).all()
            for action_id, version, attempt_id in uncertain:
                session.add(
                    AuditLogEntry(
                        account_id=account_id,
                        at=now,
                        actor="system",
                        event="action_delivery_uncertain",
                        subject_type="action",
                        subject_id=action_id,
                        details={
                            "from": "executing",
                            "to": "delivery_uncertain",
                            "version": version,
                            "reason": "restored_from_backup",
                            "attempt_id": attempt_id,
                        },
                    )
                )
            action_notices[str(account_id)] = {
                "cancelled": [
                    {"id": row.id, "created_at": row.created_at.isoformat()}
                    for row in candidates
                    if row.id in cancelled
                ],
                "uncertain": [
                    {"id": row.id, "created_at": row.created_at.isoformat()}
                    for row in candidates
                    if row.state == "executing"
                ],
            }
            actions_cancelled += len(cancelled)
            actions_uncertain += len(uncertain)
        # 4. Automation next_run_at recomputation belongs to S05.3b.
        # 3. Queued jobs refer to rows that may no longer exist; reconcilers
        #    re-create what is still valid from domain state.
        deleted = await session.execute(
            text("DELETE FROM procrastinate.procrastinate_jobs WHERE status IN ('todo', 'doing')")
        )
        jobs_deleted = int(getattr(deleted, "rowcount", 0) or 0)
        # 5. Everything dated after the dump may be missing (§6.6).
        accounts = (
            (await session.execute(select(Account.id).where(Account.status == "active")))
            .scalars()
            .all()
        )
        for account_id in accounts:
            session.add(
                CoverageGap(
                    account_id=account_id,
                    conversation_id=None,
                    kind="restore",
                    certainty="possible",
                    at_risk_from=backup_taken_at,
                    at_risk_to=now,
                    created_at=now,
                )
            )
            session.add(
                AuditLogEntry(
                    account_id=account_id,
                    at=now,
                    actor="system",
                    event="restored_from_backup",
                    details={"backup_taken_at": backup_taken_at.isoformat()},
                )
            )
        # 7a. The marker goes; a notice for the owner is queued for the running app.
        marker = await session.get(SystemState, KEY_RESTORE_PENDING)
        if marker is not None:
            await session.delete(marker)
        notice = await session.get(SystemState, KEY_RESTORE_NOTICE)
        payload = {
            "backup_taken_at": backup_taken_at.isoformat(),
            "at": now.isoformat(),
            "actions": action_notices,
        }
        if notice is None:
            session.add(SystemState(key=KEY_RESTORE_NOTICE, value=payload))
        else:
            notice.value = payload
        await session.flush()
    # 6. Retention, outside the big transaction (each class commits on its own).
    sweep = await sweep_all(session_factory, clock)
    return PostRestoreReport(
        accounts_paused, jobs_deleted, len(accounts), sweep, actions_cancelled, actions_uncertain
    )
