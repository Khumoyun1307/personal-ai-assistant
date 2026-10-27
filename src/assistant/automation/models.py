"""Persisted automation rules and content-free occurrence ledger (0014).

`spec` is content-bearing; all other fields are structural.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from assistant.db.base import Base, CreatedAt, IdPk


class Automation(Base):
    __tablename__ = "automations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('reminder','monitor','follow_up','scheduled_summary',"
            "'delegation_end','digest')",
            name="kind",
        ),
        CheckConstraint(
            "status IN ('active','paused','completed','cancelled','expired')", name="status"
        ),
        CheckConstraint("tz_policy IN ('follow_owner','fixed')", name="tz_policy"),
        UniqueConstraint("run_id", "tool_call_id", name="uq_automations_run_id_tool_call_id"),
        Index(
            "ix_automations_due",
            "next_run_at",
            postgresql_where=sql_text("status = 'active' AND next_run_at IS NOT NULL"),
        ),
        Index("ix_automations_account_id_kind_status", "account_id", "kind", "status"),
    )
    id: Mapped[IdPk] = mapped_column(Identity(always=True))
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.tasks.id", ondelete="SET NULL"), nullable=True
    )
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    refs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'active'"))
    rrule: Mapped[str | None] = mapped_column(Text, nullable=True)
    run_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    tz_policy: Mapped[str] = mapped_column(Text, nullable=False)
    next_run_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[CreatedAt]
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class AutomationOccurrence(Base):
    __tablename__ = "automation_occurrences"
    __table_args__ = (
        Index("ix_automation_occurrences_account_id", "account_id"),
        UniqueConstraint(
            "automation_id",
            "occurrence_at",
            name="uq_automation_occurrences_automation_id_occurrence_at",
        ),
        CheckConstraint("status IN ('claimed','fired','skipped')", name="status"),
    )
    id: Mapped[IdPk] = mapped_column(Identity(always=True))
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    automation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.automations.id", ondelete="CASCADE"), nullable=False
    )
    occurrence_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    note_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    fired_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[CreatedAt]
