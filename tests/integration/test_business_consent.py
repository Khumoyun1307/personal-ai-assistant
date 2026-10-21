"""Consent gates AI processing of managed content (§6 invariant 9, ARCH §14.3).

Named regression test RG-08 lives here: never weaken or delete it. Managed
content is *stored* without `ai_managed` consent (coverage and visibility are
not AI processing), but it must never reach the model — not through context,
not through a tool result — until the consent exists.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from assistant.accounts import service
from assistant.accounts.models import Account, ControlTurn
from assistant.accounts.service import ConsentMissingError
from assistant.agent.persistence import DbCallRecorder, DbConsentReader
from assistant.agent.runtime import AgentConfig, OwnerRequestRuntime, RunStatus
from assistant.agent.tools.registry import default_registry
from assistant.conversations.models import Message
from assistant.conversations.service import conversations_awaiting_ai
from assistant.llm.consent import ConsentGatedLLMClient
from assistant.llm.metering import MeteredLLMClient
from assistant.llm.port import LLMRequest, Provenance, TextItem
from tests.fakes.channel import FakeChannel
from tests.fakes.llm import FakeLLM, text_response
from tests.integration.business_env import (
    OWNER,
    Env,
    business_message,
    connect,
    conversation,
    deliver,
    job_names,
    private_message,
)

pytestmark = pytest.mark.integration

CANARY_TEXT = "CANARY-SYNTH-7f3a managed text"
CANARY_NAME = "CanaryName-7f3a"


async def _owner_account(env: Env) -> Account:
    async with env.factory() as session:
        return (
            await session.execute(select(Account).where(Account.telegram_user_id == OWNER))
        ).scalar_one()


def _runtime(env: Env, fake: FakeLLM, channel: FakeChannel) -> OwnerRequestRuntime:
    llm = ConsentGatedLLMClient(
        MeteredLLMClient(fake, DbCallRecorder(env.factory, env.clock)),
        DbConsentReader(env.factory),
    )
    return OwnerRequestRuntime(
        session_factory=env.factory,
        llm=llm,
        channel=channel,
        registry=default_registry(),
        clock=env.clock,
        config=AgentConfig(
            model="gpt-6-luna",
            monthly_budget_usd=Decimal("5"),
            enabled_flags=frozenset({"tool.get_status"}),
        ),
    )


async def test_a_pre_consent_message_is_stored_and_no_llm_job_is_queued(env: Env) -> None:
    await connect(env)  # ai_control consent only; ai_managed never granted
    await deliver(env, business_message(message_text=CANARY_TEXT, first_name=CANARY_NAME))

    async with env.factory() as session:
        stored = (await session.execute(select(Message))).scalars().all()
        awaiting = await conversations_awaiting_ai(session, (await _owner_account(env)).id)
    assert len(stored) == 1
    assert stored[0].text == CANARY_TEXT
    assert awaiting == []
    # The only job the connection raised is the owner notice; nothing LLM-bound.
    assert set(await job_names(env)) <= {"connections.notify_owner"}


async def test_granting_managed_consent_releases_flagged_conversations(env: Env) -> None:
    await connect(env)
    await deliver(env, business_message(message_text="synthetic waiting"))
    account = await _owner_account(env)
    async with env.factory() as session:
        assert await conversations_awaiting_ai(session, account.id) == []

    await deliver(env, private_message(OWNER, "/privacy managed"))
    await deliver(env, private_message(OWNER, "yes"))

    conv = await conversation(env)
    assert conv is not None
    async with env.factory() as session:
        assert (await service.active_consent(session, account.id, kind="ai_managed")).granted
        assert await conversations_awaiting_ai(session, account.id) == [conv.id]


async def test_rg08_managed_content_never_reaches_the_llm_without_consent(env: Env) -> None:
    """RG-08 (never weaken). With canary text and a canary name stored from a
    managed chat and no `ai_managed` consent: an owner turn through the real
    runtime (consent gate → metering → FakeLLM, `get_status` enabled) sends
    the model nothing managed; and a request that *would* carry managed
    content is refused at the gate before the provider is called."""
    await connect(env)
    await deliver(env, business_message(message_text=CANARY_TEXT, first_name=CANARY_NAME))
    account = await _owner_account(env)
    async with env.factory() as session, session.begin():
        turn = await service.record_control_turn(
            session,
            account_id=account.id,
            role="owner",
            text="What did my contact write?",
            telegram_message_id=7,
        )
        turn_id = turn.id

    fake, channel = FakeLLM(text_response("I cannot read chats yet.")), FakeChannel()
    outcome = await _runtime(env, fake, channel).run(turn_id)

    assert outcome.status is RunStatus.SUCCEEDED
    assert fake.requests, "the owner turn itself must reach the model"
    for request in fake.requests:
        assert not request.contains_managed()
        assert CANARY_TEXT not in request.instructions
        assert CANARY_NAME not in request.instructions
        for item in request.items:
            dumped = repr(item)
            assert CANARY_TEXT not in dumped
            assert CANARY_NAME not in dumped
            assert item.provenance is not Provenance.MANAGED

    # And the gate itself: a managed item without consent never reaches the provider.
    gated = ConsentGatedLLMClient(fake, DbConsentReader(env.factory))
    before = len(fake.requests)
    request = LLMRequest(
        account_id=account.id,
        purpose="summarize",
        model="gpt-6-luna",
        instructions="synthetic",
        items=(TextItem(role="user", text=CANARY_TEXT, provenance=Provenance.MANAGED),),
        max_output_tokens=10,
        timeout_seconds=1.0,
    )
    with pytest.raises(ConsentMissingError):
        await gated.complete(request)
    assert len(fake.requests) == before

    async with env.factory() as session:
        turns = (await session.execute(select(ControlTurn))).scalars().all()
    assert all(CANARY_TEXT not in (t.text or "") for t in turns)
