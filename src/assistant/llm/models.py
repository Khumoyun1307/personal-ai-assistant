"""`llm_admissions`: one priced, reserved provider attempt (I16, C7).

Structural accounting only: ids, model identifier, token bounds, USD amounts,
a fixed status and a fixed error classification. No content is ever stored
here, so the row survives every content deletion and consent withdrawal and
a late provider response can still settle the same attempt exactly once.

The account foreign key is `ON DELETE RESTRICT` — the deliberate accounting
exception to the shared CASCADE convention: a physical account removal must
not silently erase outstanding liabilities (C7 retention).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from assistant.db.base import Base, CreatedAt, IdPk

ADMISSION_STATUSES = ("reserved", "dispatched", "completed", "released", "billing_uncertain")
#: Statuses whose `reserved_usd` is still held against the admission month.
HELD_STATUSES = ("reserved", "dispatched", "billing_uncertain")
#: Fixed error classifications; never provider prose.
ADMISSION_ERROR_CODES = (
    "not_dispatched",
    "rate_limited",
    "auth",
    "bad_request",
    "provider_error",
    "timeout",
    "connection",
    "invalid_output",
    "interrupted",
    "stale_dispatch",
    "bound_exceeded",
    "bound_exceeded_reviewed",
)
_USD = Numeric(18, 9)


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


class LlmAdmission(Base):
    __tablename__ = "llm_admissions"
    __table_args__ = (
        UniqueConstraint("account_id", "attempt_id", name="uq_llm_admissions_account_attempt"),
        CheckConstraint(f"status IN {_in(ADMISSION_STATUSES)}", name="status"),
        CheckConstraint(
            f"error_code IS NULL OR error_code IN {_in(ADMISSION_ERROR_CODES)}", name="error_code"
        ),
        CheckConstraint("input_bound >= 0 AND output_bound >= 0", name="bounds"),
        CheckConstraint(
            "reserved_usd >= 0 AND (charged_usd IS NULL OR charged_usd >= 0)", name="usd"
        ),
        Index("ix_llm_admissions_account_id_month_start", "account_id", "month_start"),
        Index("ix_llm_admissions_status_updated_at", "status", "updated_at"),
        Index("ix_llm_admissions_run_id", "run_id"),
        Index("ix_llm_admissions_llm_call_id", "llm_call_id"),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="RESTRICT"), nullable=False
    )
    operation_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    llm_call_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.llm_calls.id", ondelete="SET NULL"), nullable=True
    )
    #: A configured model identifier that has a price (`llm/pricing.py`).
    model: Mapped[str] = mapped_column(Text, nullable=False)
    #: `ModelPrice.version` the reservation was computed with.
    price_version: Mapped[str] = mapped_column(Text, nullable=False)
    #: UTC calendar month of the reservation; settlement counts in this month.
    month_start: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    input_bound: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_bound: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_usd: Mapped[Decimal] = mapped_column(_USD, nullable=False)
    charged_usd: Mapped[Decimal | None] = mapped_column(_USD, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[CreatedAt]
