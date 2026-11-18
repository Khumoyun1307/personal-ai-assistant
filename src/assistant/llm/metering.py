"""Metering: every provider call becomes an `llm_calls` row (NFR-10, AGENTS §8).

Production wiring meters through `llm/admitted.py` (I16), which reserves
before the call and writes the `llm_calls` row in the same transaction as the
admission's settlement. `MeteredLLMClient` is the pre-I16 record-after-success
wrapper, kept for tests and tools that do not need admission. Failed calls
carry no usage and are not recorded here.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel

from assistant.llm.port import (
    CallRecord,
    LLMClient,
    LLMRequest,
    LLMResponse,
    ParsedResponse,
    Usage,
)
from assistant.llm.pricing import PRICES, price_for

#: USD per 1M tokens (input, output), derived from the sourced table in
#: `llm/pricing.py` — still the single place prices live (evals read this).
PRICES_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    price.model: (price.input_per_mtok, price.output_per_mtok) for price in PRICES
}


def estimate_cost(model: str, usage: Usage) -> Decimal | None:
    """USD estimate, or None for a model without a known price.

    The API reports the resolved model, which may carry a dated snapshot
    suffix (`gpt-6-luna-<date>`); `pricing.price_for` matches only that.
    """
    price = price_for(model)
    if price is None:
        return None
    return price.cost(usage.input_tokens, usage.output_tokens)


class CallRecorder(Protocol):
    """Persists a `CallRecord`. Bootstrap wires it to the database."""

    async def __call__(self, record: CallRecord) -> None: ...


class MeteredLLMClient:
    """Wraps an `LLMClient`; records usage of every successful call."""

    def __init__(self, inner: LLMClient, record: CallRecorder) -> None:
        self._inner = inner
        self._record = record
        self.processor = inner.processor

    async def complete(self, request: LLMRequest) -> LLMResponse:
        response = await self._inner.complete(request)
        await self._save(request, response.model, response.usage, response.request_id)
        return response

    async def parse[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> ParsedResponse[T]:
        response = await self._inner.parse(request, schema)
        await self._save(request, response.model, response.usage, response.request_id)
        return response

    async def _save(
        self, request: LLMRequest, model: str, usage: Usage, request_id: str | None
    ) -> None:
        await self._record(
            CallRecord(
                account_id=request.account_id,
                run_id=request.run_id,
                purpose=request.purpose,
                model=model,
                usage=usage,
                request_id=request_id,
            )
        )
