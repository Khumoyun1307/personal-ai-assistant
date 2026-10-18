"""The consent choke point (ARCHITECTURE §14.3, §6 invariant 9).

Every request passes through `ConsentGatedLLMClient` before any adapter sees
it. It raises — it does not return a value to branch on — when:

- the account has no active `ai_control` consent naming this processor, or
- the request carries any `Provenance.MANAGED` item and the account has no
  active `ai_managed` consent naming this processor.

Checked on every call, not once per run, so a revocation mid-run stops the
next call and a queued job re-checks when it runs.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from assistant.accounts.service import (
    CONSENT_AI_CONTROL,
    CONSENT_AI_MANAGED,
    ConsentMissingError,
    ConsentState,
)
from assistant.llm.port import LLMClient, LLMRequest, LLMResponse, ParsedResponse


class ConsentReader(Protocol):
    """Reads the current consent of a kind. Bootstrap wires it to the database."""

    async def __call__(self, account_id: int, kind: str) -> ConsentState: ...


class ConsentGatedLLMClient:
    """Wraps an `LLMClient`; refuses any call the owner has not agreed to."""

    def __init__(self, inner: LLMClient, read_consent: ConsentReader) -> None:
        self._inner = inner
        self._read_consent = read_consent
        self.processor = inner.processor

    async def complete(self, request: LLMRequest) -> LLMResponse:
        await self._check(request)
        return await self._inner.complete(request)

    async def parse[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> ParsedResponse[T]:
        await self._check(request)
        return await self._inner.parse(request, schema)

    async def _check(self, request: LLMRequest) -> None:
        await self._require(request.account_id, CONSENT_AI_CONTROL)
        if request.contains_managed():
            await self._require(request.account_id, CONSENT_AI_MANAGED)

    async def _require(self, account_id: int, kind: str) -> None:
        state = await self._read_consent(account_id, kind)
        if not state.granted:
            raise ConsentMissingError(f"no active '{kind}' consent")
        if self.processor not in state.processors:
            # Consent names who the data goes to; a provider switch without
            # re-consent must stop here (SEC-02).
            raise ConsentMissingError(f"'{kind}' consent does not name '{self.processor}'")
