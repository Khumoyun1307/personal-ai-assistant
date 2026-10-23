"""Agent run accounting (ARCHITECTURE §10.3).

These tables exist to make model usage auditable and billable: what ran, which
prompt version, how many tokens, what it cost. None of them store content —
`agent_run_steps` keeps a digest of tool arguments, never the arguments. The
one exception is `tool_confirmations.args`, held only until the owner decides
(see the class).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from assistant.db.base import Base, CreatedAt, IdPk

RUN_TYPES = (
    "owner_request",
    "triage",
    "draft_reply",
    "summarize",
    "extract_memory",
    "evaluate_monitor",
    "interpret_instruction",
)
RUN_STATUSES = ("running", "succeeded", "failed", "refused", "budget_exceeded")


class AgentRun(Base):
    """One agent invocation."""

    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "run_type IN " + str(RUN_TYPES).replace('"', "'"), name="ck_agent_runs_run_type"
        ),
        CheckConstraint(
            "status IN " + str(RUN_STATUSES).replace('"', "'"), name="ck_agent_runs_status"
        ),
        Index("ix_agent_runs_account_id_started_at", "account_id", "started_at"),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    run_type: Mapped[str] = mapped_column(Text, nullable=False)
    #: What triggered the run: an id or enum, never content.
    trigger_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    effort: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'running'"))
    iterations: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Set when the run saw forwarded/quoted third-party text or a tool result
    #: carrying contact-authored text. A tainted run cannot change state
    #: without an owner confirmation card (§6 invariant 3b).
    tainted: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    started_at: Mapped[CreatedAt]
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    error_class: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentRunStep(Base):
    """One tool call within a run. Content-free by construction."""

    __tablename__ = "agent_run_steps"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_agent_run_steps_run_id_seq"),)

    id: Mapped[IdPk]
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    #: A hash of the arguments, so a retry can be recognised as the same call
    #: without storing what was passed.
    args_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[CreatedAt]


CONFIRMATION_STATUSES = ("pending", "confirmed", "applied", "failed", "rejected", "expired")


class ToolConfirmation(Base):
    """A model-initiated state change held for the owner's tap (§7.1, S03.1).

    In a tainted run a `configure`/`control` call is not executed: its
    validated arguments wait here and the owner gets a confirmation card.
    Confirm → `agent.apply_confirmation` runs it once (`confirmed` →
    `applied` / `failed`); Reject → `rejected`; no tap → `expired`.

    CONTENT-BEARING: `args` (the model may have put contact text in them).
    Nulled as soon as the call is decided, applied or expired (§10.4).
    """

    __tablename__ = "tool_confirmations"
    __table_args__ = (
        UniqueConstraint("run_id", "tool_call_id", name="uq_tool_confirmations_run_id_call_id"),
        CheckConstraint(
            "status IN " + str(CONFIRMATION_STATUSES).replace('"', "'"),
            name="ck_tool_confirmations_status",
        ),
        Index("ix_tool_confirmations_account_id_status", "account_id", "status"),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    tool_call_id: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False)
    args: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    created_at: Mapped[CreatedAt]
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class LlmCall(Base):
    """One provider request, for budgeting (NFR-10)."""

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_account_id_created_at", "account_id", "created_at"),)

    id: Mapped[IdPk]
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: USD. Numeric, not float: money must not drift.
    cost_estimate: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    request_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]
