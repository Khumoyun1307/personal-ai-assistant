"""The owner agent loop against a real database (ARCHITECTURE §7.2, S01.4).

Wired as in production — consent gate → metering → model — with FakeLLM in
place of OpenAI. Pinned claims:

- no model call without `ai_control` consent (§6 invariant 9);
- the loop terminates on iterations, tokens and wall-clock time;
- tool results go back in one batch, after the model's own output, unchanged;
- refusals, truncation and provider outages give an honest reply, no crash;
- every model call is metered in `llm_calls`; every run is recorded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.accounts import service
from assistant.accounts.models import Account, ControlTurn
from assistant.agent import runtime as rt
from assistant.agent.models import AgentRun, AgentRunStep, LlmCall
from assistant.agent.persistence import DbCallRecorder, DbConsentReader
from assistant.agent.ports import Delivery
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime, RunStatus
from assistant.agent.tools.registry import default_registry
from assistant.db.engine import build_engine, build_session_factory
from assistant.db.uow import transaction
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.llm.metering import MeteredLLMClient
from assistant.llm.port import (
    LLMError,
    LLMErrorKind,
    LLMRequest,
    Provenance,
    ProviderItems,
    StopReason,
    TextItem,
    ToolResultItem,
)
from tests.fakes.clock import FakeClock
from tests.fakes.llm import FakeLLM, stop_response, text_response, tool_response

pytestmark = pytest.mark.integration

OWNER = 7000000001
CHAT = 7000000001
STATUS_FLAG = frozenset({"tool.get_status"})


@dataclass
class FakeChannel:
    sends: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    deliver: bool = True
    _next_id: int = 500

    async def send(self, *, account_id: int, chat_id: int, text: str) -> Delivery:
        self.sends.append(text)
        self.events.append("send")
        self._next_id += 1
        if not self.deliver:
            return Delivery(ok=False, error="forbidden")
        return Delivery(ok=True, message_id=self._next_id)

    async def typing(self, *, account_id: int, chat_id: int) -> None:
        self.events.append("typing")


@pytest.fixture
async def factory(migrated_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = build_engine(migrated_url)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("TRUNCATE app.accounts CASCADE")
    yield build_session_factory(engine)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("TRUNCATE app.accounts CASCADE")
    await engine.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


async def owner_turn(
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
    *,
    consent: bool = True,
    managed_consent: bool = False,
    history: list[tuple[str, str, bool]] = (),  # type: ignore[assignment]
    text: str = "Synthetic question?",
) -> int:
    async with transaction(factory) as uow:
        session = uow.session
        account, _ = await service.get_or_create_account(
            session, telegram_user_id=OWNER, clock=clock
        )
        await service.set_control_chat(session, account, CHAT)
        if consent:
            await service.grant_consent(
                session,
                account,
                kind="ai_control",
                policy_version="v0",
                processors=["OpenAI"],
                clock=clock,
            )
        if managed_consent:
            await service.grant_consent(
                session,
                account,
                kind="ai_managed",
                policy_version="v0",
                processors=["OpenAI"],
                clock=clock,
            )
        for role, past_text, managed in history:
            await service.record_control_turn(
                session,
                account_id=account.id,
                role=role,
                text=past_text,
                contains_managed=managed,
            )
        turn = await service.record_control_turn(
            session, account_id=account.id, role="owner", text=text, telegram_message_id=42
        )
        return turn.id


def build(
    factory: async_sessionmaker[AsyncSession],
    clock: FakeClock,
    fake: FakeLLM,
    channel: FakeChannel,
    **config: Any,
) -> OwnerRequestRuntime:
    llm = ConsentGatedLLMClient(
        MeteredLLMClient(fake, DbCallRecorder(factory, clock)), DbConsentReader(factory)
    )
    settings: dict[str, Any] = {"model": "gpt-6-luna"}
    settings.update(config)
    return OwnerRequestRuntime(
        session_factory=factory,
        llm=llm,
        channel=channel,
        registry=default_registry(),
        clock=clock,
        config=AgentConfig(**settings),
    )


async def rows(factory: async_sessionmaker[AsyncSession], model: Any) -> list[Any]:
    async with factory() as session:
        return list((await session.execute(select(model).order_by(model.id))).scalars().all())


# ------------------------------------------------------------------ happy path
async def test_an_owner_turn_gets_a_reply_and_everything_is_recorded(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    fake, channel = FakeLLM(text_response("Synthetic answer.", tokens=300)), FakeChannel()

    outcome = await build(factory, clock, fake, channel).run(turn_id)

    assert outcome.status is RunStatus.SUCCEEDED
    assert channel.sends == ["Synthetic answer."]
    assert channel.events == ["typing", "send"]
    [run] = await rows(factory, AgentRun)
    assert (run.status, run.iterations, run.prompt_version) == ("succeeded", 1, "owner_request.v1")
    assert run.trigger_ref == f"turn:{turn_id}"
    assert run.finished_at is not None
    reply = (await rows(factory, ControlTurn))[-1]
    assert (reply.role, reply.text, reply.run_id) == ("assistant", "Synthetic answer.", run.id)
    assert reply.telegram_message_id == outcome.reply_message_id
    [call] = await rows(factory, LlmCall)
    assert (call.run_id, call.purpose, call.input_tokens, call.output_tokens) == (
        run.id,
        "owner_request",
        300,
        100,
    )
    assert call.cost_estimate == Decimal("0.000080")


async def test_the_request_carries_the_prompt_profile_and_history(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(
        factory,
        clock,
        history=[("owner", "Earlier synthetic", False), ("assistant", "Reply", False)],
    )
    fake = FakeLLM(text_response("ok"))

    await build(factory, clock, fake, FakeChannel()).run(turn_id)

    [request] = fake.requests
    assert "personal assistant" in request.instructions
    assert "timezone: UTC" in request.instructions
    assert [(i.role, i.text) for i in request.items if isinstance(i, TextItem)] == [
        ("user", "Earlier synthetic"),
        ("assistant", "Reply"),
        ("user", "Synthetic question?"),
    ]
    assert request.prompt_cache_key == "owner_request:owner_request.v1"
    assert request.effort == "medium"


# --------------------------------------------------------------------- consent
async def test_without_consent_the_model_is_never_called(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock, consent=False)
    fake, channel = FakeLLM(), FakeChannel()

    outcome = await build(factory, clock, fake, channel).run(turn_id)

    assert outcome.status is RunStatus.SKIPPED
    assert fake.requests == []
    assert channel.sends == [rt.TEXT_NEEDS_CONSENT]
    assert await rows(factory, AgentRun) == []
    assert await rows(factory, LlmCall) == []


async def test_managed_history_is_excluded_without_managed_consent(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(
        factory, clock, history=[("assistant", "Synthetic managed summary", True)]
    )
    fake = FakeLLM(text_response("ok"))

    await build(factory, clock, fake, FakeChannel()).run(turn_id)

    texts = [i.text for i in fake.requests[0].items if isinstance(i, TextItem)]
    assert "Synthetic managed summary" not in texts


async def test_managed_history_is_included_and_marked_with_managed_consent(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(
        factory,
        clock,
        managed_consent=True,
        history=[("assistant", "Synthetic managed summary", True)],
    )
    fake = FakeLLM(text_response("ok"))

    await build(factory, clock, fake, FakeChannel()).run(turn_id)

    managed = [i for i in fake.requests[0].items if i.provenance is Provenance.MANAGED]
    assert [i.text for i in managed if isinstance(i, TextItem)] == ["Synthetic managed summary"]
    reply = (await rows(factory, ControlTurn))[-1]
    assert reply.contains_managed is True


# ----------------------------------------------------------------------- tools
async def test_a_tool_call_round_trip_appends_output_then_all_results(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    seen: list[LLMRequest] = []

    def second_call(request: LLMRequest) -> Any:
        seen.append(request)
        return text_response("You are active.")

    fake = FakeLLM(tool_response(("c1", "get_status", "{}")), second_call)

    outcome = await build(factory, clock, fake, FakeChannel(), enabled_flags=STATUS_FLAG).run(
        turn_id
    )

    assert outcome.status is RunStatus.SUCCEEDED
    assert [t.name for t in fake.requests[0].tools] == ["get_status"]
    tail = seen[0].items[-2:]
    assert isinstance(tail[0], ProviderItems)
    assert isinstance(tail[1], ToolResultItem)
    assert tail[1].call_id == "c1"
    assert '"status": "ok"' in tail[1].output
    assert '"business_connection": "none"' in tail[1].output
    [step] = await rows(factory, AgentRunStep)
    assert (step.tool_name, step.status, step.seq) == ("get_status", "ok", 1)
    [run] = await rows(factory, AgentRun)
    assert run.iterations == 2
    assert len(await rows(factory, LlmCall)) == 2


async def test_tools_are_not_offered_while_their_flag_is_off(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    fake = FakeLLM(text_response("ok"))

    await build(factory, clock, fake, FakeChannel()).run(turn_id)

    assert fake.requests[0].tools == ()


# --------------------------------------------------------------------- budgets
async def test_the_loop_stops_at_the_iteration_limit(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    fake = FakeLLM(*(tool_response((f"c{i}", "get_status", "{}")) for i in range(3)))
    channel = FakeChannel()

    outcome = await build(
        factory, clock, fake, channel, max_iterations=3, enabled_flags=STATUS_FLAG
    ).run(turn_id)

    assert outcome.status is RunStatus.FAILED
    assert len(fake.requests) == 3
    assert channel.sends == [rt.TEXT_LIMITS]
    [run] = await rows(factory, AgentRun)
    assert (run.status, run.error_class, run.iterations) == ("failed", "run_limits", 3)


async def test_the_loop_stops_when_the_token_budget_is_spent(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    fake = FakeLLM(tool_response(("c1", "get_status", "{}"), tokens=60))

    outcome = await build(
        factory, clock, fake, FakeChannel(), run_token_budget=50, enabled_flags=STATUS_FLAG
    ).run(turn_id)

    assert outcome.status is RunStatus.FAILED
    assert len(fake.requests) == 1


async def test_the_loop_stops_at_the_wall_clock_deadline(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)

    def slow(_request: LLMRequest) -> Any:
        clock.advance(minutes=6)
        return tool_response(("c1", "get_status", "{}"))

    fake = FakeLLM(slow)

    outcome = await build(
        factory,
        clock,
        fake,
        FakeChannel(),
        run_timeout=timedelta(minutes=5),
        enabled_flags=STATUS_FLAG,
    ).run(turn_id)

    assert outcome.status is RunStatus.FAILED
    assert len(fake.requests) == 1


async def test_a_reached_monthly_budget_means_no_model_call(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    async with transaction(factory) as uow:
        account = (await uow.session.execute(select(Account))).scalar_one()
        uow.session.add(
            LlmCall(
                account_id=account.id,
                purpose="owner_request",
                model="gpt-6-luna",
                cost_estimate=Decimal("5.00"),
                created_at=clock.now(),
            )
        )
    fake, channel = FakeLLM(), FakeChannel()

    outcome = await build(factory, clock, fake, channel, monthly_budget_usd=Decimal("5")).run(
        turn_id
    )

    assert outcome.status is RunStatus.BUDGET_EXCEEDED
    assert fake.requests == []
    assert channel.sends == [rt.TEXT_BUDGET_REACHED]


# -------------------------------------------------------- honest failure modes
async def test_a_refusal_gets_a_safe_reply(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    channel = FakeChannel()

    outcome = await build(factory, clock, FakeLLM(stop_response(StopReason.REFUSAL)), channel).run(
        turn_id
    )

    assert outcome.status is RunStatus.REFUSED
    assert channel.sends == [rt.TEXT_REFUSED]


async def test_a_provider_outage_is_reported_and_nothing_crashes(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    channel = FakeChannel()

    outcome = await build(factory, clock, FakeLLM(LLMError(LLMErrorKind.UNAVAILABLE)), channel).run(
        turn_id
    )

    assert outcome.status is RunStatus.FAILED
    assert channel.sends == [rt.TEXT_PROVIDER_DOWN]
    [run] = await rows(factory, AgentRun)
    assert run.error_class == "llm_unavailable"
    assert await rows(factory, LlmCall) == []


async def test_truncated_output_is_retried_once_with_a_larger_budget(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)
    fake = FakeLLM(stop_response(StopReason.MAX_TOKENS), text_response("Shorter answer."))
    channel = FakeChannel()

    outcome = await build(factory, clock, fake, channel, max_output_tokens=1000).run(turn_id)

    assert outcome.status is RunStatus.SUCCEEDED
    assert [r.max_output_tokens for r in fake.requests] == [1000, 2000]
    assert channel.sends == ["Shorter answer."]


async def test_an_undelivered_reply_is_recorded_as_such(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    turn_id = await owner_turn(factory, clock)

    await build(factory, clock, FakeLLM(text_response("ok")), FakeChannel(deliver=False)).run(
        turn_id
    )

    [run] = await rows(factory, AgentRun)
    assert run.error_class == "reply_not_delivered"


async def test_an_assistant_turn_never_starts_a_run(
    factory: async_sessionmaker[AsyncSession], clock: FakeClock
) -> None:
    await owner_turn(factory, clock)
    async with transaction(factory) as uow:
        account = (await uow.session.execute(select(Account))).scalar_one()
        turn = await service.record_control_turn(
            uow.session, account_id=account.id, role="assistant", text="x"
        )
    fake = FakeLLM()

    outcome = await build(factory, clock, fake, FakeChannel()).run(turn.id)

    assert outcome.status is RunStatus.SKIPPED
    assert fake.requests == []
