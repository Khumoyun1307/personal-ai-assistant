"""Inspect and reset derived suppression using existing, content-free feedback."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.inbox.models import Notification
from assistant.inbox.notification_policy import (
    MUTE_THRESHOLD,
    MUTE_WINDOW,
    FeedbackMark,
    feedback_scope,
    muted_pairs,
)


def monitor_id(refs: Mapping[str, Any]) -> int | None:
    """Only an unambiguous, ids-only monitor reference can define a mute."""
    ids = refs.get("automation_ids")
    if isinstance(ids, list) and len(ids) == 1:
        value = ids[0]
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def mark(row: Notification) -> FeedbackMark:
    return FeedbackMark(
        row.kind, row.contact_id, row.feedback or "", row.updated_at, monitor_id(row.refs)
    )


@dataclass(frozen=True, slots=True)
class DerivedMute:
    notification_id: int
    kind: str
    contact_id: int | None
    monitor_id: int | None
    since: datetime


async def feedback_rows(
    session: AsyncSession, *, account_id: int, now: datetime
) -> list[Notification]:
    return list(
        await session.scalars(
            select(Notification).where(
                Notification.account_id == account_id,
                Notification.feedback.is_not(None),
                Notification.updated_at >= now - MUTE_WINDOW,
            )
        )
    )


async def list_mutes(session: AsyncSession, *, account_id: int, now: datetime) -> list[DerivedMute]:
    rows = await feedback_rows(session, account_id=account_id, now=now)
    muted = muted_pairs((mark(row) for row in rows), now)
    result: list[DerivedMute] = []
    for scope in sorted(muted, key=lambda scope: (scope[0], scope[1] or 0)):
        negative = sorted(
            (
                row
                for row in rows
                if row.feedback == "not_useful"
                and feedback_scope(row.kind, row.contact_id, monitor_id(row.refs)) == scope
            ),
            key=lambda row: (row.updated_at, row.id),
        )
        anchor = negative[0]
        since = negative[MUTE_THRESHOLD - 1].updated_at
        useful = [
            row.updated_at
            for row in rows
            if row.feedback == "useful"
            and feedback_scope(row.kind, row.contact_id, monitor_id(row.refs)) == scope
        ]
        if useful:
            # A useful mark temporarily lifted this mute. Show when it resumed.
            resumed_at = next(row.updated_at for row in negative if row.updated_at >= max(useful))
            since = max(since, resumed_at)
        result.append(
            DerivedMute(
                anchor.id,
                anchor.kind,
                anchor.contact_id,
                monitor_id(anchor.refs),
                since,
            )
        )
    return result


async def reset_mutes(
    session: AsyncSession, *, account_id: int, notification_id: int | None = None
) -> bool:
    """Clear one scope's marks or all marks. Delivery state is never changed.

    Resetting all also clears sub-threshold marks, so they cannot immediately
    recreate the mute. An anchor from another account reveals/changes nothing.
    """
    scope = None
    if notification_id is not None:
        anchor = await session.scalar(
            select(Notification).where(
                Notification.account_id == account_id, Notification.id == notification_id
            )
        )
        if anchor is None:
            return False
        scope = feedback_scope(anchor.kind, anchor.contact_id, monitor_id(anchor.refs))
    rows = await session.scalars(
        select(Notification)
        .where(Notification.account_id == account_id, Notification.feedback.is_not(None))
        .with_for_update()
    )
    for row in rows:
        if scope is None or feedback_scope(row.kind, row.contact_id, monitor_id(row.refs)) == scope:
            row.feedback = None
    await session.flush()
    return True
