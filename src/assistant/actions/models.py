"""Owner-voice actions (revision 0013; ARCHITECTURE §9.4, §10.3).

Only `text` is content-bearing; privacy retention and every deletion scope
clear it. The remaining columns are ids, hashes, enums, flags and dates.
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
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from assistant.db.base import Base, CreatedAt, IdPk


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (
        CheckConstraint("kind IN ('send_message','edit_message','delete_own')", name="kind"),
        CheckConstraint(
            "origin IN ('owner_dictated','agent_proposal','delegation_auto')", name="origin"
        ),
        CheckConstraint(
            "state IN ('proposed','awaiting_approval','revising','approved','scheduled',"
            "'executing','succeeded','failed','delivery_uncertain','rejected','outdated',"
            "'expired','cancelled','blocked')",
            name="state",
        ),
        CheckConstraint("version >= 1", name="version"),
        UniqueConstraint("run_id", "tool_call_id", name="uq_actions_run_id_tool_call_id"),
        UniqueConstraint("approval_token", name="uq_actions_approval_token"),
        Index(
            "ux_actions_live_card",
            "conversation_id",
            unique=True,
            postgresql_where=sql_text("state IN ('awaiting_approval','revising')"),
        ),
        Index("ix_actions_account_id_created_at", "account_id", "created_at"),
        Index("ix_actions_conversation_id_state", "conversation_id", "state"),
        Index(
            "ix_actions_inflight",
            "state",
            "attempt_started_at",
            postgresql_where=sql_text("state IN ('approved','executing','delivery_uncertain')"),
        ),
        Index(
            "ix_actions_expiring",
            "expires_at",
            postgresql_where=sql_text("state IN ('awaiting_approval','revising','approved')"),
        ),
        Index(
            "ix_actions_trigger_turn_id_conversation_id",
            "trigger_turn_id",
            "conversation_id",
            postgresql_where=sql_text("trigger_turn_id IS NOT NULL"),
        ),
        Index(
            "ix_actions_card_unsynced",
            "account_id",
            postgresql_where=sql_text(
                "card_message_id IS NOT NULL AND card_state IS DISTINCT FROM state"
            ),
        ),
    )

    id: Mapped[IdPk] = mapped_column(Identity(always=True))
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.conversations.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    origin: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'proposed'"))
    state_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("1"))
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_text_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_text_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    base_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.messages.id", ondelete="SET NULL"), nullable=True
    )
    shown_through_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    target_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.messages.id", ondelete="SET NULL"), nullable=True
    )
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    draft_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    trigger_turn_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.control_turns.id", ondelete="SET NULL"), nullable=True
    )
    revision_turn_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.control_turns.id", ondelete="SET NULL"), nullable=True
    )
    draft_flags: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    policy_decision: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    approval_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    card_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    card_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_started_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    retry_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    identity_pending: Mapped[bool] = mapped_column(nullable=False, server_default=sql_text("false"))
    error_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[CreatedAt]
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
