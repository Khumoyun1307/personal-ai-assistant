"""Priced call admission: reserve before the call, settle after (I16, C7).

The interface I15's shared `OperationBudget` and the `AdmittedLLMClient`
wrapper both call. Every function works in the **caller's** transaction and
must be committed before any provider I/O; none holds a lock across a call.

Lock order (C7 "account → admission"): the account row first, `FOR NO KEY
UPDATE` — the same lock content deletion takes (`privacy/deletion.py`), and
compatible with ingestion's foreign-key share — then the admission row.
That serializes concurrent reservations (two jobs cannot both spend the same
remaining USD) and serializes settlement with deletion.

Lifecycle::

    reserved ──mark_dispatched──▶ dispatched ──settle(completed)──▶ completed
       │                              │  └─settle(definite_failure)─▶ released
       └─settle(not_dispatched)─▶ released └─settle(billing_uncertain)─▶ billing_uncertain
    billing_uncertain ──settle(completed)──▶ completed   (late response / reconciliation)

`reserved`, `dispatched` and `billing_uncertain` hold their full reserve in
the admission month; `completed` counts `charged_usd`; `released` counts
nothing. Settlement happens at most once per attempt: a repeated or late
duplicate settlement returns False and changes nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import case, exists, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.llm.models import HELD_STATUSES, LlmAdmission
from assistant.llm.port import LLMRequest, ProviderItems, TextItem, ToolResultItem, Usage
from assistant.llm.pricing import price_for
from assistant.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CallAllowance:
    """A committed reservation for one provider attempt (no content)."""

    admission_id: int
    reserved_usd: Decimal
    input_bound: int
    output_bound: int


class AdmissionDenied(StrEnum):
    """Why an attempt was not admitted. Consent and quarantine are refused
    earlier, by the consent gate (`ConsentMissingError`), and never reach a
    reservation."""

    NO_CONSENT = "no_consent"
    QUARANTINED = "quarantined"
    MONTHLY_BUDGET = "monthly_budget"
    UNPRICED = "unpriced"
    INVALID_BOUND = "invalid_bound"


class CallOutcome(StrEnum):
    NOT_DISPATCHED = "not_dispatched"
    """Provably never sent: the reservation is released."""

    COMPLETED = "completed"
    """The provider answered with usage: charge it exactly once."""

    DEFINITE_FAILURE = "definite_failure"
    """The provider answered with a refusal status (4xx/5xx): not billed."""

    BILLING_UNCERTAIN = "billing_uncertain"
    """Sent, outcome unknown (timeout, dropped connection, crash, output we
    could not read): hold the full reserve until reconciled."""


def month_of(moment: datetime) -> datetime:
    """Start of the UTC calendar month (the budget month)."""
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# ------------------------------------------------------------------ bounds
#: Fixed per-request and per-item overhead for role/format tokens.
_REQUEST_OVERHEAD = 64
_ITEM_OVERHEAD = 16


def _utf8(value: str) -> int:
    return len(value.encode("utf-8"))


def _json_bytes(value: object) -> int:
    return _utf8(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def input_bound(request: LLMRequest, schema: type[BaseModel] | None = None) -> int:
    """A conservative upper bound on the input tokens `request` can be billed.

    A byte-level BPE token covers at least one UTF-8 byte, so the byte length
    of every serialized part bounds its token count. Tool and structured-
    output schemas are re-rendered by the provider, so they count twice, plus
    fixed overhead. Cached tokens are charged at full price (no discount is
    verified). Never content-bearing: only lengths leave this function.
    """
    total = _REQUEST_OVERHEAD + _utf8(request.instructions)
    for item in request.items:
        total += _ITEM_OVERHEAD
        match item:
            case TextItem():
                total += _utf8(item.text)
            case ToolResultItem():
                total += _utf8(item.call_id) + _utf8(item.output)
            case ProviderItems():
                total += _json_bytes(list(item.payload))
    for tool in request.tools:
        total += _ITEM_OVERHEAD + 2 * (
            _utf8(tool.name) + _utf8(tool.description) + _json_bytes(dict(tool.parameters))
        )
    if schema is not None:
        total += _ITEM_OVERHEAD + 2 * _json_bytes(schema.model_json_schema())
    return total


# ----------------------------------------------------------------- spending
async def month_spend(session: AsyncSession, *, account_id: int, since: datetime) -> Decimal:
    """USD committed in admission months from `since` (a month start) on.

    Settled charges, plus every held reservation (reserved, dispatched,
    billing_uncertain), plus pre-I16 / unadmitted `llm_calls` estimates —
    a call linked to an admission is counted once, through the admission.
    """
    contribution = case(
        (LlmAdmission.status == "completed", LlmAdmission.charged_usd),
        (LlmAdmission.status.in_(HELD_STATUSES), LlmAdmission.reserved_usd),
        else_=0,
    )
    admitted = (
        await session.execute(
            select(func.coalesce(func.sum(contribution), 0)).where(
                LlmAdmission.account_id == account_id, LlmAdmission.month_start >= since
            )
        )
    ).scalar_one()
    unadmitted = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(c.cost_estimate), 0) FROM app.llm_calls c "
                "WHERE c.account_id = :account_id AND c.created_at >= :since "
                "AND NOT EXISTS (SELECT 1 FROM app.llm_admissions a WHERE a.llm_call_id = c.id)"
            ),
            {"account_id": account_id, "since": since},
        )
    ).scalar_one()
    return Decimal(admitted) + Decimal(unadmitted)


async def billing_uncertain_count(session: AsyncSession, *, account_id: int) -> int:
    """Attempts whose billing is not known yet (dispatched or uncertain)."""
    return int(
        (
            await session.execute(
                select(func.count()).where(
                    LlmAdmission.account_id == account_id,
                    LlmAdmission.status.in_(("dispatched", "billing_uncertain")),
                )
            )
        ).scalar_one()
    )


async def _lock_account(session: AsyncSession, account_id: int) -> bool:
    # FOR NO KEY UPDATE: conflicts with deletion's lock, not with ingestion's
    # foreign-key share (see privacy/deletion.py).
    row = await session.execute(
        text("SELECT id FROM app.accounts WHERE id = :id FOR NO KEY UPDATE"), {"id": account_id}
    )
    return row.first() is not None


async def _lock_admission(
    session: AsyncSession, account_id: int, admission_id: int
) -> LlmAdmission | None:
    return await session.scalar(
        select(LlmAdmission)
        .where(LlmAdmission.account_id == account_id, LlmAdmission.id == admission_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


# ---------------------------------------------------------------- admission
async def admit_call(
    session: AsyncSession,
    *,
    account_id: int,
    operation_id: UUID,
    run_id: int | None,
    attempt_id: UUID,
    model: str,
    input_bound: int,
    output_bound: int,
    now: datetime,
    monthly_budget_usd: Decimal | None,
) -> CallAllowance | AdmissionDenied:
    """Reserve the worst-case cost of one attempt, atomically per account.

    Denies an unpriced model (never free), a non-positive or negative bound,
    an account with an unreviewed bound overrun, and any reservation that
    would take the month's committed spend past `monthly_budget_usd` (zero
    discretionary overshoot). The caller commits before dispatching.
    """
    price = price_for(model)
    if price is None:
        logger.warning("llm_admission_denied", reason=AdmissionDenied.UNPRICED.value)
        return AdmissionDenied.UNPRICED
    if input_bound < 0 or output_bound <= 0:
        return AdmissionDenied.INVALID_BOUND
    if not await _lock_account(session, account_id):
        return AdmissionDenied.INVALID_BOUND
    overrun = await session.scalar(
        select(
            exists().where(
                LlmAdmission.account_id == account_id,
                LlmAdmission.error_code == "bound_exceeded",
            )
        )
    )
    if overrun:
        logger.warning("llm_admission_denied", reason="bound_overrun_unreviewed")
        return AdmissionDenied.INVALID_BOUND
    if run_id is not None:
        owned = await session.scalar(
            text("SELECT 1 FROM app.agent_runs WHERE id = :run AND account_id = :account"),
            {"run": run_id, "account": account_id},
        )
        if owned is None:
            run_id = None  # never link another account's run; the call is still metered
    reserved = price.cost(input_bound, output_bound)
    month = month_of(now)
    if monthly_budget_usd is not None:
        spent = await month_spend(session, account_id=account_id, since=month)
        if spent + reserved > monthly_budget_usd:
            logger.warning("llm_admission_denied", reason=AdmissionDenied.MONTHLY_BUDGET.value)
            return AdmissionDenied.MONTHLY_BUDGET
    row = LlmAdmission(
        account_id=account_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        run_id=run_id,
        model=model,
        price_version=price.version,
        month_start=month,
        input_bound=input_bound,
        output_bound=output_bound,
        reserved_usd=reserved,
        status="reserved",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return CallAllowance(row.id, reserved, input_bound, output_bound)


async def mark_dispatched(
    session: AsyncSession, *, account_id: int, admission_id: int, now: datetime
) -> bool:
    """Record that the provider attempt is about to start. Committed before
    the call, so a `reserved` row is proof that no attempt started."""
    if not await _lock_account(session, account_id):
        return False
    row = await _lock_admission(session, account_id, admission_id)
    if row is None or row.status != "reserved":
        return False
    row.status = "dispatched"
    row.updated_at = now
    return True


async def settle_call(
    session: AsyncSession,
    *,
    account_id: int,
    admission_id: int,
    outcome: CallOutcome,
    usage: Usage | None,
    actual_model: str | None,
    now: datetime,
    purpose: str = "settlement",
    request_id: str | None = None,
    error_code: str | None = None,
) -> bool:
    """Settle one attempt at most once. Returns whether anything changed.

    `completed` with usage writes the `llm_calls` row (run link as it is
    now — NULL after deletion) and charges the attempt; it is accepted from
    `reserved`, `dispatched` or `billing_uncertain` (late response or
    explicit reconciliation). Usage beyond the reserved bound is recorded as
    its true cost and blocks further admission until reviewed. Without usage
    a completion stays `billing_uncertain`. Settlement needs no consent and
    no content, so it works after deletion or withdrawal.
    """
    if not await _lock_account(session, account_id):
        return False
    row = await _lock_admission(session, account_id, admission_id)
    if row is None or row.status in ("completed", "released"):
        return False
    if outcome is CallOutcome.COMPLETED and usage is None:
        outcome, error_code = CallOutcome.BILLING_UNCERTAIN, error_code or "invalid_output"
    match outcome:
        case CallOutcome.NOT_DISPATCHED:
            if row.status != "reserved":
                return False  # once dispatched, nothing can call it unused
            row.status, row.charged_usd, row.error_code = "released", Decimal(0), "not_dispatched"
        case CallOutcome.DEFINITE_FAILURE:
            if row.status not in ("reserved", "dispatched"):
                return False
            row.status, row.charged_usd, row.error_code = "released", Decimal(0), error_code
        case CallOutcome.BILLING_UNCERTAIN:
            if row.status == "billing_uncertain":
                return False
            row.status, row.error_code = "billing_uncertain", error_code
            logger.warning("llm_billing_uncertain", admission_id=row.id, code=error_code)
        case CallOutcome.COMPLETED:
            assert usage is not None
            price = price_for(row.model)
            if price is None:  # a price removed after admission: keep it held, visibly
                row.status, row.error_code = "billing_uncertain", error_code
                row.updated_at = now
                return True
            charged = price.cost(usage.input_tokens, usage.output_tokens)
            call_id = (
                await session.execute(
                    text(
                        "INSERT INTO app.llm_calls (account_id, run_id, purpose, model, "
                        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                        "cost_estimate, request_id, created_at) VALUES (:account, :run, "
                        ":purpose, :model, :input, :output, :cache_read, :cache_write, :cost, "
                        ":request_id, :now) RETURNING id"
                    ),
                    {
                        "account": account_id,
                        "run": row.run_id,
                        "purpose": purpose,
                        "model": actual_model or row.model,
                        "input": usage.input_tokens,
                        "output": usage.output_tokens,
                        "cache_read": usage.cache_read_tokens,
                        "cache_write": usage.cache_write_tokens,
                        "cost": charged,
                        "request_id": request_id,
                        "now": now,
                    },
                )
            ).scalar_one()
            row.status, row.charged_usd, row.llm_call_id = "completed", charged, call_id
            row.error_code = None
            if charged > row.reserved_usd:
                row.error_code = "bound_exceeded"
                logger.error("llm_bound_exceeded", admission_id=row.id)
    row.updated_at = now
    return True


async def review_bound_overrun(
    session: AsyncSession, *, account_id: int, admission_id: int
) -> bool:
    """Operator acknowledgement of a recorded overrun; admission resumes.
    The true charge stays as recorded."""
    if not await _lock_account(session, account_id):
        return False
    row = await _lock_admission(session, account_id, admission_id)
    if row is None or row.error_code != "bound_exceeded":
        return False
    row.error_code = "bound_exceeded_reviewed"
    return True
