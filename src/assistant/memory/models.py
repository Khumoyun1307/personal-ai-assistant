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

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, Text
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
