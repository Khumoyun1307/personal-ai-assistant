"""I16 priced call admission against a real PostgreSQL (C7).

Reserve before the call, settle after: unpriced models fail closed, the last
allowance can be reserved once, nested and concurrent work cannot spend the
same remaining USD, uncertain billing stays held and visible, and settled
accounting follows its retention while unresolved rows never expire.

FakeLLM and FakeClock only; no provider and no sleeps.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy.exc
from pydantic import BaseModel
from sqlalchemy import select, text

from assistant.accounts.models import Account, Consent
from assistant.accounts.service import ConsentMissingError
from assistant.agent.models import AgentRun
from assistant.agent.persistence import DbConsentReader, LlmCallRepository
from assistant.db.uow import transaction
from assistant.llm.admission import (
    AdmissionDenied,
    CallAllowance,
    admit_call,
    billing_uncertain_count,
    mark_dispatched,
    month_of,
    month_spend,
    review_bound_overrun,
)
from assistant.llm.admitted import AdmittedLLMClient, CallNotAdmittedError
from assistant.llm.consent import ConsentChecker, ConsentGatedLLMClient
from assistant.llm.models import LlmAdmission
from assistant.llm.port import (
    LLMError,
    LLMErrorKind,
    LLMRequest,
    LLMResponse,
    Provenance,
    StopReason,
    TextItem,
    Usage,
)
from assistant.privacy.accounting import (
    export_admissions,
    reconcile_stale_admissions,
    sweep_admissions,
)
from tests.fakes.llm import FakeLLM, text_response
from tests.integration.business_env import Env, environment

pytestmark = pytest.mark.integration

#: One `request()` reserves about USD 0.000525; this fits one, never two.
LIMIT = Decimal("0.0008")


@pytest.fixture
async def env(migrated_url: str) -> AsyncIterator[Env]:
    async with environment(migrated_url) as built:
        yield built


async def make_account(env: Env, telegram_user_id: int = 7100000001) -> int:
    async with transaction(env.factory) as uow:
        account = Account(telegram_user_id=telegram_user_id, status="active")
        uow.session.add(account)
        await uow.session.flush()
        uow.session.add(
            Consent(
                account_id=account.id,
                kind="ai_control",
                policy_version="v-test",
                processors=["OpenAI"],
            )
        )
        return account.id


def request(account: int, *, model: str = "gpt-6-luna", run_id: int | None = None) -> LLMRequest:
    return LLMRequest(
        account_id=account,
        purpose="owner_request",
        model=model,
        instructions="synthetic instructions",
        items=(TextItem("user", "synthetic question", Provenance.OWNER_CONTROL),),
        max_output_tokens=1000,
        timeout_seconds=10,
        run_id=run_id,
    )


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def client(
    env: Env,
    fake: FakeLLM,
    *,
    limit: Decimal | None = LIMIT,
    sleep: Sleeps | None = None,
    before_dispatch: Any = None,
) -> ConsentGatedLLMClient:
    check = before_dispatch or ConsentChecker(DbConsentReader(env.factory), processor="OpenAI")
    admitted = AdmittedLLMClient(
        fake,
        env.factory,
        env.clock,
        before_dispatch=check,
        monthly_budget_usd=limit,
        sleep=sleep or Sleeps(),
    )
    return ConsentGatedLLMClient(admitted, DbConsentReader(env.factory))


async def rows(env: Env) -> list[LlmAdmission]:
    async with env.factory() as session:
        return list(await session.scalars(select(LlmAdmission).order_by(LlmAdmission.id)))


async def call_count(env: Env) -> int:
    async with env.factory() as session:
        return int((await session.execute(text("SELECT count(*) FROM app.llm_calls"))).scalar_one())


async def admit(env: Env, account: int, **overrides: Any) -> CallAllowance | AdmissionDenied:
    values: dict[str, Any] = {
        "account_id": account,
        "operation_id": uuid4(),
        "run_id": None,
        "attempt_id": uuid4(),
        "model": "gpt-6-luna",
        "input_bound": 100,
        "output_bound": 1000,
        "now": env.clock.now(),
        "monthly_budget_usd": LIMIT,
    }
    values.update(overrides)
    async with transaction(env.factory) as uow:
        return await admit_call(uow.session, **values)


# ---------------------------------------------------------------- pricing
async def test_an_unpriced_model_fails_admission_and_nothing_is_sent(env: Env) -> None:
    account = await make_account(env)
    fake = FakeLLM(text_response("never"))

    with pytest.raises(CallNotAdmittedError) as raised:
        await client(env, fake).complete(request(account, model="gpt-6.1-sol"))

    assert raised.value.kind is LLMErrorKind.NOT_ADMITTED
    assert raised.value.reason is AdmissionDenied.UNPRICED
    assert fake.requests == []
    assert await rows(env) == []
    assert await call_count(env) == 0


async def test_unpriced_is_refused_even_without_a_monthly_budget(env: Env) -> None:
    account = await make_account(env)
    fake = FakeLLM(text_response("never"))

    with pytest.raises(CallNotAdmittedError):
        await client(env, fake, limit=None).complete(request(account, model="gpt-6-astra"))

    assert fake.requests == []


# ------------------------------------------------------------ concurrency
async def test_two_concurrent_admissions_for_the_last_allowance_admit_exactly_one(
    env: Env,
) -> None:
    account = await make_account(env)

    results = await asyncio.gather(admit(env, account), admit(env, account))

    granted = [r for r in results if isinstance(r, CallAllowance)]
    assert len(granted) == 1
    assert [r for r in results if not isinstance(r, CallAllowance)] == [
        AdmissionDenied.MONTHLY_BUDGET
    ]
    (row,) = await rows(env)
    assert row.status == "reserved"


async def test_a_nested_call_cannot_spend_the_parents_held_allowance(env: Env) -> None:
    account = await make_account(env)
    nested: dict[str, Any] = {}

    async def parent_in_flight(_: LLMRequest) -> LLMResponse:
        try:
            await llm.complete(request(account))
        except CallNotAdmittedError as error:
            nested["denied"] = error.reason
        return text_response("parent answer", tokens=30)

    fake = FakeLLM(parent_in_flight)
    llm = client(env, fake)

    response = await llm.complete(request(account))

    assert response.text == "parent answer"
    assert nested["denied"] is AdmissionDenied.MONTHLY_BUDGET
    assert len(fake.requests) == 1
    (row,) = await rows(env)
    assert row.status == "completed"
    # Settlement freed the unused reserve: the next call is admitted.
    fake.queue(text_response("next", tokens=30))
    assert (await llm.complete(request(account))).text == "next"


async def test_spend_and_status_count_held_reservations(env: Env) -> None:
    account = await make_account(env)
    allowance = await admit(env, account, monthly_budget_usd=None)
    assert isinstance(allowance, CallAllowance)

    async with env.factory() as session:
        spent = await LlmCallRepository(session, account).spend_since(month_of(env.clock.now()))
        uncertain = await billing_uncertain_count(session, account_id=account)
    assert spent == allowance.reserved_usd
    assert uncertain == 0  # reserved: provably not sent yet

    async with transaction(env.factory) as uow:
        await mark_dispatched(
            uow.session,
            account_id=account,
            admission_id=allowance.admission_id,
            now=env.clock.now(),
        )
    async with env.factory() as session:
        assert await billing_uncertain_count(session, account_id=account) == 1


# --------------------------------------------------------------- outcomes
async def test_a_definite_rate_limit_is_released_and_retried_as_a_new_admission(
    env: Env,
) -> None:
    account = await make_account(env)
    sleeps = Sleeps()
    fake = FakeLLM(
        LLMError(LLMErrorKind.RATE_LIMITED, exc_type="RateLimitError"),
        text_response("second attempt", tokens=30),
    )

    response = await client(env, fake, sleep=sleeps).complete(request(account))

    assert response.text == "second attempt"
    assert sleeps.delays == [0.5]
    first, second = await rows(env)
    assert (first.status, first.charged_usd, first.error_code) == (
        "released",
        Decimal(0),
        "rate_limited",
    )
    assert second.status == "completed"
    assert first.operation_id == second.operation_id
    assert first.attempt_id != second.attempt_id
    assert await call_count(env) == 1


async def test_a_timeout_is_held_as_billing_uncertain_and_never_replayed(env: Env) -> None:
    account = await make_account(env)
    fake = FakeLLM(LLMError(LLMErrorKind.UNAVAILABLE, exc_type="APITimeoutError"))

    with pytest.raises(LLMError):
        await client(env, fake, limit=None).complete(request(account))

    (row,) = await rows(env)
    assert (row.status, row.error_code, row.charged_usd) == ("billing_uncertain", "timeout", None)
    assert len(fake.requests) == 1
    async with env.factory() as session:
        assert await month_spend(session, account_id=account, since=month_of(env.clock.now())) == (
            row.reserved_usd
        )
        assert await billing_uncertain_count(session, account_id=account) == 1


async def test_unreadable_structured_output_is_billing_uncertain(env: Env) -> None:
    class Out(BaseModel):
        value: int

    account = await make_account(env)
    fake = FakeLLM()
    fake.parsed.append(LLMError(LLMErrorKind.INVALID_OUTPUT))

    with pytest.raises(LLMError):
        await client(env, fake, limit=None).parse(request(account), Out)

    (row,) = await rows(env)
    assert (row.status, row.error_code) == ("billing_uncertain", "invalid_output")


async def test_a_cancelled_call_is_billing_uncertain(env: Env) -> None:
    account = await make_account(env)

    async def cancelled(_: LLMRequest) -> LLMResponse:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await client(env, FakeLLM(cancelled), limit=None).complete(request(account))

    (row,) = await rows(env)
    assert (row.status, row.error_code) == ("billing_uncertain", "interrupted")


async def test_consent_withdrawn_after_reservation_releases_it_unsent(env: Env) -> None:
    account = await make_account(env)
    real = ConsentChecker(DbConsentReader(env.factory), processor="OpenAI")

    async def withdraw_then_check(req: LLMRequest) -> None:
        async with transaction(env.factory) as uow:
            await uow.session.execute(
                text("UPDATE app.consents SET revoked_at = now() WHERE account_id = :a"),
                {"a": account},
            )
        await real(req)

    fake = FakeLLM(text_response("never"))
    with pytest.raises(ConsentMissingError):
        await client(env, fake, before_dispatch=withdraw_then_check).complete(request(account))

    assert fake.requests == []
    (row,) = await rows(env)
    assert (row.status, row.charged_usd, row.error_code) == (
        "released",
        Decimal(0),
        "not_dispatched",
    )


async def test_usage_beyond_the_bound_is_charged_truly_and_blocks_admission(env: Env) -> None:
    account = await make_account(env)
    huge = LLMResponse(StopReason.COMPLETED, "gpt-6-luna", Usage(50_000, 50_000), text="x")
    fake = FakeLLM(huge)
    llm = client(env, fake, limit=None)

    await llm.complete(request(account))

    (row,) = await rows(env)
    assert row.status == "completed"
    assert row.charged_usd == Decimal("0.030000000")
    assert row.charged_usd > row.reserved_usd
    assert row.error_code == "bound_exceeded"
    with pytest.raises(CallNotAdmittedError) as raised:
        await llm.complete(request(account))
    assert raised.value.reason is AdmissionDenied.INVALID_BOUND

    async with transaction(env.factory) as uow:
        assert await review_bound_overrun(uow.session, account_id=account, admission_id=row.id)
    fake.queue(text_response("resumed"))
    assert (await llm.complete(request(account))).text == "resumed"


async def test_another_accounts_run_is_never_linked(env: Env) -> None:
    mine = await make_account(env)
    other = await make_account(env, telegram_user_id=7100000002)
    async with transaction(env.factory) as uow:
        run = AgentRun(account_id=other, run_type="owner_request", model="gpt-6-luna")
        uow.session.add(run)
        await uow.session.flush()
        foreign_run = run.id

    allowance = await admit(env, mine, run_id=foreign_run)

    assert isinstance(allowance, CallAllowance)
    (row,) = await rows(env)
    assert row.run_id is None


# ------------------------------------------------------------ month/clock
async def test_an_uncertain_reserve_stays_in_its_month(env: Env) -> None:
    account = await make_account(env)
    october = month_of(env.clock.now())
    fake = FakeLLM(LLMError(LLMErrorKind.UNAVAILABLE, exc_type="APIConnectionError"))
    with pytest.raises(LLMError):
        await client(env, fake).complete(request(account))
    (held,) = await rows(env)

    env.clock.advance(days=31)
    november = month_of(env.clock.now())
    fake.queue(text_response("november", tokens=30))
    assert (await client(env, fake).complete(request(account))).text == "november"

    async with env.factory() as session:
        october_spend = await month_spend(session, account_id=account, since=october)
        november_only = await month_spend(session, account_id=account, since=november)
    assert held.status == "billing_uncertain"
    assert october_spend - november_only == held.reserved_usd


# ------------------------------------------------------ reconcile / retain
async def test_stale_reserved_rows_release_and_stale_dispatches_become_uncertain(
    env: Env,
) -> None:
    account = await make_account(env)
    reserved = await admit(env, account, monthly_budget_usd=None)
    dispatched = await admit(env, account, monthly_budget_usd=None)
    assert isinstance(reserved, CallAllowance)
    assert isinstance(dispatched, CallAllowance)
    async with transaction(env.factory) as uow:
        await mark_dispatched(
            uow.session,
            account_id=account,
            admission_id=dispatched.admission_id,
            now=env.clock.now(),
        )

    async with transaction(env.factory) as uow:
        early = await reconcile_stale_admissions(uow.session, now=env.clock.now())
    async with transaction(env.factory) as uow:
        late = await reconcile_stale_admissions(
            uow.session, now=env.clock.now() + timedelta(hours=2)
        )

    assert (early.released, early.uncertain) == (0, 0)
    assert (late.released, late.uncertain) == (1, 1)
    first, second = await rows(env)
    assert (first.status, first.error_code) == ("released", "not_dispatched")
    assert (second.status, second.error_code) == ("billing_uncertain", "stale_dispatch")


async def test_sweep_deletes_settled_accounting_after_a_year_and_keeps_unresolved(
    env: Env,
) -> None:
    account = await make_account(env)
    fake = FakeLLM(
        text_response("settled", tokens=30),
        LLMError(LLMErrorKind.UNAVAILABLE, exc_type="APITimeoutError"),
    )
    llm = client(env, fake, limit=None)
    await llm.complete(request(account))
    with pytest.raises(LLMError):
        await llm.complete(request(account))

    async with transaction(env.factory) as uow:
        kept = await sweep_admissions(uow.session, cutoff=env.clock.now())
    async with transaction(env.factory) as uow:
        swept = await sweep_admissions(uow.session, cutoff=env.clock.now() + timedelta(days=366))

    assert (kept, swept) == (0, 1)
    (left,) = await rows(env)
    assert left.status == "billing_uncertain"


async def test_export_is_content_free_accounting(env: Env) -> None:
    account = await make_account(env)
    await client(env, FakeLLM(text_response("x", tokens=30)), limit=None).complete(request(account))

    async with env.factory() as session:
        (exported,) = await export_admissions(session, account_id=account)

    assert exported["status"] == "completed"
    assert exported["model"] == "gpt-6-luna"
    assert exported["price_version"] == "gpt-6-luna@2026-11-09"
    assert "synthetic" not in repr(exported)


async def test_physical_account_removal_is_refused_while_admissions_remain(env: Env) -> None:
    account = await make_account(env)
    await client(env, FakeLLM(text_response("x", tokens=30)), limit=None).complete(request(account))

    with pytest.raises(sqlalchemy.exc.IntegrityError):
        async with transaction(env.factory) as uow:
            await uow.session.execute(
                text("DELETE FROM app.accounts WHERE id = :id"), {"id": account}
            )
    assert len(await rows(env)) == 1
