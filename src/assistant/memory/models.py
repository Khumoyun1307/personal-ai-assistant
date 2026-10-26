"""Conversation summaries (ARCHITECTURE §10.3, §11.1; S03.2).

One table, two kinds:

- **rolling** — one row per conversation, the running summary of everything
  up to `covers_through_message_id`. Refreshed incrementally after activity
  settles (`memory.refresh_summary`); long-term memory once raw messages
  expire (FR-MEM-04). Kept until deleted (§10.4).
- **period** — a cache of one conversation's summary for one catch-up range
  (`period_start`, `period_end`; NULL end = "until the latest message"),
  valid while `source_digest` still matches the messages in the range.
  Purged by the retention sweep after `PERIOD_SUMMARY_RETENTION`.

CONTENT-BEARING: `summary` (model output derived from contact messages:
registered in `privacy/content_columns.py`, deleted by every deletion
cascade). `source_digest` is a hash over message ids and update times only.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from assistant.db.base import Base, CreatedAt, IdPk

KIND_ROLLING = "rolling"
KIND_PERIOD = "period"
SUMMARY_KINDS = (KIND_ROLLING, KIND_PERIOD)


class ConversationSummary(Base):
    """A model-written summary of one conversation (never of several: §6
    invariant 4)."""

    __tablename__ = "conversation_summaries"
    __table_args__ = (
        CheckConstraint("kind IN ('rolling', 'period')", name="kind"),
        CheckConstraint(
            "(kind = 'rolling' AND period_start IS NULL AND period_end IS NULL) "
            "OR (kind = 'period' AND period_start IS NOT NULL)",
            name="period_bounds",
        ),
        CheckConstraint("period_end IS NULL OR period_end > period_start", name="period_order"),
        Index(
            "ix_conversation_summaries_account_id_conversation_id",
            "account_id",
            "conversation_id",
        ),
        Index(
            "uq_conversation_summaries_rolling",
            "conversation_id",
            unique=True,
            postgresql_where=sql_text("kind = 'rolling'"),
        ),
        Index(
            "uq_conversation_summaries_period",
            "conversation_id",
            "period_start",
            "period_end",
            unique=True,
            postgresql_where=sql_text("kind = 'period'"),
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.conversations.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    #: The highest `messages.id` the summary has seen. Not a foreign key: the
    #: summary is deleted with the messages by every cascade that removes them.
    covers_through_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: How many messages the summary was written from (this call, for a
    #: period; cumulative, for a rolling summary).
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    #: Period only: hash over the range's (message id, updated_at) pairs. A
    #: new, edited or deleted message changes it and the cache is not reused.
    source_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The validated `ConversationSummaryOutput` (agent/schemas.py). CONTENT.
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[CreatedAt]


# ------------------------------------------------------------------ memory items
SCOPE_OWNER = "owner"
SCOPE_CONTACT = "contact"
SCOPE_CONVERSATION = "conversation"
#: `delegation` scope arrives with delegations (M06); not built yet.
MEMORY_SCOPES = (SCOPE_OWNER, SCOPE_CONTACT, SCOPE_CONVERSATION)
#: `shareable` (FR-MEM-08) is R2; not built yet.
MEMORY_KINDS = ("instruction", "preference", "fact", "style")
#: `agent_inferred` (FR-MEM-05) is R2 and needs a review flow first.
MEMORY_SOURCES = ("owner_stated",)
STATUS_ACTIVE = "active"
#: Forgotten: never used again, hard-deleted once the Undo window has passed.
STATUS_ARCHIVED = "archived"
MEMORY_STATUSES = (STATUS_ACTIVE, STATUS_ARCHIVED)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class MemoryItem(Base):
    """An owner instruction, preference, fact or contact note (ARCHITECTURE
    §10.3, §11.1; S03.3, FR-MEM-01/03).

    Scoped by foreign key, so deleting a contact or a conversation deletes its
    notes (§10.4): `scope` names exactly which FK is set (none for `owner`).

    CONTENT-BEARING: `content` (the owner's words; registered in
    `privacy/content_columns.py`). `provenance` holds flags only (whether the
    item came from a run that had third-party text in it).
    """

    __tablename__ = "memory_items"
    __table_args__ = (
        CheckConstraint(_in("scope", MEMORY_SCOPES), name="scope"),
        CheckConstraint(_in("kind", MEMORY_KINDS), name="kind"),
        CheckConstraint(_in("source", MEMORY_SOURCES), name="source"),
        CheckConstraint(_in("status", MEMORY_STATUSES), name="status"),
        CheckConstraint(
            "(scope = 'owner' AND contact_id IS NULL AND conversation_id IS NULL) "
            "OR (scope = 'contact' AND contact_id IS NOT NULL AND conversation_id IS NULL) "
            "OR (scope = 'conversation' AND conversation_id IS NOT NULL AND contact_id IS NULL)",
            name="scope_target",
        ),
        CheckConstraint("(status = 'archived') = (archived_at IS NOT NULL)", name="archived_at"),
        CheckConstraint(
            "NOT applies_to_replies OR kind = 'instruction'", name="applies_to_replies"
        ),
        UniqueConstraint("run_id", "tool_call_id", name="uq_memory_items_run_id_tool_call_id"),
        Index("ix_memory_items_account_id_status", "account_id", "status"),
        Index("ix_memory_items_contact_id", "contact_id"),
        Index("ix_memory_items_conversation_id", "conversation_id"),
    )

    id: Mapped[IdPk]
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("app.accounts.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    contact_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.contacts.id", ondelete="CASCADE"), nullable=True
    )
    conversation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.conversations.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    #: CONTENT.
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sql_text("'owner_stated'")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sql_text("'active'"))
    #: Flags only, e.g. `{"tainted": true}`: saved from a run that had other
    #: people's text in its context (the owner confirmed it on a card).
    provenance: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sql_text("'{}'")
    )
    #: Owner-set: an owner instruction that reply drafting must follow (§7.3).
    applies_to_replies: Mapped[bool] = mapped_column(
        nullable=False, server_default=sql_text("false")
    )
    #: The run and tool call that saved it: a repeated call saves nothing new.
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("app.agent_runs.id", ondelete="SET NULL"), nullable=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[CreatedAt]
    archived_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
