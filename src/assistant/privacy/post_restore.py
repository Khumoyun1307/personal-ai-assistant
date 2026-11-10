"""After a database restore (ARCHITECTURE §15.6; runbooks/restore.md).

A restore rewinds the database by up to 24 h. Everything decided after the
dump — pauses, consent changes, deletions, approvals — is undone, and
messages Telegram delivered in between are gone for good. `post_restore`
makes the restored state safe and honest, keeping the gate through the sweep:

1. gate committed, restored AI consents revoked, pending inputs/cards removed;
   autonomy paused for every account;
2. open actions cancelled and executing ones made uncertain (from M04, when
   the `actions` table exists — recorded here so the step is not forgotten);
3. queued procrastinate jobs deleted (reconcilers re-create valid work);
4. schedules recomputed: missed and claimed occurrences skipped with the
   note `skipped_restore`, `next_run_at` from now (S05.3b);
5. a `possible` coverage gap for [backup_taken_at, now] per active account;
6. the retention sweep;
7. maintenance completion recorded; I20 journal/manual/purge reconciliation
   must succeed separately before opening the gate or publishing the notice.

`assistant run` refuses to start any role while `restore_pending` is set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import service
from assistant.accounts.models import Account, AuditLogEntry, PendingInput
from assistant.actions.lifecycle import cancel_pending
from assistant.actions.models import Action
from assistant.actions.states import PENDING, StateReason
from assistant.automation import schedules
from assistant.conversations.models import CoverageGap
from assistant.core.clock import Clock
from assistant.db.models import KEY_RESTORE_PENDING, SystemState
from assistant.db.uow import transaction
from assistant.privacy.owner_requests import cancel_restored_requests
from assistant.privacy.retention import SweepReport, sweep_all

KEY_RESTORE_NOTICE = "restore_notice_pending"


class RestorePendingError(RuntimeError):
    """A restored database still needs maintenance or privacy reconciliation."""


@dataclass(frozen=True, slots=True)
class PostRestoreReport:
    accounts_paused: int
    jobs_deleted: int
    gaps_recorded: int
    sweep: SweepReport
    actions_cancelled: int = 0
    actions_uncertain: int = 0
    #: Step 4: scheduled occurrences skipped with `skipped_restore`.
    schedules_skipped: int = 0
    privacy_pending: bool = True


async def restore_pending(session: AsyncSession) -> dict[str, Any] | None:
    """The `restore_pending` marker, if the restore script left one."""
    row = await session.get(SystemState, KEY_RESTORE_PENDING)
    return None if row is None else dict(row.value)


async def assert_not_restore_pending(session: AsyncSession) -> None:
    marker = await restore_pending(session)
    if marker is not None:
        raise RestorePendingError(
            "the database was restored from a backup; run "
            "`python -m assistant post-restore --backup-taken-at <ts>`, then "
            "reconcile privacy using the journal or documented manual/purge fallback"
        )


async def post_restore(
    session_factory: async_sessionmaker[AsyncSession], clock: Clock, *, backup_taken_at: datetime
) -> PostRestoreReport:
    now = clock.now()
    if backup_taken_at > now:
        raise ValueError("backup_taken_at is in the future")
    # Commit the gate first, even for a direct invocation without restore.sh.
    # A later failure (including a separately committed sweep) cannot open it.
    async with transaction(session_factory) as uow:
        from assistant.privacy.journal import anchors

        marker = await uow.session.get(SystemState, KEY_RESTORE_PENDING)
        if marker is None:
            marker = SystemState(key=KEY_RESTORE_PENDING, value={})
            uow.session.add(marker)
        if "restore_id" not in marker.value:
            marker.value = {**marker.value, "restore_id": str(uuid4())}
        if "journal_anchors" not in marker.value:
            heads = await anchors(uow.session)
            cutoff = datetime.fromisoformat(marker.value.get("recovery_cutoff", now.isoformat()))
            if cutoff.tzinfo is None or not backup_taken_at <= cutoff <= now:
                raise ValueError("restore_cutoff_invalid")
            marker.value = {
                **marker.value,
                "backup_taken_at": backup_taken_at.isoformat(),
                "recovery_cutoff": cutoff.isoformat(),
                "journal_anchors": [head.model_dump(mode="json") for head in heads],
                "journal_supported": marker.value.get("journal_supported", bool(heads)),
                "privacy_reason": "privacy_reconciliation_required",
            }
            blocked = await uow.session.get(SystemState, "privacy_journal_upload_blocked")
            if blocked is None:
                uow.session.add(
                    SystemState(
                        key="privacy_journal_upload_blocked", value={"reason": "restore_pending"}
                    )
                )
        elif marker.value.get("backup_taken_at") != backup_taken_at.isoformat():
            raise ValueError("restore_backup_mismatch")
    async with transaction(session_factory) as uow:
        session = uow.session
        # Recovery itself must not append new decisions on the rewound chain.
        await session.execute(text("SET LOCAL app.privacy_replay = 'on'"))
        # Revoke every restored scope, including inactive accounts: a later
        # reactivation must not resurrect an old grant. Invalidate old cards
        # and text questions so only a newly displayed consent can re-grant.
        for account in (await session.scalars(select(Account).order_by(Account.id))).all():
            await cancel_restored_requests(session, account_id=account.id, now=now)
            account.settings = {
                **(account.settings or {}),
                service.RESTORE_CONSENT_PENDING: [
                    service.CONSENT_AI_CONTROL,
                    service.CONSENT_AI_MANAGED,
                ],
            }
            for kind in (service.CONSENT_AI_CONTROL, service.CONSENT_AI_MANAGED):
                await service.revoke_consent(session, account, kind=kind, clock=clock)
            await session.execute(delete(PendingInput).where(PendingInput.account_id == account.id))
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
        # 4. No schedule from a rewound backup fires by itself (S05.3b §3.7):
        #    missed and claimed occurrences are skipped with a note, and the
        #    next run is recomputed from now.
        schedules_skipped = await schedules.skip_for_restore(session, now)
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
        payload = {
            "backup_taken_at": backup_taken_at.isoformat(),
            "at": now.isoformat(),
            "actions": action_notices,
            # Per account: how many scheduled items step 4 skipped (counts only).
            "schedules_skipped": {
                str(account_id): count for account_id, count in schedules_skipped.items()
            },
        }
        marker = await session.get(SystemState, KEY_RESTORE_PENDING)
        assert marker is not None  # committed before any recovery work
        previous = marker.value.get("recovery_notice")
        if (
            isinstance(previous, dict)
            and previous.get("backup_taken_at") == payload["backup_taken_at"]
        ):
            # A retry after a failed sweep sees already-cancelled actions.
            # Keep their original receipt rather than replacing it with zeros.
            payload = previous
        else:
            marker.value = {**marker.value, "recovery_notice": payload}
        await session.flush()
    # 6. Retention, outside the big transaction (each class commits on its own).
    sweep = await sweep_all(session_factory, clock)
    # I20: maintenance and fresh consent do not prove deletion reconciliation.
    # Only a validated independent journal or an explicit purge may open it.
    async with transaction(session_factory) as uow:
        marker = await uow.session.get(SystemState, KEY_RESTORE_PENDING)
        if marker is not None:
            marker.value = {**marker.value, "maintenance_complete": True}
    return PostRestoreReport(
        accounts_paused,
        jobs_deleted,
        len(accounts),
        sweep,
        actions_cancelled,
        actions_uncertain,
        sum(schedules_skipped.values()),
    )
