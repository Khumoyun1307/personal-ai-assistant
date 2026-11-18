"""I16: real call sites through the production-style admitted chain.

The owner runtime (`complete`) and triage (`parse`) run over
consent → `AdmittedLLMClient` → FakeLLM, as `bootstrap.build_llm_client`
wires them. Each call is reserved, dispatched and settled with its run link;
a budget denial reaches the owner as the budget text, an unpriced route as a
distinct error class; and a caller holding the account lock in an open
transaction fails fast and closed instead of waiting forever.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy.exc
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.agent.models import AgentRun
from assistant.agent.persistence import DbConsentReader
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime, RunStatus
from assistant.agent.tools.registry import default_registry
from assistant.agent.triage import TriageConfig, Triager, TriageStatus
from assistant.llm.admitted import AdmittedLLMClient
from assistant.llm.consent import ConsentChecker, ConsentGatedLLMClient
from assistant.llm.models import LlmAdmission
from tests.fakes.channel import FakeChannel
from tests.fakes.clock import FakeClock
from tests.fakes.llm import FakeLLM, text_response
from tests.integration.business_env import Env
from tests.integration.test_agent_runtime import clock, factory, owner_turn  # noqa: F401
from tests.integration.test_llm_admission import request
from tests.integration.test_triage import RecordingStore, echo, seed, tenv  # noqa: F401

pytestmark = pytest.mark.integration


def admitted_chain(
    sessions: async_sessionmaker[AsyncSession],
    clk: FakeClock,
    fake: FakeLLM,
    *,
    limit: Decimal | None = None,
    lock_timeout_ms: int = 10_000,
) -> ConsentGatedLLMClient:
    consent = DbConsentReader(sessions)
    admitted = AdmittedLLMClient(
        fake,
        sessions,
        clk,
        before_dispatch=ConsentChecker(consent, processor=fake.processor),
        monthly_budget_usd=limit,
        lock_timeout_ms=lock_timeout_ms,
    )
    return ConsentGatedLLMClient(admitted, consent)


def owner_runtime(
    sessions: async_sessionmaker[AsyncSession],
    clk: FakeClock,
    fake: FakeLLM,
    channel: FakeChannel,
    *,
    model: str = "gpt-6-luna",
    limit: Decimal | None = None,
) -> OwnerRequestRuntime:
    return OwnerRequestRuntime(
        session_factory=sessions,
        llm=admitted_chain(sessions, clk, fake, limit=limit),
        channel=channel,
        registry=default_registry(),
        clock=clk,
        config=AgentConfig(model=model, monthly_budget_usd=limit),
    )


async def admissions(sessions: async_sessionmaker[AsyncSession]) -> list[LlmAdmission]:
    async with sessions() as session:
        return list(await session.scalars(select(LlmAdmission).order_by(LlmAdmission.id)))


async def runs(sessions: async_sessionmaker[AsyncSession]) -> list[AgentRun]:
    async with sessions() as session:
        return list(await session.scalars(select(AgentRun).order_by(AgentRun.id)))


async def test_owner_runtime_calls_are_admitted_and_settled_with_their_run(
    factory: async_sessionmaker[AsyncSession],  # noqa: F811
    clock: FakeClock,  # noqa: F811
) -> None:
    turn_id = await owner_turn(factory, clock)
    fake, channel = FakeLLM(text_response("Synthetic answer.", tokens=300)), FakeChannel()

    outcome = await owner_runtime(factory, clock, fake, channel).run(turn_id)

    assert outcome.status is RunStatus.SUCCEEDED
    [run] = await runs(factory)
    [row] = await admissions(factory)
    assert (row.status, row.run_id) == ("completed", run.id)
    assert row.llm_call_id is not None
    assert row.charged_usd is not None
    assert row.charged_usd < row.reserved_usd


async def test_a_budget_denial_reaches_the_owner_as_the_budget_text(
    factory: async_sessionmaker[AsyncSession],  # noqa: F811
    clock: FakeClock,  # noqa: F811
) -> None:
    """Pre-check `spent >= limit` passes; admission `spent + reserve > limit` denies."""
    turn_id = await owner_turn(factory, clock)
    fake, channel = FakeLLM(text_response("never")), FakeChannel()

    outcome = await owner_runtime(factory, clock, fake, channel, limit=Decimal("0.0000001")).run(
        turn_id
    )

    assert outcome.status is RunStatus.BUDGET_EXCEEDED
    assert fake.requests == []
    assert len(channel.sends) == 1
    assert "budget" in channel.sends[0]
    [run] = await runs(factory)
    assert run.error_class == "monthly_budget"
    assert await admissions(factory) == []


async def test_an_unpriced_route_fails_with_a_distinct_error_class(
    factory: async_sessionmaker[AsyncSession],  # noqa: F811
    clock: FakeClock,  # noqa: F811
) -> None:
    turn_id = await owner_turn(factory, clock)
    fake, channel = FakeLLM(text_response("never")), FakeChannel()

    outcome = await owner_runtime(factory, clock, fake, channel, model="gpt-6-astra").run(turn_id)

    assert outcome.status is RunStatus.FAILED
    assert fake.requests == []
    [run] = await runs(factory)
    assert run.error_class == "llm_not_admitted_unpriced"


async def test_triage_parse_calls_are_admitted_and_settled(tenv: Env) -> None:  # noqa: F811
    a, _ = await seed(tenv)
    fake = FakeLLM()
    fake.parsed = [echo]
    triager = Triager(
        session_factory=tenv.factory,
        llm=admitted_chain(tenv.factory, tenv.clock, fake),
        clock=tenv.clock,
        config=TriageConfig(model="gpt-6-luna"),
        store=RecordingStore(),
    )

    assert (await triager.triage(a)).status is TriageStatus.TRIAGED

    [row] = await admissions(tenv.factory)
    [run] = await runs(tenv.factory)
    assert (row.status, row.run_id) == ("completed", run.id)


class RepeatingStore(RecordingStore):
    """Records whether a failure counts as a repeating content error (I2)."""

    def __init__(self) -> None:
        super().__init__()
        self.repeating: list[bool] = []

    async def fail(self, session: AsyncSession, **kwargs: Any) -> None:
        self.repeating.append(kwargs["repeating"])
        await super().fail(session, **kwargs)


async def test_triage_admission_denial_is_transient_not_a_repeating_error(
    tenv: Env,  # noqa: F811
) -> None:
    """A denied admission sent nothing and cost nothing: I2's 5-minute
    transient retry applies, never the repeating-content degradation."""
    a, _ = await seed(tenv)
    fake = FakeLLM()
    store = RepeatingStore()
    triager = Triager(
        session_factory=tenv.factory,
        llm=admitted_chain(tenv.factory, tenv.clock, fake),
        clock=tenv.clock,
        config=TriageConfig(model="gpt-6-astra"),  # no recorded price
        store=store,
    )

    outcome = await triager.triage(a)

    assert outcome.status is not TriageStatus.TRIAGED
    assert fake.requests == []
    assert store.failures == ["llm_not_admitted"]
    assert store.repeating == [False]
    assert await admissions(tenv.factory) == []


async def test_a_caller_holding_the_account_lock_fails_fast_and_closed(
    factory: async_sessionmaker[AsyncSession],  # noqa: F811
    clock: FakeClock,  # noqa: F811
) -> None:
    turn_id = await owner_turn(factory, clock)
    fake = FakeLLM(text_response("never"))
    llm = admitted_chain(factory, clock, fake, lock_timeout_ms=200)
    async with factory() as holder:
        await holder.begin()
        account_id = (
            await holder.execute(
                text("SELECT account_id FROM app.control_turns WHERE id = :t"), {"t": turn_id}
            )
        ).scalar_one()
        await holder.execute(
            text("SELECT id FROM app.accounts WHERE id = :a FOR UPDATE"), {"a": account_id}
        )
        with pytest.raises(sqlalchemy.exc.DBAPIError, match="lock timeout"):
            await llm.complete(request(account_id))
        await holder.rollback()

    assert fake.requests == []
    assert await admissions(factory) == []
