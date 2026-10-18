"""Agent run accounting (ARCHITECTURE §10.3).

These tables exist to make model usage auditable and billable: what ran, which
prompt version, how many tokens, what it cost. None of them store content —
`agent_run_steps` keeps a digest of tool arguments, never the arguments.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

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
from sqlalchemy.dialects.postgresql import TIMESTAMP
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
