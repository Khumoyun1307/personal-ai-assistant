"""The durable inbox (ARCHITECTURE §6.1, §10.3).

Every Telegram update is written here before anything else happens, and the
poller's offset only advances after the write commits. That is what makes
"crash mid-processing loses nothing" true.

Note the deliberate exception to tenant scoping: `account_id` is NULLABLE here.
An update arrives before we know whose it is, and updates from non-allowlisted
users never get one at all — their payload is cleared at processing and no
domain rows are created (§6 invariant 12).
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from assistant.db.base import Base, CreatedAt, IdPk

INBOUND_STATUSES = ("pending", "processed", "ignored", "rejected", "failed")


class InboundUpdate(Base):
    """One raw Telegram update, exactly once.

    CONTENT-BEARING (`payload`) — ARCHITECTURE §10.1; cleared at processing for
    ignored/rejected rows, otherwise purged after 7 days (§10.4).
    """

    __tablename__ = "inbound_updates"
    __table_args__ = (
        # The idempotency guarantee: Telegram may redeliver an update after a
        # crash, and this is what makes the redelivery a no-op. Keyed by bot id
        # too, because dev and prod bots number their updates independently.
        UniqueConstraint("bot_id", "update_id", name="uq_inbound_updates_bot_id_update_id"),
        CheckConstraint(
            "status IN ('pending', 'processed', 'ignored', 'rejected', 'failed')",
            name="ck_inbound_updates_status",
        ),
        # The claim query: oldest pending first.
        Index(
            "ix_inbound_updates_pending",
            "received_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_inbound_updates_account_id", "account_id"),
    )

    id: Mapped[IdPk]
    bot_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    #: NULL until routed, and permanently NULL for non-allowlisted senders.
    account_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="SET NULL"), nullable=True
    )
    #: Set once the update becomes a stored message, so deleting that message
    #: can clear this payload too (§6.5 step 6). The FK (revision 0005) is
    #: SET NULL: purging a message must not purge the audit of its arrival.
    message_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("app.messages.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
        index=True,
    )
    #: `none_as_null`: clearing the payload must store SQL NULL, not a JSON
    #: `null` value, so `IS NULL` checks (retention, audits) see it as empty.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_at: Mapped[CreatedAt]
    processed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class Task(Base):
    """A tracked commitment or question. `title` and `details` are content."""

    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('owner_commitment','contact_commitment','open_question',"
            "'follow_up','reminder','decision')",
            name="kind",
        ),
        CheckConstraint("status IN ('open','done','cancelled','expired')", name="status"),
        CheckConstraint("created_by IN ('owner','agent')", name="created_by"),
        Index(
            "ux_tasks_agent_dedupe",
            "conversation_id",
            "kind",
            "first_source_message_id",
            unique=True,
            postgresql_where=text("created_by = 'agent'"),
        ),
        Index("ix_tasks_account_id_status_due_at", "account_id", "status", "due_at"),
        Index("ix_tasks_conversation_id_status", "conversation_id", "status"),
    )
    id: Mapped[IdPk] = mapped_column(Identity(always=True))
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.conversations.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    closed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_message_ids: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )
    first_source_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[CreatedAt]
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)


class ConversationTriage(Base):
    """Latest verdict per chat; `reasons` is content and follows chat retention."""

    __tablename__ = "conversation_triage"
    __table_args__ = (
        Index("ix_conversation_triage_account_id", "account_id"),
        UniqueConstraint("conversation_id", name="uq_conversation_triage_conversation_id"),
        CheckConstraint("urgency IN ('low','normal','high','urgent')", name="urgency"),
        CheckConstraint(
            "escalation_category IN ('none','urgent_decision','needs_owner','fyi','suspicious')",
            name="escalation_category",
        ),
        CheckConstraint("script IN ('latin','cyrillic','mixed','other')", name="script"),
    )
    id: Mapped[IdPk] = mapped_column(Identity(always=True))
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.conversations.id", ondelete="CASCADE"), nullable=False
    )
    covers_through_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    needs_reply: Mapped[bool] = mapped_column(nullable=False)
    contains_question: Mapped[bool] = mapped_column(nullable=False)
    requests_decision: Mapped[bool] = mapped_column(nullable=False)
    urgency: Mapped[str] = mapped_column(Text, nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    escalation_category: Mapped[str] = mapped_column(Text, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default=text("'[]'"))
    language: Mapped[str | None] = mapped_column(Text, nullable=True)
    script: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[CreatedAt]


class Digest(Base):
    """Content-free delivery ledger for one account's digest period."""

    __tablename__ = "digests"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "period_start",
            "period_end",
            name="uq_digests_account_id_period_start_period_end",
        ),
        CheckConstraint("status IN ('building','sent','failed','uncertain')", name="status"),
    )
    id: Mapped[IdPk] = mapped_column(Identity(always=True))
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    period_start: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[CreatedAt]


class Notification(Base):
    """Content-free notification references; text is rendered at delivery."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("account_id", "dedupe_key", name="uq_notifications_account_id_dedupe_key"),
        CheckConstraint("alert_class IN ('system','standard')", name="alert_class"),
        CheckConstraint("priority IN ('low','normal','high','urgent')", name="priority"),
        CheckConstraint(
            "status IN ('queued','sent','suppressed','batched','failed','uncertain')", name="status"
        ),
        CheckConstraint("feedback IN ('useful','not_useful')", name="feedback"),
        Index(
            "ix_notifications_account_id_status_deliver_after",
            "account_id",
            "status",
            "deliver_after",
        ),
    )
    id: Mapped[IdPk] = mapped_column(Identity(always=True))
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    alert_class: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'standard'")
    )
    priority: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    dedupe_key: Mapped[str] = mapped_column(Text, nullable=False)
    refs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.conversations.id", ondelete="SET NULL"), nullable=True
    )
    contact_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.contacts.id", ondelete="SET NULL"), nullable=True
    )
    reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    bypass_quiet_hours: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    deliver_after: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    digest_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.digests.id", ondelete="SET NULL"), nullable=True
    )
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[CreatedAt]
    sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
