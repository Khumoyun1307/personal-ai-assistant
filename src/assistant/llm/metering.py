"""Metering: every provider call becomes an `llm_calls` row (NFR-10, AGENTS §8).

Done in a wrapper rather than in each caller, so a new run type cannot forget
it. Failed calls carry no usage and are not recorded here; the adapter logs
them content-free.
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

#: USD per 1M tokens (input, output). The single place prices live. Source:
#: ARCHITECTURE §3/§7.4 ($0.10 / $0.50 for gpt-6-luna). Cached input tokens are
#: charged at the full input price — a deliberate over-estimate until the
#: cached-token discount is verified, so the budget errs on the safe side.
PRICES_PER_MTOK: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-6-luna": (Decimal("0.10"), Decimal("0.50")),
}

_MILLION = Decimal(1_000_000)


def estimate_cost(model: str, usage: Usage) -> Decimal | None:
    """USD estimate, or None for a model without a known price.

    The API reports the resolved model, which may carry a snapshot suffix
    (`gpt-6-luna-<date>`), so a configured name also matches as a prefix.
    """
    prices = next(
        (
            price
            for name, price in PRICES_PER_MTOK.items()
            if model == name or model.startswith(f"{name}-")
        ),
        None,
    )
    if prices is None:
        return None
    input_price, output_price = prices
    return (usage.input_tokens * input_price + usage.output_tokens * output_price) / _MILLION


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
