"""M03 S03.2: conversation summaries.

- `conversation_summaries`: a model-written summary of ONE conversation,
  `kind` rolling (one per conversation, incremental, up to
  `covers_through_message_id`) or period (a catch-up cache for one range,
  valid while `source_digest` matches). `summary` (jsonb) is
  CONTENT-BEARING (ARCHITECTURE §10.1): deleted by managed-data deletion,
  conversation purge and message deletion (invalidation); period rows are
  swept after 30 days, rolling rows are kept until deleted (§10.4).
  `source_digest` hashes message ids and update times only (structural).
- No change to `conversations`: `pending_ai_since` is the refresh flag.

Hand-written and reviewed (AGENTS §12). Data-handling note for the
downgrade: it drops the table and with it every stored summary (content);
nothing older reads them.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

APP = "app"


def upgrade() -> None:
    op.create_table(
        "conversation_summaries",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("account_id", sa.BigInteger, nullable=False),
        sa.Column("conversation_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("period_start", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("period_end", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("covers_through_message_id", sa.BigInteger, nullable=True),
        sa.Column("message_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("source_digest", sa.Text, nullable=True),
        sa.Column("summary", postgresql.JSONB, nullable=False),
        sa.Column("prompt_version", sa.Text, nullable=True),
        sa.Column("model", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            [f"{APP}.accounts.id"],
            name="fk_conversation_summaries_account_id_accounts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            [f"{APP}.conversations.id"],
            name="fk_conversation_summaries_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "kind IN ('rolling', 'period')", name="ck_conversation_summaries_kind"
        ),
        sa.CheckConstraint(
            "(kind = 'rolling' AND period_start IS NULL AND period_end IS NULL) "
            "OR (kind = 'period' AND period_start IS NOT NULL)",
            name="ck_conversation_summaries_period_bounds",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversation_summaries"),
        schema=APP,
    )
    op.create_index(
        "ix_conversation_summaries_account_id_conversation_id",
        "conversation_summaries",
        ["account_id", "conversation_id"],
        schema=APP,
    )
    op.create_index(
        "uq_conversation_summaries_rolling",
        "conversation_summaries",
        ["conversation_id"],
        unique=True,
        schema=APP,
        postgresql_where=sa.text("kind = 'rolling'"),
    )
    op.create_index(
        "uq_conversation_summaries_period",
        "conversation_summaries",
        ["conversation_id", "period_start", "period_end"],
        unique=True,
        schema=APP,
        postgresql_where=sa.text("kind = 'period'"),
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_index("uq_conversation_summaries_period", "conversation_summaries", schema=APP)
    op.drop_index("uq_conversation_summaries_rolling", "conversation_summaries", schema=APP)
    op.drop_index(
        "ix_conversation_summaries_account_id_conversation_id",
        "conversation_summaries",
        schema=APP,
    )
    op.drop_table("conversation_summaries", schema=APP)
