"""S03.4: the cost guards for real-model runs, exercised on FakeLLM only."""

from __future__ import annotations

from decimal import Decimal

import pytest
from evals.budget import (
    BudgetedLLMClient,
    BudgetExceededError,
    CallLedger,
    UnknownPriceError,
    estimate,
    parse_price,
    price_of,
    worst_case,
)
from evals.targets import PlannedCall
from pydantic import BaseModel

from assistant.llm.port import (
    LLMError,
    LLMErrorKind,
    LLMRequest,
    Provenance,
    TextItem,
    Usage,
)
from tests.fakes.llm import FakeLLM, text_response

LUNA = "gpt-6-luna"  # priced in PRICES_PER_MTOK (USD 0.10 in / 0.50 out per 1M tokens)


class Answer(BaseModel):
    value: str


def request(
    *, model: str = LUNA, chars: int = 100, max_output_tokens: int = 1000, purpose: str = "eval_x"
) -> LLMRequest:
    return LLMRequest(
        account_id=1,
        purpose=purpose,
        model=model,
        instructions="x" * chars,
        items=(TextItem("user", "q", Provenance.OWNER_CONTROL),),
        max_output_tokens=max_output_tokens,
        timeout_seconds=10.0,
    )


# ------------------------------------------------------------------ pricing
def test_the_product_price_table_prices_a_known_model() -> None:
    cost = price_of(LUNA, Usage(1_000_000, 1_000_000), {})
    assert cost == Decimal("0.60")  # 0.10 in + 0.50 out


def test_a_snapshot_suffix_uses_the_base_models_price() -> None:
    assert price_of(f"{LUNA}-2026-10-01", Usage(1_000_000, 0), {}) == Decimal("0.10")


def test_an_unknown_model_is_refused_not_guessed() -> None:
    with pytest.raises(UnknownPriceError) as caught:
        price_of("gpt-6-sol", Usage(10, 10), {})
    assert "--price gpt-6-sol=" in str(caught.value)


def test_an_explicit_price_covers_a_model_the_table_lacks() -> None:
    model, prices = parse_price("gpt-6-sol=0.30:1.50")
    assert model == "gpt-6-sol"
    assert price_of(model, Usage(1_000_000, 1_000_000), {model: prices}) == Decimal("1.80")


@pytest.mark.parametrize("spec", ["nomodel", "m=1", "m=a:b", "=1:2", "m=-1:2"])
def test_a_malformed_price_is_refused(spec: str) -> None:
    with pytest.raises(ValueError, match="--price expects"):
        parse_price(spec)


# --------------------------------------------------------------- the estimate
def test_the_worst_case_prices_the_full_output_budget_and_conservative_input() -> None:
    # 2000 chars -> 1000 input tokens at 2 chars/token; output at the full budget.
    expected = (Decimal(1000) * Decimal("0.10") + Decimal(3000) * Decimal("0.50")) / 1_000_000
    assert worst_case(LUNA, 2000, 3000, {}) == expected


def test_the_estimate_is_the_sum_of_the_worst_cases_and_checks_the_cap() -> None:
    plan = [PlannedCall("a", 2000, 3000), PlannedCall("b", 2000, 3000)]
    result = estimate(plan, LUNA, Decimal("1"), {})
    assert result.calls == 2
    assert result.upper_bound_usd == 2 * worst_case(LUNA, 2000, 3000, {})
    assert result.fits
    assert not estimate(plan, LUNA, Decimal("0.001"), {}).fits


# ------------------------------------------------------------ the guard
async def test_every_call_is_metered_by_purpose_model_and_cost() -> None:
    inner = FakeLLM(text_response("a", tokens=300), text_response("b", tokens=300))
    client = BudgetedLLMClient(inner, cap_usd=Decimal("1"))
    await client.complete(request(purpose="eval_one"))
    await client.complete(request(purpose="eval_two"))

    entries = client.ledger.entries
    assert [(e.purpose, e.model) for e in entries] == [("eval_one", LUNA), ("eval_two", LUNA)]
    assert all(e.usage.input_tokens == 300 for e in entries)
    assert client.ledger.spent == sum(e.cost_usd for e in entries)
    assert client.ledger.by_model()[LUNA].calls == 2


async def test_the_call_that_could_exceed_the_cap_is_never_made() -> None:
    inner = FakeLLM(text_response("a"), text_response("b"))
    one_call = worst_case(LUNA, 100, 1000, {})
    client = BudgetedLLMClient(inner, cap_usd=one_call * Decimal("1.5"))

    await client.complete(request())  # fits: spent is tiny, worst case < cap
    with pytest.raises(BudgetExceededError):
        await client.complete(request(max_output_tokens=1_000_000))

    assert len(inner.requests) == 1  # the second call never reached the provider


async def test_the_guard_counts_what_was_spent_not_only_the_estimate() -> None:
    inner = FakeLLM(text_response("a", tokens=900_000), text_response("b"))
    worst = worst_case(LUNA, 100, 1000, {})
    client = BudgetedLLMClient(inner, cap_usd=Decimal("0.12"))
    assert worst < Decimal("0.12")

    await client.complete(request())  # 900k input tokens + 300k output: $0.24
    assert client.ledger.spent > Decimal("0.12")
    with pytest.raises(BudgetExceededError):
        await client.complete(request())
    assert len(inner.requests) == 1


async def test_a_model_without_a_price_is_refused_before_any_call() -> None:
    inner = FakeLLM(text_response("a"))
    client = BudgetedLLMClient(inner, cap_usd=Decimal("1"))
    with pytest.raises(UnknownPriceError):
        await client.complete(request(model="gpt-6-sol"))
    assert inner.requests == []


async def test_invalid_structured_output_is_billed_at_its_worst_case() -> None:
    """The provider generated (and billed) output we cannot see the size of."""
    inner = FakeLLM()
    inner.parsed = [LLMError(LLMErrorKind.INVALID_OUTPUT)]
    client = BudgetedLLMClient(inner, cap_usd=Decimal("1"))
    with pytest.raises(LLMError):
        await client.parse(request(), Answer)
    (entry,) = client.ledger.entries
    assert entry.estimated
    assert entry.cost_usd == worst_case(LUNA, 100 + 1, 1000, {})


async def test_an_unavailable_provider_costs_nothing() -> None:
    inner = FakeLLM()
    inner.parsed = [LLMError(LLMErrorKind.UNAVAILABLE)]
    client = BudgetedLLMClient(inner, cap_usd=Decimal("1"))
    with pytest.raises(LLMError):
        await client.parse(request(), Answer)
    assert client.ledger.entries == []


async def test_parse_is_metered_like_complete() -> None:
    inner = FakeLLM()
    inner.parsed = [{"value": "x"}]
    client = BudgetedLLMClient(inner, cap_usd=Decimal("1"))
    response = await client.parse(request(), Answer)
    assert response.parsed == Answer(value="x")
    assert client.ledger.by_model()[LUNA].calls == 1


async def test_the_wrapper_keeps_the_inner_clients_processor() -> None:
    assert BudgetedLLMClient(FakeLLM(), cap_usd=Decimal("1")).processor == "OpenAI"


def test_cost_per_100_items_is_total_over_items_times_100() -> None:
    ledger = CallLedger()
    from evals.budget import CallEntry

    ledger.entries += [
        CallEntry("p", LUNA, Usage(10, 10), Decimal("0.02")),
        CallEntry("p", LUNA, Usage(10, 10), Decimal("0.03")),
    ]
    totals = ledger.by_model()[LUNA]
    assert totals.cost_usd == Decimal("0.05")
    assert totals.per_100_items(25) == Decimal("0.20")
    assert totals.per_100_items(0) is None
