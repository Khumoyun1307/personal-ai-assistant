"""I16 / C7 regression first: deletion during dispatch keeps the reserve.

A model call is with the provider (FakeLLM paused inside its step) while the
owner deletes chat data, deletes everything, withdraws both AI consents and
the run row's linkage disappears. The admission identity and its reserve
must survive unchanged, a competing call must not be able to spend the held
allowance, and the late response settles the same attempt exactly once — or,
when no usage ever arrives, the attempt stays visibly `billing_uncertain`.

Synthetic text only; FakeLLM stands in for the provider.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from assistant.accounts.models import Account
from assistant.accounts.service import CONSENT_AI_CONTROL, CONSENT_AI_MANAGED, revoke_consent
from assistant.agent.models import AgentRun
from assistant.agent.persistence import DbConsentReader
from assistant.db.uow import transaction
from assistant.llm.admission import AdmissionDenied, CallOutcome, admit_call, settle_call
from assistant.llm.admitted import AdmittedLLMClient
from assistant.llm.consent import ConsentChecker, ConsentGatedLLMClient
from assistant.llm.models import LlmAdmission
from assistant.llm.port import (
    LLMError,
    LLMErrorKind,
    LLMRequest,
    Provenance,
    TextItem,
    Usage,
)
from assistant.privacy.deletion import delete_conversation_data, delete_everything
from tests.fakes.llm import FakeLLM, text_response
from tests.integration.business_env import (
    Env,
    business_message,
    connect,
    conversation,
    deliver,
    environment,
    grant_managed,
    messages,
)

pytestmark = pytest.mark.integration

SAID = "ADMK synthetic chat line for the deletion race"
#: Fits one reservation of `request()` (about USD 0.0005), never two.
LIMIT = Decimal("0.0008")


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


async def account_id(env: Env) -> int:
    async with env.factory() as session:
        return (await session.execute(select(Account.id))).scalar_one()


async def new_run(env: Env, account: int) -> int:
    async with transaction(env.factory) as uow:
        run = AgentRun(account_id=account, run_type="owner_request", model="gpt-6-luna")
        uow.session.add(run)
        await uow.session.flush()
        return run.id


def request(account: int, run_id: int | None) -> LLMRequest:
    return LLMRequest(
        account_id=account,
        purpose="owner_request",
        model="gpt-6-luna",
        instructions="synthetic instructions",
        items=(TextItem("user", "synthetic owner question", Provenance.OWNER_CONTROL),),
        max_output_tokens=1000,
        timeout_seconds=10,
        run_id=run_id,
    )


def client(env: Env, fake: FakeLLM) -> ConsentGatedLLMClient:
    """Production order: consent → admission (re-checks consent) → provider."""
    checker = ConsentChecker(DbConsentReader(env.factory), processor=fake.processor)
    admitted = AdmittedLLMClient(
        fake, env.factory, env.clock, before_dispatch=checker, monthly_budget_usd=LIMIT
    )
    return ConsentGatedLLMClient(admitted, DbConsentReader(env.factory))


async def admissions(env: Env) -> list[LlmAdmission]:
    async with env.factory() as session:
        rows = await session.scalars(select(LlmAdmission).order_by(LlmAdmission.id))
        return list(rows)


async def llm_call_rows(env: Env) -> list[dict[str, Any]]:
    async with env.factory() as session:
        result = await session.execute(text("SELECT id, run_id, cost_estimate FROM app.llm_calls"))
        return [dict(row._mapping) for row in result]


async def competing_admission(env: Env, account: int) -> object:
    async with transaction(env.factory) as uow:
        return await admit_call(
            uow.session,
            account_id=account,
            operation_id=uuid4(),
            run_id=None,
            attempt_id=uuid4(),
            model="gpt-6-luna",
            input_bound=100,
            output_bound=1000,
            now=env.clock.now(),
            monthly_budget_usd=LIMIT,
        )


async def delete_and_withdraw(env: Env, account: int, run_id: int) -> None:
    """Every deletion path that can meet an in-flight call, then the run link."""
    chat = await conversation(env)
    assert chat is not None
    async with transaction(env.factory) as uow:
        await delete_conversation_data(
            uow.session, account_id=account, conversation_id=chat.id, actor="owner", clock=env.clock
        )
    async with transaction(env.factory) as uow:
        await delete_everything(uow.session, account_id=account, actor="owner", clock=env.clock)
    async with transaction(env.factory) as uow:
        owner = await uow.session.get(Account, account)
        assert owner is not None
        await revoke_consent(uow.session, owner, kind=CONSENT_AI_MANAGED, clock=env.clock)
        await revoke_consent(uow.session, owner, kind=CONSENT_AI_CONTROL, clock=env.clock)
    # The run row goes too (retention / account-scope linkage removal).
    async with transaction(env.factory) as uow:
        await uow.session.execute(text("DELETE FROM app.agent_runs WHERE id = :id"), {"id": run_id})


async def seed(env: Env) -> tuple[int, int]:
    await connect(env)
    await grant_managed(env)
    await deliver(env, business_message(message_id=1, message_text=SAID))
    account = await account_id(env)
    return account, await new_run(env, account)


async def test_deletion_during_dispatch_keeps_reserve_and_settles_late_response_once(
    env: Env,
) -> None:
    account, run_id = await seed(env)
    during: dict[str, Any] = {}

    async def in_flight(_: LLMRequest) -> object:
        (row,) = await admissions(env)
        during["identity"] = (row.id, row.attempt_id, row.reserved_usd, row.status)
        await delete_and_withdraw(env, account, run_id)
        (after,) = await admissions(env)
        during["after_deletion"] = (after.id, after.attempt_id, after.reserved_usd, after.status)
        during["run_link"] = after.run_id
        during["competing"] = await competing_admission(env, account)
        return text_response("synthetic late answer", tokens=30)

    fake = FakeLLM(in_flight)
    response = await client(env, fake).complete(request(account, run_id))

    assert response.text == "synthetic late answer"
    admission_id, attempt_id, reserved, status = during["identity"]
    assert status == "dispatched"
    # Same identity and the full reserve survive every deletion path.
    assert during["after_deletion"] == (admission_id, attempt_id, reserved, "dispatched")
    assert during["run_link"] is None
    # The held allowance cannot be spent by anyone else meanwhile.
    assert during["competing"] == AdmissionDenied.MONTHLY_BUDGET
    # Source content is gone.
    assert await messages(env) == []

    (row,) = await admissions(env)
    assert (row.id, row.attempt_id, row.status) == (admission_id, attempt_id, "completed")
    assert row.charged_usd == Decimal("0.000008")  # 30 in + 10 out at Luna prices
    (call,) = await llm_call_rows(env)
    assert row.llm_call_id == call["id"]
    assert call["run_id"] is None

    # Exactly once: a repeated late settlement changes nothing.
    async with transaction(env.factory) as uow:
        again = await settle_call(
            uow.session,
            account_id=account,
            admission_id=admission_id,
            outcome=CallOutcome.COMPLETED,
            usage=Usage(input_tokens=500, output_tokens=500),
            actual_model="gpt-6-luna",
            now=env.clock.now(),
        )
    assert again is False
    assert len(await llm_call_rows(env)) == 1
    (unchanged,) = await admissions(env)
    assert unchanged.charged_usd == row.charged_usd
    # The difference is freed: a new reservation fits again.
    assert not isinstance(await competing_admission(env, account), AdmissionDenied)


async def test_deletion_during_dispatch_without_usage_stays_billing_uncertain(env: Env) -> None:
    account, run_id = await seed(env)

    async def in_flight(_: LLMRequest) -> object:
        await delete_and_withdraw(env, account, run_id)
        raise LLMError(LLMErrorKind.UNAVAILABLE, exc_type="APITimeoutError")

    fake = FakeLLM(in_flight)
    with pytest.raises(LLMError):
        await client(env, fake).complete(request(account, run_id))

    (row,) = await admissions(env)
    assert row.status == "billing_uncertain"
    assert row.charged_usd is None
    assert row.reserved_usd > 0
    assert row.error_code == "timeout"
    assert await llm_call_rows(env) == []
    # Deletion and withdrawal never classify the call as unused.
    assert await competing_admission(env, account) == AdmissionDenied.MONTHLY_BUDGET

    # Explicit reconciliation (late usage) settles the same attempt once.
    async with transaction(env.factory) as uow:
        first = await settle_call(
            uow.session,
            account_id=account,
            admission_id=row.id,
            outcome=CallOutcome.COMPLETED,
            usage=Usage(input_tokens=30, output_tokens=10),
            actual_model="gpt-6-luna",
            now=env.clock.now(),
            purpose="owner_request",
        )
    async with transaction(env.factory) as uow:
        second = await settle_call(
            uow.session,
            account_id=account,
            admission_id=row.id,
            outcome=CallOutcome.COMPLETED,
            usage=Usage(input_tokens=30, output_tokens=10),
            actual_model="gpt-6-luna",
            now=env.clock.now(),
        )
    assert (first, second) == (True, False)
    (settled,) = await admissions(env)
    assert (settled.id, settled.attempt_id, settled.status) == (row.id, row.attempt_id, "completed")
    assert len(await llm_call_rows(env)) == 1
