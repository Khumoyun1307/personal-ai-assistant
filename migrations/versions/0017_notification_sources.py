"""Retain triggering verdict explanations for the lifetime of an alert.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-09

Content: notification_sources.reasons follows message retention and cascades
with its notification (30 days). Legacy alerts cannot recover overwritten
verdicts and intentionally remain unbound. Downgrade deletes these copies.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_sources",
        sa.Column("notification_id", sa.BigInteger(), primary_key=True),
        sa.Column("account_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("verdict_id", sa.BigInteger(), nullable=False),
        sa.Column("covers_through_message_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column("verdict_state", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["notification_id"], ["app.notifications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["app.accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["app.conversations.id"], ondelete="CASCADE"),
        schema="app",
    )
    op.create_index(
        "ix_notification_sources_account_id", "notification_sources", ["account_id"], schema="app"
    )


def downgrade() -> None:
    op.drop_table("notification_sources", schema="app")
