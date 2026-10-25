"""Cost guards for real-model evaluation runs (AGENTS §8, §9; PLAN §1.4).

S03.4 real-model runs are capped at USD 1 in total (owner approval,
2026-10-21); the cap enforced here is per run (default USD 1). The guards, in order:

1. **Refuse without `--live`** (`evals/__main__.py`): a model run needs an
   explicit flag, and the owner's go-ahead for that specific run.
2. **Pre-run estimate** (`estimate`): every call a target plans, priced at its
   *worst case* (input at a conservative characters-per-token, output at
   `max_output_tokens`). A run whose worst case exceeds the cap does not start.
3. **Hard stop in flight** (`BudgetedLLMClient`): before each call, spent plus
   that call's worst case must fit under the cap, else `BudgetExceededError`
   is raised and the call is never made. A call that fails after possibly
   generating (invalid structured output, a timeout or connection failure) is
   billed at its worst case, because its real usage is unknown. The SDK client
   is built with `max_retries=0` (`evals/live.py`), so there are no invisible
   retries: every attempt is one call here.
4. **Metering**: every call becomes a `CallEntry` (purpose, model, tokens,
   cost); the report totals them per model and per 100 items.

Prices come from `assistant.llm.metering.PRICES_PER_MTOK`, the one place the
product keeps them, plus explicit `--price MODEL=IN:OUT` overrides for models
the table lacks (exact id, positive prices; never for a model the table already
prices). Only a dated snapshot (`name-YYYY-MM-DD`) shares a base model's price.
A model with no price is refused: without one the cap cannot be enforced. No
model id is hard-coded here; models come from configuration.

No provider file, batch or feedback API is used: calls go through the
`LLMClient` port (Responses API, `store=False`).
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from pydantic import BaseModel

from assistant.llm.metering import PRICES_PER_MTOK
from assistant.llm.port import (
    InputItem,
    LLMClient,
    LLMError,
    LLMErrorKind,
    LLMRequest,
    LLMResponse,
    ParsedResponse,
    TextItem,
    ToolResultItem,
    Usage,
)
from evals.errors import EvalsError
from evals.targets import PlannedCall

#: Default cap for one run (the S03.4 total of USD 1 is tracked via the reports).
DEFAULT_BUDGET_USD = Decimal("1")
#: Worst case for input: one token per character (Cyrillic and Uzbek text
#: tokenizes at about 2-3 characters per token, so this over-estimates), plus a
#: fixed per-call overhead for the structured-output schema and request framing.
CHARS_PER_TOKEN = 1
CALL_OVERHEAD_TOKENS = 500
#: A dated snapshot of a model (`name-YYYY-MM-DD`), the only suffix that shares
#: the base model's price. `name-pro` or `name-preview` are different models.
_SNAPSHOT = re.compile(r"-\d{4}-\d{2}-\d{2}$")

_MILLION = Decimal(1_000_000)
Prices = Mapping[str, tuple[Decimal, Decimal]]


class BudgetError(EvalsError):
    """A cost guard stopped the run."""


class BudgetExceededError(BudgetError):
    """The next call could push spending over the cap."""

    def __init__(self, spent: Decimal, cap: Decimal) -> None:
        super().__init__(f"the next call could exceed the cap (spent ${spent:.4f} of ${cap})")
        self.spent = spent
        self.cap = cap


class UnknownPriceError(BudgetError):
    def __init__(self, model: str) -> None:
        super().__init__(
            f"no price known for model '{model}': add it to PRICES_PER_MTOK or pass "
            f"--price {model}=<input USD per 1M tokens>:<output USD per 1M tokens>"
        )
        self.model = model


def parse_price(spec: str) -> tuple[str, tuple[Decimal, Decimal]]:
    """`MODEL=IN:OUT` (USD per 1M tokens) from the command line."""
    try:
        model, rest = spec.split("=", 1)
        input_price, output_price = rest.split(":", 1)
        prices = (Decimal(input_price), Decimal(output_price))
    except ValueError, ArithmeticError:
        raise ValueError(f"--price expects MODEL=IN:OUT, got '{spec}'") from None
    if not model or min(prices) <= 0:
        # A zero price would make a model free and the cap meaningless.
        raise ValueError(f"--price expects MODEL=IN:OUT with positive prices, got '{spec}'")
    return model, prices


def _table_price(model: str) -> tuple[Decimal, Decimal] | None:
    """The product table's price for `model` or its dated snapshot, never a
    longer name that merely starts with it."""
    return PRICES_PER_MTOK.get(_SNAPSHOT.sub("", model))


def check_overrides(overrides: Prices) -> None:
    """Refuse an override for a model the product table already prices: it
    could only lower the price and so disable the cap."""
    for model in overrides:
        if _table_price(model) is not None:
            raise ValueError(f"--price: '{model}' is already priced in PRICES_PER_MTOK")


def _lookup(model: str, overrides: Prices) -> tuple[Decimal, Decimal] | None:
    # The table first, so an override can never reprice a model it covers;
    # overrides match the exact model id only (never a prefix).
    return _table_price(model) or overrides.get(model)


def price_of(model: str, usage: Usage, overrides: Prices) -> Decimal:
    """USD for `usage` on `model`; raises `UnknownPriceError` without a price."""
    prices = _lookup(model, overrides)
    if prices is None:
        raise UnknownPriceError(model)
    return (usage.input_tokens * prices[0] + usage.output_tokens * prices[1]) / _MILLION


def worst_case(model: str, input_chars: int, max_output_tokens: int, overrides: Prices) -> Decimal:
    """The most one call can cost: conservative input, full output budget."""
    usage = Usage(
        input_tokens=-(-input_chars // CHARS_PER_TOKEN) + CALL_OVERHEAD_TOKENS,
        output_tokens=max_output_tokens,
    )
    return price_of(model, usage, overrides)


# ------------------------------------------------------------------ estimate
@dataclass(frozen=True, slots=True)
class CostEstimate:
    calls: int
    #: Every call at its worst case; the run cannot cost more than this.
    upper_bound_usd: Decimal
    cap_usd: Decimal

    @property
    def fits(self) -> bool:
        return self.upper_bound_usd <= self.cap_usd


def estimate(
    plan: Sequence[PlannedCall], model: str, cap_usd: Decimal, overrides: Prices
) -> CostEstimate:
    total = sum(
        (worst_case(model, call.input_chars, call.max_output_tokens, overrides) for call in plan),
        Decimal(0),
    )
    return CostEstimate(len(plan), total, cap_usd)


# -------------------------------------------------------------------- ledger
@dataclass(frozen=True, slots=True)
class CallEntry:
    purpose: str
    model: str
    usage: Usage
    cost_usd: Decimal
    #: Billed at the worst case because the real usage is unknown.
    estimated: bool = False


@dataclass
class CallLedger:
    """Every call of one run: ids and numbers, never content."""

    entries: list[CallEntry] = field(default_factory=list)

    @property
    def spent(self) -> Decimal:
        return sum((entry.cost_usd for entry in self.entries), Decimal(0))

    def by_model(self) -> dict[str, ModelTotals]:
        grouped: dict[str, list[CallEntry]] = defaultdict(list)
        for entry in self.entries:
            grouped[entry.model].append(entry)
        return {
            model: ModelTotals(
                calls=len(entries),
                input_tokens=sum(e.usage.input_tokens for e in entries),
                output_tokens=sum(e.usage.output_tokens for e in entries),
                cost_usd=sum((e.cost_usd for e in entries), Decimal(0)),
            )
            for model, entries in sorted(grouped.items())
        }


@dataclass(frozen=True, slots=True)
class ModelTotals:
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal

    def per_100_items(self, items: int) -> Decimal | None:
        return None if items == 0 else self.cost_usd / items * 100


def _request_chars(request: LLMRequest) -> int:
    def item_chars(item: InputItem) -> int:
        match item:
            case TextItem(text=text):
                return len(text)
            case ToolResultItem(output=output):
                return len(output)
            case _:
                return len(json.dumps(list(item.payload), default=str))

    tool_chars = sum(
        len(tool.name) + len(tool.description) + len(json.dumps(tool.parameters, default=str))
        for tool in request.tools
    )
    return len(request.instructions) + sum(map(item_chars, request.items)) + tool_chars


_BILLED_FAILURES = frozenset({LLMErrorKind.INVALID_OUTPUT, LLMErrorKind.UNAVAILABLE})


class BudgetedLLMClient:
    """An `LLMClient` that meters every call and refuses to overspend.

    Wraps the adapter (or `FakeLLM` in tests). It sits where the metering
    wrapper sits in production, but writes to an in-memory ledger: evaluation
    runs use synthetic data and no account, so there is no `llm_calls` row and
    no consent to check (the consent wrapper guards the owner's data; this is
    not that).
    """

    def __init__(
        self,
        inner: LLMClient,
        *,
        cap_usd: Decimal,
        overrides: Prices | None = None,
        ledger: CallLedger | None = None,
    ) -> None:
        self._inner = inner
        self._cap = cap_usd
        self._overrides: Prices = overrides or {}
        check_overrides(self._overrides)
        self.ledger = ledger if ledger is not None else CallLedger()
        self.processor = inner.processor

    def _guard(self, request: LLMRequest) -> Decimal:
        worst = worst_case(
            request.model, _request_chars(request), request.max_output_tokens, self._overrides
        )
        if self.ledger.spent + worst > self._cap:
            raise BudgetExceededError(self.ledger.spent, self._cap)
        return worst

    def _record(self, request: LLMRequest, model: str, usage: Usage) -> None:
        # The resolved model may carry a snapshot suffix; price what ran, and
        # fall back to the requested model's price (the guard checked it exists).
        try:
            cost = price_of(model, usage, self._overrides)
        except UnknownPriceError:
            cost = price_of(request.model, usage, self._overrides)
        self.ledger.entries.append(CallEntry(request.purpose, model, usage, cost))

    async def complete(self, request: LLMRequest) -> LLMResponse:
        worst = self._guard(request)
        try:
            response = await self._inner.complete(request)
        except LLMError as error:
            self._bill_unknown(request, error, worst)
            raise
        self._record(request, response.model, response.usage)
        return response

    async def parse[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> ParsedResponse[T]:
        worst = self._guard(request)
        try:
            response = await self._inner.parse(request, schema)
        except LLMError as error:
            self._bill_unknown(request, error, worst)
            raise
        self._record(request, response.model, response.usage)
        return response

    def _bill_unknown(self, request: LLMRequest, error: LLMError, worst: Decimal) -> None:
        # Output may have been generated and billed, with no usage reported: an
        # unusable structured output, or a timeout/connection failure (the
        # provider can finish and bill a request we stopped waiting for). Assume
        # the worst. Requests the provider rejected (429, auth, bad request)
        # generated nothing and cost nothing.
        if error.kind in _BILLED_FAILURES:
            self.ledger.entries.append(
                CallEntry(request.purpose, request.model, Usage(), worst, estimated=True)
            )
