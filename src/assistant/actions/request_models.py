"""Structural owner-request storage (C1; revision 0015). No copied prose."""

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, MappedColumn, mapped_column

from assistant.db.base import Base, IdPk

OPERATIONS = ("dictate", "compose", "reply", "revise")
REQUEST_STATES = (
    "collecting",
    "ready",
    "preparing",
    "awaiting_approval",
    "suspended",
    "completed",
    "cancelled",
    "superseded",
    "expired",
)
TERMINAL_REQUEST_STATES = ("completed", "cancelled", "superseded", "expired")
MISSING_SLOTS = ("recipient", "content", "target", "constraints")
SELECTION_BASES = ("unique_strong", "owner_choice", "explicit_reply")
REQUEST_REASONS = (
    "owner_cancelled",
    "topic_switch",
    "source_unavailable",
    "recipient_changed",
    "superseded",
    "max_age",
    "action_terminal",
    "restored_from_backup",
)
SOURCE_FIELDS = ("text", "quoted_text")
SOURCE_PURPOSES = ("instruction", "literal_body", "constraint", "quote", "suggestion")


def enum_check(column: str, values: tuple[str, ...]) -> CheckConstraint:
    choices = ",".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({choices})", name=column)


def reference(target: str, *, ondelete: str = "CASCADE") -> MappedColumn[Any]:
    return mapped_column(BigInteger, ForeignKey(f"app.{target}.id", ondelete=ondelete))


class OwnerRequest(Base):
    __tablename__ = "owner_requests"
    __table_args__ = (
        enum_check("operation", OPERATIONS),
        enum_check("state", REQUEST_STATES),
        enum_check("selection_basis", SELECTION_BASES),
        enum_check("missing_slot", MISSING_SLOTS),
        enum_check("reason", REQUEST_REASONS),
        CheckConstraint("version >= 1", name="version"),
        CheckConstraint("intent_revision >= 0", name="intent_revision"),
        CheckConstraint("manual_watermark >= 0", name="manual_watermark"),
        CheckConstraint(
            "(contact_id IS NULL) = (conversation_id IS NULL) AND "
            "(contact_id IS NULL) = (selection_basis IS NULL)",
            name="recipient_pair",
        ),
        CheckConstraint(
            "(action_id IS NULL) = (action_version IS NULL) AND action_version >= 1",
            name="action_pair",
        ),
        CheckConstraint(
            "missing_slot IS NULL OR state IN ('collecting','suspended')",
            name="question_state",
        ),
        CheckConstraint(
            "NOT focused OR state NOT IN ('completed','cancelled','superseded','expired')",
            name="terminal_focus",
        ),
        CheckConstraint(
            "(ended_at IS NOT NULL) = (state IN ('completed','cancelled','superseded','expired'))",
            name="ended_state",
        ),
        UniqueConstraint("account_id", "trigger_turn_id"),
        Index("ix_owner_requests_account_id", "account_id"),
        Index("ix_owner_requests_account_id_expires_at", "account_id", "expires_at"),
        Index(
            "uq_owner_requests_focused", "account_id", unique=True, postgresql_where=text("focused")
        ),
    )

    id: Mapped[IdPk] = mapped_column(Identity(always=True))
    account_id: Mapped[int] = reference("accounts")
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    operation: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    focused: Mapped[bool] = mapped_column(server_default=text("false"))
    trigger_turn_id: Mapped[int] = reference("control_turns")
    last_turn_id: Mapped[int] = reference("control_turns")
    contact_id: Mapped[int | None] = reference("contacts")
    conversation_id: Mapped[int | None] = reference("conversations")
    selection_basis: Mapped[str | None] = mapped_column(Text)
    intent_revision: Mapped[int | None] = mapped_column(BigInteger)
    manual_watermark: Mapped[int | None] = mapped_column(BigInteger)
    resolution_candidates: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    missing_slot: Mapped[str | None] = mapped_column(Text)
    question_input_id: Mapped[int | None] = reference("pending_inputs", ondelete="SET NULL")
    action_id: Mapped[int | None] = reference("actions", ondelete="SET NULL")
    action_version: Mapped[int | None] = mapped_column(Integer)
    target_message_id: Mapped[int | None] = reference("messages", ondelete="SET NULL")
    constraints: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'"))
    requested_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)


class OwnerRequestSource(Base):
    __tablename__ = "owner_request_sources"
    __table_args__ = (
        enum_check("field", SOURCE_FIELDS),
        enum_check("purpose", SOURCE_PURPOSES),
        CheckConstraint("ordinal >= 0 AND ordinal < 64", name="ordinal"),
        CheckConstraint("start_char >= 0 AND end_char > start_char", name="bounds"),
        CheckConstraint("field_sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
        CheckConstraint("field != 'quoted_text' OR purpose = 'quote'", name="quoted_field"),
        UniqueConstraint("account_id", "request_id", "ordinal"),
        Index("ix_owner_request_sources_account_id", "account_id"),
        Index("ix_owner_request_sources_account_id_turn_id", "account_id", "turn_id"),
    )

    id: Mapped[IdPk] = mapped_column(Identity(always=True))
    account_id: Mapped[int] = reference("accounts")
    request_id: Mapped[int] = reference("owner_requests")
    ordinal: Mapped[int] = mapped_column(Integer)
    turn_id: Mapped[int] = reference("control_turns")
    field: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(Text)
    start_char: Mapped[int] = mapped_column(Integer)
    end_char: Mapped[int] = mapped_column(Integer)
    field_sha256: Mapped[str] = mapped_column(Text)
