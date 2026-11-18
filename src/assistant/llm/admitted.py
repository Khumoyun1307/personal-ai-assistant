"""The admission wrapper: every provider attempt is priced and reserved (I16).

Order per call (C7): the outer consent gate → priced reservation (committed)
→ consent re-check at the call boundary → `dispatched` (committed) → the
provider attempt → settlement. Nothing here holds a database lock or an open
transaction across the provider call.

Attempts are explicit. The SDK's own retries are off in production
(`bootstrap.OPENAI_MAX_RETRIES = 0`) because an invisible retry could bill
without a record; instead this wrapper retries a *definite*, transient
failure (429 or a provider 5xx — answered, so not billed) as a new, separately
admitted attempt. An attempt whose outcome is unknown (timeout, dropped
connection, cancellation, output we could not read) is never replayed: its
reserve stays held as `billing_uncertain` until a late settlement or an
explicit reconciliation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.core.clock import Clock
from assistant.db.uow import transaction
from assistant.llm.admission import (
    AdmissionDenied,
    CallAllowance,
    CallOutcome,
    admit_call,
    input_bound,
    mark_dispatched,
    settle_call,
)
from assistant.llm.port import (
    LLMClient,
    LLMError,
    LLMErrorKind,
    LLMRequest,
    LLMResponse,
    ParsedResponse,
    Usage,
)
from assistant.observability.logging import get_logger

logger = get_logger(__name__)

#: A check run immediately before dispatch (the consent re-check).
DispatchCheck = Callable[[LLMRequest], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]

#: SDK classes that mean the provider answered with an error status (not billed).
_STATUS_ERRORS = frozenset({"InternalServerError", "APIStatusError"})
#: Delays before the second and third attempt of a transient definite failure.
RETRY_DELAYS_SECONDS = (0.5, 2.0)


class CallNotAdmittedError(LLMError):
    """Admission refused the attempt; nothing was sent to the provider."""

    def __init__(self, reason: AdmissionDenied) -> None:
        super().__init__(LLMErrorKind.NOT_ADMITTED, code=reason.value)
        self.reason = reason


class _Billed(Protocol):
    @property
    def model(self) -> str: ...
    @property
    def usage(self) -> Usage: ...
    @property
    def request_id(self) -> str | None: ...


def classify_failure(error: LLMError) -> tuple[CallOutcome, str]:
    """Billing outcome of a failed attempt, with its fixed error code."""
    match error.kind:
        case LLMErrorKind.RATE_LIMITED:
            return CallOutcome.DEFINITE_FAILURE, "rate_limited"
        case LLMErrorKind.AUTH:
            return CallOutcome.DEFINITE_FAILURE, "auth"
        case LLMErrorKind.BAD_REQUEST:
            return CallOutcome.DEFINITE_FAILURE, "bad_request"
        case LLMErrorKind.UNAVAILABLE if error.exc_type in _STATUS_ERRORS:
            return CallOutcome.DEFINITE_FAILURE, "provider_error"
        case LLMErrorKind.UNAVAILABLE if error.exc_type == "APITimeoutError":
            return CallOutcome.BILLING_UNCERTAIN, "timeout"
        case LLMErrorKind.UNAVAILABLE:
            return CallOutcome.BILLING_UNCERTAIN, "connection"
        case LLMErrorKind.INVALID_OUTPUT:
            # The provider answered and billed, but the usage was lost with the output.
            return CallOutcome.BILLING_UNCERTAIN, "invalid_output"
        case _:
            return CallOutcome.BILLING_UNCERTAIN, "interrupted"


def _retryable(error: LLMError) -> bool:
    return error.kind is LLMErrorKind.RATE_LIMITED or (
        error.kind is LLMErrorKind.UNAVAILABLE and error.exc_type in _STATUS_ERRORS
    )


class AdmittedLLMClient:
    """Wraps the provider adapter; reserves, dispatches and settles each attempt."""

    def __init__(
        self,
        inner: LLMClient,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Clock,
        *,
        before_dispatch: DispatchCheck | None = None,
        monthly_budget_usd: Decimal | None = None,
        new_id: Callable[[], UUID] = uuid4,
        sleep: Sleep = asyncio.sleep,
        retry_delays: tuple[float, ...] = RETRY_DELAYS_SECONDS,
    ) -> None:
        self._inner = inner
        self._factory = session_factory
        self._clock = clock
        self._before_dispatch = before_dispatch
        self._budget = monthly_budget_usd
        self._new_id = new_id
        self._sleep = sleep
        self._delays = retry_delays
        self.processor = inner.processor

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return await self._run(request, None, lambda: self._inner.complete(request))

    async def parse[T: BaseModel](self, request: LLMRequest, schema: type[T]) -> ParsedResponse[T]:
        return await self._run(request, schema, lambda: self._inner.parse(request, schema))

    async def _run[R: _Billed](
        self,
        request: LLMRequest,
        schema: type[BaseModel] | None,
        call: Callable[[], Awaitable[R]],
    ) -> R:
        operation_id = request.operation_id or self._new_id()
        bound = input_bound(request, schema)
        retries = 0
        while True:
            try:
                return await self._attempt(request, operation_id, bound, call)
            except CallNotAdmittedError:
                raise
            except LLMError as error:
                if not _retryable(error) or retries >= len(self._delays):
                    raise
                await self._sleep(self._delays[retries])
                retries += 1

    async def _attempt[R: _Billed](
        self,
        request: LLMRequest,
        operation_id: UUID,
        bound: int,
        call: Callable[[], Awaitable[R]],
    ) -> R:
        async with transaction(self._factory) as uow:
            allowance = await admit_call(
                uow.session,
                account_id=request.account_id,
                operation_id=operation_id,
                run_id=request.run_id,
                attempt_id=self._new_id(),
                model=request.model,
                input_bound=bound,
                output_bound=request.max_output_tokens,
                now=self._clock.now(),
                monthly_budget_usd=self._budget,
            )
        if isinstance(allowance, AdmissionDenied):
            raise CallNotAdmittedError(allowance)
        try:
            if self._before_dispatch is not None:
                await self._before_dispatch(request)
            async with transaction(self._factory) as uow:
                dispatched = await mark_dispatched(
                    uow.session,
                    account_id=request.account_id,
                    admission_id=allowance.admission_id,
                    now=self._clock.now(),
                )
        except BaseException:
            # `reserved` is the proof no attempt started: release it.
            await self._settle(request, allowance, CallOutcome.NOT_DISPATCHED)
            raise
        if not dispatched:
            await self._settle(request, allowance, CallOutcome.NOT_DISPATCHED)
            raise CallNotAdmittedError(AdmissionDenied.INVALID_BOUND)
        try:
            response = await call()
        except LLMError as error:
            outcome, code = classify_failure(error)
            await self._settle(request, allowance, outcome, error_code=code)
            raise
        except BaseException:
            await self._settle(
                request, allowance, CallOutcome.BILLING_UNCERTAIN, error_code="interrupted"
            )
            raise
        await self._settle(
            request,
            allowance,
            CallOutcome.COMPLETED,
            usage=response.usage,
            actual_model=response.model,
            request_id=response.request_id,
        )
        return response

    async def _settle(
        self,
        request: LLMRequest,
        allowance: CallAllowance,
        outcome: CallOutcome,
        *,
        usage: Usage | None = None,
        actual_model: str | None = None,
        request_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """Best effort: a settlement that cannot be written leaves the row
        `reserved`/`dispatched` — still held — for the hourly reconciler to
        release (never dispatched) or mark `billing_uncertain`."""
        try:
            async with transaction(self._factory) as uow:
                await settle_call(
                    uow.session,
                    account_id=request.account_id,
                    admission_id=allowance.admission_id,
                    outcome=outcome,
                    usage=usage,
                    actual_model=actual_model,
                    now=self._clock.now(),
                    purpose=request.purpose,
                    request_id=request_id,
                    error_code=error_code,
                )
        except Exception as error:
            logger.error(
                "llm_settlement_failed",
                admission_id=allowance.admission_id,
                outcome=outcome.value,
                exc_type=type(error).__name__,
            )
