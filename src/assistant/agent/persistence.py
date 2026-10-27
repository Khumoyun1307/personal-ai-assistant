"""Persistence for agent runs (ARCHITECTURE §10.3).

Account-scoped repositories over `control_turns`, `agent_runs`,
`agent_run_steps` and `llm_calls`, plus the database-backed implementations
of the two ports the LLM wrappers need (`ConsentReader`, `CallRecorder`).

Every write is a short transaction of its own: none is held across an LLM or
Telegram call (AGENTS §13).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts.models import ControlTurn
from assistant.accounts.service import ConsentState, active_consent
from assistant.agent.models import AgentRun, AgentRunStep, LlmCall
from assistant.core.clock import Clock
from assistant.core.errors import NotInAccountError
from assistant.db.repository import AccountScopedRepository
from assistant.db.uow import transaction
from assistant.llm.metering import estimate_cost
from assistant.llm.port import CallRecord


class ControlTurnRepository(AccountScopedRepository[ControlTurn]):
    model = ControlTurn

    async def recent_before(
        self, turn_id: int, *, limit: int, include_managed: bool
    ) -> list[ControlTurn]:
        """The `limit` turns before `turn_id`, oldest first.

        Turns flagged `contains_managed` are dropped unless the caller has
        established that `ai_managed` is active (ARCHITECTURE §14.3).
        """
        statement = self._select().where(ControlTurn.id < turn_id)
        if not include_managed:
            statement = statement.where(ControlTurn.contains_managed.is_(False))
        statement = statement.order_by(ControlTurn.id.desc()).limit(limit)
        result = await self.session.execute(statement)
        rows = cast("list[ControlTurn]", list(result.scalars().all()))
        rows.reverse()
        return rows


class AgentRunRepository(AccountScopedRepository[AgentRun]):
    model = AgentRun

    async def for_trigger(self, trigger_ref: str) -> list[AgentRun]:
        """Runs started for one trigger (e.g. `turn:42`), oldest first."""
        result = await self.session.execute(
            self._select().where(AgentRun.trigger_ref == trigger_ref).order_by(AgentRun.id)
        )
        return cast("list[AgentRun]", list(result.scalars().all()))

    async def add_step(
        self,
        run_id: int,
        *,
        tool_name: str,
        args_digest: str | None,
        status: str,
        latency_ms: int,
    ) -> AgentRunStep:
        """Append a content-free step. The run must belong to this account."""
        if await self.get(run_id) is None:
            raise NotInAccountError("run not found in this account")
        next_seq = (
            await self.session.execute(
                select(func.coalesce(func.max(AgentRunStep.seq), 0) + 1).where(
                    AgentRunStep.run_id == run_id
                )
            )
        ).scalar_one()
        step = AgentRunStep(
            run_id=run_id,
            seq=int(next_seq),
            tool_name=tool_name,
            args_digest=args_digest,
            status=status,
            latency_ms=latency_ms,
        )
        self.session.add(step)
        await self.session.flush()
        return step


class LlmCallRepository(AccountScopedRepository[LlmCall]):
    model = LlmCall

    async def spend_since(self, since: datetime) -> Decimal:
        """Estimated USD spent since `since` (NFR-10 budget checks)."""
        statement = select(func.sum(LlmCall.cost_estimate)).where(
            self._scope(), LlmCall.created_at >= since
        )
        total = (await self.session.execute(statement)).scalar_one()
        return Decimal(0) if total is None else Decimal(total)


async def account_of_turn(session: AsyncSession, turn_id: int) -> int | None:
    """Which account a control turn belongs to.

    The one unscoped lookup in the agent: a job carries only `turn_id`
    (AGENTS §13), and the account has to come from somewhere before the scoped
    repositories can be built. It returns an id, never a row.
    """
    result = await session.execute(select(ControlTurn.account_id).where(ControlTurn.id == turn_id))
    return result.scalar_one_or_none()


def args_digest(arguments: Any) -> str:
    """A stable hash of tool arguments, so a step can be matched without storing them."""
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def month_start(moment: datetime) -> datetime:
    """Start of the UTC calendar month. The budget month is UTC for now."""
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class DbConsentReader:
    """`ConsentReader` over the database: a fresh read on every call."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(self, account_id: int, kind: str) -> ConsentState:
        async with transaction(self._session_factory) as uow:
            return await active_consent(uow.session, account_id, kind=kind)


class DbCallRecorder:
    """`CallRecorder` over the database: one `llm_calls` row per call."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Clock) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def __call__(self, record: CallRecord) -> None:
        async with transaction(self._session_factory) as uow:
            LlmCallRepository(uow.session, record.account_id).add(
                LlmCall(
                    run_id=record.run_id,
                    purpose=record.purpose,
                    model=record.model,
                    input_tokens=record.usage.input_tokens,
                    output_tokens=record.usage.output_tokens,
                    cache_read_tokens=record.usage.cache_read_tokens,
                    cache_write_tokens=record.usage.cache_write_tokens,
                    cost_estimate=estimate_cost(record.model, record.usage),
                    request_id=record.request_id,
                    created_at=self._clock.now(),
                )
            )
