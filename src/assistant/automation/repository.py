"""Account-scoped automation persistence; jobs are triggers, these rows are truth."""

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import column, select, table, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.automation.models import Automation
from assistant.automation.rules import RuleBase
from assistant.core.clock import Clock
from assistant.core.errors import NotInAccountError

_KINDS = frozenset(
    {"reminder", "monitor", "follow_up", "scheduled_summary", "delegation_end", "digest"}
)
_STATUSES = frozenset({"active", "paused", "completed", "cancelled", "expired"})
_ENDED = frozenset({"completed", "cancelled", "expired"})
_REF_TABLES = {"conversation_ids": "conversations", "contact_ids": "contacts"}


async def _check_ids(
    session: AsyncSession, *, account_id: int, table_name: str, ids: list[int]
) -> None:
    if not ids:
        return
    if any(type(identifier) is not int or identifier <= 0 for identifier in ids):
        raise NotInAccountError("not found")
    target = table(table_name, column("id"), column("account_id"), schema="app")
    found = set(
        (
            await session.scalars(
                select(target.c.id).where(target.c.account_id == account_id, target.c.id.in_(ids))
            )
        ).all()
    )
    if found != set(ids):
        raise NotInAccountError("not found")


async def create(
    session: AsyncSession,
    *,
    account_id: int,
    kind: str,
    spec: RuleBase,
    refs: Mapping[str, list[int]],
    timezone: str,
    tz_policy: str,
    clock: Clock,
    rrule: str | None = None,
    run_at: datetime | None = None,
    next_run_at: datetime | None = None,
    expires_at: datetime | None = None,
    task_id: int | None = None,
    run_id: int | None = None,
    tool_call_id: str | None = None,
    confirmed_at: datetime | None = None,
) -> Automation:
    if kind not in _KINDS or tz_policy not in {"fixed", "follow_owner"}:
        raise ValueError("invalid automation kind or timezone policy")
    if run_id is not None:
        await _check_ids(session, account_id=account_id, table_name="agent_runs", ids=[run_id])
        if tool_call_id is not None:
            existing = await session.scalar(
                select(Automation).where(
                    Automation.account_id == account_id,
                    Automation.run_id == run_id,
                    Automation.tool_call_id == tool_call_id,
                )
            )
            if existing is not None:
                return existing
    for key, ids in refs.items():
        if key not in _REF_TABLES:
            raise ValueError("invalid automation reference kind")
        await _check_ids(session, account_id=account_id, table_name=_REF_TABLES[key], ids=ids)
    if task_id is not None:
        await _check_ids(session, account_id=account_id, table_name="tasks", ids=[task_id])
    now = clock.now()
    statement = (
        insert(Automation)
        .values(
            account_id=account_id,
            kind=kind,
            spec=spec.model_dump(mode="json"),
            refs={key: list(ids) for key, ids in refs.items()},
            timezone=timezone,
            tz_policy=tz_policy,
            rrule=rrule,
            run_at=run_at,
            next_run_at=next_run_at,
            expires_at=expires_at,
            task_id=task_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            confirmed_at=confirmed_at,
            status="active",
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["run_id", "tool_call_id"])
        .returning(Automation)
    )
    created = await session.scalar(statement)
    if created is not None:
        return created
    existing = await session.scalar(
        select(Automation).where(
            Automation.account_id == account_id,
            Automation.run_id == run_id,
            Automation.tool_call_id == tool_call_id,
        )
    )
    if existing is None:
        raise NotInAccountError("not found")
    return existing


async def get(session: AsyncSession, *, account_id: int, automation_id: int) -> Automation | None:
    return await session.scalar(
        select(Automation).where(
            Automation.account_id == account_id, Automation.id == automation_id
        )
    )


async def list_active(
    session: AsyncSession, *, account_id: int, kind: str | None = None
) -> list[Automation]:
    statement = select(Automation).where(
        Automation.account_id == account_id, Automation.status == "active"
    )
    if kind is not None:
        statement = statement.where(Automation.kind == kind)
    return list((await session.scalars(statement.order_by(Automation.id))).all())


async def set_status(
    session: AsyncSession, *, account_id: int, automation_id: int, status: str, clock: Clock
) -> bool:
    if status not in _STATUSES:
        raise ValueError("invalid automation status")
    now = clock.now()
    changed = await session.scalar(
        update(Automation)
        .where(Automation.account_id == account_id, Automation.id == automation_id)
        .values(status=status, updated_at=now, ended_at=now if status in _ENDED else None)
        .returning(Automation.id)
    )
    return changed is not None
